#!/usr/bin/env python3
"""
Shared library for the Total ALS Map retrieval substrate.

SQLite + external-content FTS5 over normalized Grok/X JSONL.
Optional Phase 2′b local embeddings (numpy + sentence-transformers).
Phase 2′c heuristic thread_features (no LLM).
Named analysis indexes (message/thread id sets or saved filters) live in the
same sqlite and optionally scope search/sample/stats/semantic.
Used by build_map_index.py, build_map_embeddings.py,
build_thread_features.py, map_query.py, and als_map_mcp.py.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import sqlite3
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GROK = REPO_ROOT / "data" / "normalized" / "grok.jsonl"
DEFAULT_X = REPO_ROOT / "data" / "normalized" / "x.jsonl"
DEFAULT_DB = REPO_ROOT / "data" / "index" / "als_map.sqlite"
DEFAULT_STATS = REPO_ROOT / "data" / "index" / "als_map-stats.json"
DEFAULT_EMBEDDINGS = REPO_ROOT / "data" / "index" / "als_map_embeddings.npz"
DEFAULT_EMBED_META = REPO_ROOT / "data" / "index" / "als_map_embeddings.meta.json"

# Hard caps (efficiency review) — keep agent/Grok token burn bounded.
MAX_LIMIT = 50
DEFAULT_SEARCH_LIMIT = 20
DEFAULT_THREAD_LIMIT = 30
DEFAULT_BROWSE_LIMIT = 30
DEFAULT_GET_MANY = 20
MAX_GET_MANY = 50
DEFAULT_SNIPPET_CHARS = 400
MAX_SNIPPET_CHARS = 2000
DEFAULT_FULL_CHARS = 32_000
MAX_FULL_CHARS = 64_000
MAX_RESPONSE_BYTES = 64_000
DEFAULT_SAMPLE_N = 20
DEFAULT_OPENING_NGRAM_TOP = 15
META_SCHEMA_VERSION = 1

# Phase 2′b local embeddings (Raider disk hard limit from docs/embed-disk-budget.md).
EMBED_SCHEMA_VERSION = 1
DEFAULT_EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBED_DISK_HARD_LIMIT = 74_815_478_169  # floor(0.80 * free) measured 2026-11-09
EMBED_RAM_GUIDANCE = 28_324_106_240  # floor(0.50 * MemAvailable); not pre-allocated
DEFAULT_EMBED_BATCH = 64
_EMBED_INDEX_CACHE: dict[str, Any] = {}
_EMBED_MODEL_CACHE: dict[str, Any] = {}

# Phase 2′c heuristic thread features (no LLM).
THREAD_FEATURES_SCHEMA_VERSION = 1
THREAD_FEATURES_META_KEY = "thread_features"
DEFAULT_THREAD_FEATURES_LIMIT = 20

# Named analysis indexes (same DB; survive FTS rebuild via copy).
ANALYSIS_INDEX_SCHEMA_VERSION = 1
ANALYSIS_INDEX_KINDS = frozenset({"message_ids", "thread_ids", "filter"})
DEFAULT_INDEX_LIST_LIMIT = 20
DEFAULT_INDEX_MEMBERS_LIMIT = 20
MAX_INDEX_MEMBER_WRITE = 50
MAX_INDEX_MEMBERS_TOTAL = 5_000
MAX_INDEX_COUNT = 100
MAX_MEMBER_ID_LEN = 256
MAX_INDEX_DESCRIPTION_LEN = 500
_INDEX_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
# Member ids are corpus ids / thread keys — never path-like.
_MEMBER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,255}$")
_FILTER_TIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?$"
)
_FILTER_ALLOWED_KEYS = frozenset({"sources", "source", "role", "since", "until"})
_ALLOWED_ROLES = frozenset({"human", "assistant", "self"})
KNOWN_SOURCES = frozenset(
    {"grok_chat", "x_post", "x_reply", "x_repost", "x_like", "x_note"}
)

CITE_FIELDS = ("id", "source", "created_at", "url", "thread_id", "role")

# Shorthand source groups for Phase 2′a discovery tools.
SOURCE_ALIASES: dict[str, tuple[str, ...]] = {
    "x": ("x_post", "x_reply", "x_repost", "x_like", "x_note"),
    "grok": ("grok_chat",),
}

# Crude register heuristics — approximate only; documented as such in tool output.
_CONTRACTION_RE = re.compile(r"\b\w+'\w+\b", re.UNICODE)
_WORD_RE = re.compile(r"[^\W_]+(?:'[^\W_]+)?", re.UNICODE)
_IMPERATIVE_VERBS = frozenset(
    {
        "add",
        "check",
        "close",
        "create",
        "delete",
        "do",
        "explain",
        "find",
        "fix",
        "get",
        "give",
        "help",
        "list",
        "look",
        "make",
        "open",
        "please",
        "remove",
        "run",
        "search",
        "send",
        "set",
        "show",
        "start",
        "stop",
        "tell",
        "try",
        "update",
        "use",
        "write",
    }
)
_THREAD_LENGTH_BUCKETS: tuple[tuple[str, int, int | None], ...] = (
    ("1", 1, 1),
    ("2-5", 2, 5),
    ("6-20", 6, 20),
    ("21+", 21, None),
)

# Approximate edit/refine + critique cues on human/self turns only.
_REFINE_CUE_RE = re.compile(
    r"\b("
    r"try again|rewrite|revis(?:e|ion)|rephrase|tweak|adjust|"
    r"edit(?:\s+this|\s+it)?|instead|make it|"
    r"change (?:the|this|it)|update (?:the|this|it)|"
    r"fix(?:\s+it|\s+this)?|rather|more like|less like|"
    r"shorter|longer|again but"
    r")\b",
    re.IGNORECASE,
)
_CRITIQUE_CUE_RE = re.compile(
    r"\b("
    r"critique|review this|what do you think|"
    r"is this (?:wrong|correct|right)|feedback|honest opinion|"
    r"roast|tear apart|spot (?:the|any)|any issues|what'?s wrong|"
    r"does this (?:make sense|work)|am i (?:crazy|wrong)|"
    r"be critical|brutal(?:ly)? honest"
    r")\b",
    re.IGNORECASE,
)
_CODE_MARK_RE = re.compile(
    r"(```|^\s{0,3}(def |class |function |import |from .+ import |"
    r"#include |package )|;\s*$|\{\s*$)",
    re.MULTILINE,
)
_TECH_MARK_RE = re.compile(
    r"\b("
    r"api|http|https|sql|json|python|typescript|javascript|docker|linux|"
    r"git|regex|algorithm|latency|throughput|gpu|cuda|systemd|oauth|jwt|"
    r"sqlite|postgres|kubernetes|webpack|react|css|html|tcp|udp|dns|"
    r"clang|rust|golang|nodejs|npm|pip|venv|ssh|nginx|cloudflare"
    r")\b",
    re.IGNORECASE,
)
_THREAD_FEATURES_DDL = """
CREATE TABLE IF NOT EXISTS thread_features (
    thread_id TEXT PRIMARY KEY,
    message_count INTEGER NOT NULL,
    human_count INTEGER NOT NULL,
    assistant_count INTEGER NOT NULL,
    self_count INTEGER NOT NULL,
    other_role_count INTEGER NOT NULL,
    human_assistant_ratio REAL,
    refine_cue_count INTEGER NOT NULL,
    critique_cue_count INTEGER NOT NULL,
    code_msg_count INTEGER NOT NULL,
    code_density REAL NOT NULL,
    tech_msg_count INTEGER NOT NULL,
    tech_density REAL NOT NULL,
    has_refine INTEGER NOT NULL,
    has_critique INTEGER NOT NULL,
    source_primary TEXT,
    first_created_at TEXT,
    last_created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_thread_features_len
    ON thread_features(message_count);
CREATE INDEX IF NOT EXISTS idx_thread_features_flags
    ON thread_features(has_refine, has_critique, code_density);
"""

_ANALYSIS_INDEX_DDL = """
CREATE TABLE IF NOT EXISTS analysis_indexes (
    name TEXT PRIMARY KEY,
    version INTEGER NOT NULL DEFAULT 1,
    kind TEXT NOT NULL,
    description TEXT,
    filter_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analysis_index_members (
    index_name TEXT NOT NULL,
    member_id TEXT NOT NULL,
    PRIMARY KEY (index_name, member_id),
    FOREIGN KEY (index_name) REFERENCES analysis_indexes(name) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_analysis_index_members_id
    ON analysis_index_members(member_id);
"""


@dataclass(frozen=True)
class InputFingerprint:
    """Content-addressed skip key for rebuild."""

    grok_path: str
    grok_size: int
    grok_mtime_ns: int
    x_path: str
    x_size: int
    x_mtime_ns: int
    schema_version: int = META_SCHEMA_VERSION

    def digest(self) -> str:
        payload = json.dumps(
            {
                "grok_path": self.grok_path,
                "grok_size": self.grok_size,
                "grok_mtime_ns": self.grok_mtime_ns,
                "x_path": self.x_path,
                "x_size": self.x_size,
                "x_mtime_ns": self.x_mtime_ns,
                "schema_version": self.schema_version,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "digest": self.digest(),
            "grok_path": self.grok_path,
            "grok_size": self.grok_size,
            "grok_mtime_ns": self.grok_mtime_ns,
            "x_path": self.x_path,
            "x_size": self.x_size,
            "x_mtime_ns": self.x_mtime_ns,
            "schema_version": self.schema_version,
        }


def fingerprint_inputs(grok: Path, x: Path) -> InputFingerprint:
    gs = grok.stat()
    xs = x.stat()
    return InputFingerprint(
        grok_path=str(grok.resolve()),
        grok_size=gs.st_size,
        grok_mtime_ns=gs.st_mtime_ns,
        x_path=str(x.resolve()),
        x_size=xs.st_size,
        x_mtime_ns=xs.st_mtime_ns,
    )


def connect(db_path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    db_path = Path(db_path)
    if readonly:
        if not db_path.exists():
            raise FileNotFoundError(f"index missing: {db_path}")
        uri = f"file:{db_path.resolve()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TABLE IF EXISTS records_fts;
        DROP TABLE IF EXISTS thread_features;
        DROP TABLE IF EXISTS records;
        DROP TABLE IF EXISTS map_meta;

        CREATE TABLE map_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE records (
            rowid INTEGER PRIMARY KEY,
            id TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL,
            created_at TEXT,
            url TEXT,
            thread_id TEXT NOT NULL,
            role TEXT,
            text TEXT NOT NULL,
            meta_json TEXT NOT NULL
        );

        CREATE INDEX idx_records_source ON records(source);
        CREATE INDEX idx_records_thread ON records(thread_id);
        CREATE INDEX idx_records_created ON records(created_at);
        CREATE INDEX idx_records_role ON records(role);

        CREATE VIRTUAL TABLE records_fts USING fts5(
            text,
            content='records',
            content_rowid='rowid',
            tokenize='porter unicode61'
        );
        """
    )
    conn.executescript(_THREAD_FEATURES_DDL)
    conn.executescript(_ANALYSIS_INDEX_DDL)


def _has_analysis_index_tables(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='analysis_indexes'"
    ).fetchone()
    return row is not None


def ensure_analysis_index_schema(conn: sqlite3.Connection) -> None:
    """Idempotent DDL for named analysis indexes (writable connections only)."""
    conn.executescript(_ANALYSIS_INDEX_DDL)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO map_meta(key, value) VALUES (?, ?)",
            ("analysis_index_schema", str(ANALYSIS_INDEX_SCHEMA_VERSION)),
        )
    except sqlite3.Error:
        # map_meta may be absent mid-rebuild; tables still created.
        pass


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _normalize_index_name(name: str | None) -> str:
    raw = (name or "").strip().lower()
    # Reject path-like / traversal attempts before charset check.
    if any(tok in raw for tok in ("/", "\\", "..", "\x00")):
        raise ValueError("index name must not contain path separators or '..'")
    if not raw or not _INDEX_NAME_RE.match(raw):
        raise ValueError(
            "index name must match ^[a-z][a-z0-9_-]{0,63}$ "
            "(lowercase letters, digits, _-)"
        )
    return raw


def _normalize_index_kind(kind: str | None) -> str:
    k = (kind or "").strip().lower()
    if k not in ANALYSIS_INDEX_KINDS:
        raise ValueError(
            "kind must be message_ids|thread_ids|filter "
            f"(got {kind!r})"
        )
    return k


def _normalize_description(description: str | None) -> str | None:
    if description is None:
        return None
    text = str(description)
    if len(text) > MAX_INDEX_DESCRIPTION_LEN:
        raise ValueError(
            f"description capped at {MAX_INDEX_DESCRIPTION_LEN} chars"
        )
    return text


def _normalize_member_id(raw: Any) -> str | None:
    mid = str(raw).strip()
    if not mid:
        return None
    if len(mid) > MAX_MEMBER_ID_LEN:
        raise ValueError(f"member id longer than {MAX_MEMBER_ID_LEN} chars")
    if any(tok in mid for tok in ("/", "\\", "..", "\x00", " ")):
        raise ValueError("member id must not contain spaces or path separators")
    if not _MEMBER_ID_RE.match(mid):
        raise ValueError(
            "member id must match ^[A-Za-z0-9][A-Za-z0-9._:+-]{0,255}$"
        )
    return mid


def _normalize_member_ids(ids: Sequence[str] | None) -> list[str]:
    if ids is None:
        return []
    if not isinstance(ids, (list, tuple)):
        raise ValueError("ids must be an array of strings")
    out: list[str] = []
    seen: set[str] = set()
    for raw in ids:
        mid = _normalize_member_id(raw)
        if mid is None or mid in seen:
            continue
        seen.add(mid)
        out.append(mid)
    if len(out) > MAX_INDEX_MEMBER_WRITE:
        raise ValueError(
            f"ids capped at {MAX_INDEX_MEMBER_WRITE} per call "
            f"(got {len(out)})"
        )
    return out


def _validate_filter_time(label: str, value: str) -> str:
    text = str(value).strip()
    if not _FILTER_TIME_RE.match(text):
        raise ValueError(
            f"{label} must be ISO-8601 date or datetime "
            "(YYYY-MM-DD or YYYY-MM-DDTHH:MM[:SS][Z|±HH:MM])"
        )
    return text


def _validate_filter_sources(src: Any) -> list[str]:
    srcs = _parse_sources(src)
    if not srcs:
        raise ValueError("filter sources resolved empty")
    bad = [s for s in srcs if s not in KNOWN_SOURCES]
    if bad:
        raise ValueError(
            "filter sources must be known map sources or x|grok aliases; "
            f"rejected: {bad}"
        )
    return srcs


def _filter_expr_dict(
    *,
    sources: str | Sequence[str] | None = None,
    role: str | None = None,
    since: str | None = None,
    until: str | None = None,
    filter_obj: dict[str, Any] | str | None = None,
) -> dict[str, Any] | None:
    """
    Normalize a saved filter expression (sources/role/since/until only).

    Strict allowlist — never accepts free-form SQL / expressions. Values are
    always bound as parameters later (no string concat into SQL).
    """
    base: dict[str, Any] = {}
    if filter_obj is not None:
        if isinstance(filter_obj, str):
            if len(filter_obj) > 2_000:
                raise ValueError("filter JSON too large")
            try:
                parsed = json.loads(filter_obj)
            except json.JSONDecodeError as exc:
                raise ValueError(f"filter must be JSON object: {exc}") from exc
        else:
            parsed = filter_obj
        if not isinstance(parsed, dict):
            raise ValueError("filter must be a JSON object")
        unknown = set(parsed.keys()) - _FILTER_ALLOWED_KEYS
        if unknown:
            raise ValueError(
                "filter allows only sources/source/role/since/until; "
                f"rejected keys: {sorted(unknown)}"
            )
        for key in _FILTER_ALLOWED_KEYS:
            if key in parsed and parsed[key] is not None:
                base[key] = parsed[key]
    if sources is not None:
        base["sources"] = sources
    if role is not None:
        base["role"] = role
    if since is not None:
        base["since"] = since
    if until is not None:
        base["until"] = until
    out: dict[str, Any] = {}
    src = base.get("sources", base.get("source"))
    if src is not None:
        out["sources"] = _validate_filter_sources(src)
    if "role" in base:
        role_val = base["role"]
        if role_val == "" or role_val is None:
            out["role"] = None
        else:
            role_s = str(role_val).strip()
            if role_s not in _ALLOWED_ROLES:
                raise ValueError(
                    "filter role must be human|assistant|self "
                    f"(got {role_val!r})"
                )
            out["role"] = role_s
    if base.get("since"):
        out["since"] = _validate_filter_time("since", str(base["since"]))
    if base.get("until"):
        out["until"] = _validate_filter_time("until", str(base["until"]))
    return out or None


def _copy_analysis_indexes(
    src_db: Path, dest_conn: sqlite3.Connection
) -> int:
    """Copy analysis index tables from an existing DB into dest (rebuild)."""
    src_db = Path(src_db)
    if not src_db.exists():
        return 0
    ensure_analysis_index_schema(dest_conn)
    try:
        with connect(src_db, readonly=True) as src:
            tables = {
                r["name"]
                for r in src.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "analysis_indexes" not in tables:
                return 0
            rows = src.execute(
                """
                SELECT name, version, kind, description, filter_json,
                       created_at, updated_at
                FROM analysis_indexes
                """
            ).fetchall()
            if not rows:
                return 0
            dest_conn.executemany(
                """
                INSERT OR REPLACE INTO analysis_indexes
                    (name, version, kind, description, filter_json,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        r["name"],
                        int(r["version"]),
                        r["kind"],
                        r["description"],
                        r["filter_json"],
                        r["created_at"],
                        r["updated_at"],
                    )
                    for r in rows
                ],
            )
            if "analysis_index_members" in tables:
                members = src.execute(
                    "SELECT index_name, member_id FROM analysis_index_members"
                ).fetchall()
                if members:
                    dest_conn.executemany(
                        """
                        INSERT OR IGNORE INTO analysis_index_members
                            (index_name, member_id)
                        VALUES (?, ?)
                        """,
                        [(m["index_name"], m["member_id"]) for m in members],
                    )
            return len(rows)
    except sqlite3.Error:
        return 0


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSON: {exc}") from exc
            if not isinstance(obj, dict):
                raise ValueError(f"{path}:{line_no}: expected object")
            yield obj


def _normalize_record(obj: dict[str, Any]) -> tuple[Any, ...]:
    rid = obj.get("id")
    source = obj.get("source")
    thread_id = obj.get("thread_id")
    text = obj.get("text")
    if not isinstance(rid, str) or not rid:
        raise ValueError("missing id")
    if not isinstance(source, str) or not source:
        raise ValueError(f"{rid}: missing source")
    if not isinstance(thread_id, str) or not thread_id:
        raise ValueError(f"{rid}: missing thread_id")
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    created_at = obj.get("created_at")
    if created_at is not None and not isinstance(created_at, str):
        created_at = str(created_at)
    url = obj.get("url")
    if url is not None and not isinstance(url, str):
        url = str(url)
    role = obj.get("role")
    if role is not None and not isinstance(role, str):
        role = str(role)
    meta = obj.get("meta")
    if meta is None:
        meta = {}
    meta_json = json.dumps(meta, ensure_ascii=False, separators=(",", ":"))
    return (rid, source, created_at, url, thread_id, role, text, meta_json)


def build_index(
    *,
    grok: Path = DEFAULT_GROK,
    x: Path = DEFAULT_X,
    db_path: Path = DEFAULT_DB,
    stats_path: Path | None = DEFAULT_STATS,
    force: bool = False,
    progress_every: int = 5000,
) -> dict[str, Any]:
    """Build (or skip) als_map.sqlite. Returns stats dict."""
    grok = Path(grok)
    x = Path(x)
    db_path = Path(db_path)
    if not grok.is_file():
        raise FileNotFoundError(f"missing grok JSONL: {grok}")
    if not x.is_file():
        raise FileNotFoundError(f"missing x JSONL: {x}")

    fp = fingerprint_inputs(grok, x)
    if db_path.exists() and not force:
        try:
            with connect(db_path, readonly=True) as conn:
                row = conn.execute(
                    "SELECT value FROM map_meta WHERE key='fingerprint'"
                ).fetchone()
                count_row = conn.execute("SELECT COUNT(*) AS n FROM records").fetchone()
            if row is not None:
                stored = json.loads(row["value"])
                if stored.get("digest") == fp.digest():
                    stats = {
                        "skipped": True,
                        "reason": "fingerprint_match",
                        "db": str(db_path),
                        "records": int(count_row["n"]) if count_row else 0,
                        "fingerprint": fp.to_dict(),
                        "built_at": None,
                    }
                    if stats_path is not None and stats_path.exists():
                        try:
                            prior = json.loads(stats_path.read_text(encoding="utf-8"))
                            for k in ("by_source", "by_role", "elapsed_s"):
                                if k in prior:
                                    stats[k] = prior[k]
                        except (OSError, json.JSONDecodeError):
                            pass
                    return stats
        except (sqlite3.Error, json.JSONDecodeError, KeyError, TypeError):
            pass  # rebuild

    t0 = time.perf_counter()
    tmp_path = db_path.with_suffix(db_path.suffix + ".tmp")
    if tmp_path.exists():
        tmp_path.unlink()

    by_source: dict[str, int] = {}
    by_role: dict[str, int] = {}
    total = 0
    batch: list[tuple[Any, ...]] = []

    def flush(conn: sqlite3.Connection) -> None:
        nonlocal batch
        if not batch:
            return
        conn.executemany(
            """
            INSERT INTO records (id, source, created_at, url, thread_id, role, text, meta_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            batch,
        )
        batch = []

    with connect(tmp_path) as conn:
        _init_schema(conn)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")

        for label, path in (("grok", grok), ("x", x)):
            for obj in _iter_jsonl(path):
                try:
                    row = _normalize_record(obj)
                except ValueError as exc:
                    raise ValueError(f"{path}: {exc}") from exc
                source = row[1]
                role = row[5] if row[5] is not None else "null"
                by_source[source] = by_source.get(source, 0) + 1
                by_role[role] = by_role.get(role, 0) + 1
                batch.append(row)
                total += 1
                if len(batch) >= 1000:
                    flush(conn)
                if progress_every and total % progress_every == 0:
                    print(
                        f"  … {total} records ({label})",
                        file=sys.stderr,
                        flush=True,
                    )
            flush(conn)

        # Populate external-content FTS from records
        conn.execute(
            "INSERT INTO records_fts(rowid, text) SELECT rowid, text FROM records"
        )
        conn.execute(
            "INSERT INTO map_meta(key, value) VALUES (?, ?)",
            ("fingerprint", json.dumps(fp.to_dict(), separators=(",", ":"))),
        )
        built_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        conn.execute(
            "INSERT INTO map_meta(key, value) VALUES (?, ?)",
            ("built_at", built_at),
        )
        conn.execute(
            "INSERT INTO map_meta(key, value) VALUES (?, ?)",
            ("record_count", str(total)),
        )
        # Phase 2′c heuristics — same fingerprint as FTS rows.
        build_thread_features(conn, force=True, records_fingerprint=fp.digest())
        # Named analysis indexes: create empty tables, then copy from prior DB.
        ensure_analysis_index_schema(conn)
        if db_path.exists():
            _copy_analysis_indexes(db_path, conn)
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    os.replace(tmp_path, db_path)
    # Clean WAL/SHM left beside tmp if any remain under final name
    for suffix in ("-wal", "-shm"):
        side = Path(str(tmp_path) + suffix)
        if side.exists():
            side.unlink()

    elapsed = round(time.perf_counter() - t0, 3)
    stats: dict[str, Any] = {
        "skipped": False,
        "db": str(db_path),
        "records": total,
        "by_source": dict(sorted(by_source.items())),
        "by_role": dict(sorted(by_role.items())),
        "fingerprint": fp.to_dict(),
        "built_at": built_at,
        "elapsed_s": elapsed,
        "inputs": {"grok": str(grok), "x": str(x)},
    }
    if stats_path is not None:
        stats_path = Path(stats_path)
        stats_path.parent.mkdir(parents=True, exist_ok=True)
        stats_path.write_text(
            json.dumps(stats, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        stats["stats_path"] = str(stats_path)
    return stats


def _clamp_limit(limit: int | None, default: int) -> int:
    if limit is None:
        return default
    return max(1, min(int(limit), MAX_LIMIT))


def _clamp_chars(n: int | None, default: int, hard_max: int) -> int:
    if n is None:
        return default
    return max(0, min(int(n), hard_max))


def _parse_sources(sources: str | Sequence[str] | None) -> list[str] | None:
    if sources is None:
        return None
    if isinstance(sources, str):
        parts = [p.strip() for p in sources.split(",") if p.strip()]
    else:
        parts = [str(p).strip() for p in sources if str(p).strip()]
    if not parts:
        return None
    expanded: list[str] = []
    seen: set[str] = set()
    for part in parts:
        key = part.lower()
        aliases = SOURCE_ALIASES.get(key)
        values = aliases if aliases is not None else (part,)
        for value in values:
            if value not in seen:
                seen.add(value)
                expanded.append(value)
    return expanded or None


def _filter_where(
    *,
    sources: str | Sequence[str] | None = None,
    role: str | None = None,
    since: str | None = None,
    until: str | None = None,
    table_alias: str = "",
) -> tuple[str, list[Any]]:
    """Shared WHERE builder for browse / sample / style filters."""
    prefix = f"{table_alias}." if table_alias else ""
    where = ["1=1"]
    params: list[Any] = []
    srcs = _parse_sources(sources)
    if srcs:
        where.append(f"{prefix}source IN ({','.join('?' * len(srcs))})")
        params.extend(srcs)
    if role is not None:
        where.append(f"{prefix}role IS ?" if role == "" else f"{prefix}role = ?")
        params.append(None if role == "" else role)
    if since:
        where.append(f"{prefix}created_at IS NOT NULL AND {prefix}created_at >= ?")
        params.append(since)
    if until:
        where.append(f"{prefix}created_at IS NOT NULL AND {prefix}created_at < ?")
        params.append(until)
    return " AND ".join(where), params


def _load_analysis_index(
    conn: sqlite3.Connection, name: str
) -> sqlite3.Row | None:
    if not _has_analysis_index_tables(conn):
        return None
    return conn.execute(
        """
        SELECT name, version, kind, description, filter_json,
               created_at, updated_at
        FROM analysis_indexes WHERE name = ?
        """,
        (name,),
    ).fetchone()


def _index_scope_clause(
    conn: sqlite3.Connection,
    index: str | None,
    *,
    table_alias: str = "",
) -> tuple[str | None, list[Any], dict[str, Any] | None]:
    """
    Optional named-index scope → (sql_fragment, params, index_meta).

    message_ids → id IN members; thread_ids → thread_id IN members;
    filter → AND saved filter expression. Returns (None, [], None) when unset.
    """
    if index is None or str(index).strip() == "":
        return None, [], None
    name = _normalize_index_name(str(index))
    if not _has_analysis_index_tables(conn):
        raise ValueError(
            f"unknown analysis index: {name} "
            "(analysis index tables missing — create one via als_index_create)"
        )
    row = _load_analysis_index(conn, name)
    if row is None:
        raise ValueError(f"unknown analysis index: {name}")
    prefix = f"{table_alias}." if table_alias else ""
    kind = str(row["kind"])
    meta = {
        "name": row["name"],
        "version": int(row["version"]),
        "kind": kind,
    }
    if kind == "message_ids":
        return (
            f"{prefix}id IN (SELECT member_id FROM analysis_index_members "
            f"WHERE index_name = ?)",
            [name],
            meta,
        )
    if kind == "thread_ids":
        return (
            f"{prefix}thread_id IN (SELECT member_id FROM analysis_index_members "
            f"WHERE index_name = ?)",
            [name],
            meta,
        )
    # filter kind — evaluate saved expression at query time
    filt = _filter_expr_dict(filter_obj=row["filter_json"])
    if not filt:
        return "1=0", [], meta  # empty filter matches nothing
    where_sql, params = _filter_where(
        sources=filt.get("sources"),
        role=filt.get("role"),
        since=filt.get("since"),
        until=filt.get("until"),
        table_alias=table_alias,
    )
    return where_sql, params, meta


def _apply_index_scope(
    conn: sqlite3.Connection,
    where: list[str],
    params: list[Any],
    index: str | None,
    *,
    table_alias: str = "",
) -> dict[str, Any] | None:
    clause, extra, meta = _index_scope_clause(
        conn, index, table_alias=table_alias
    )
    if clause:
        where.append(f"({clause})")
        params.extend(extra)
    return meta


def _median(sorted_vals: Sequence[float]) -> float | None:
    n = len(sorted_vals)
    if n == 0:
        return None
    mid = n // 2
    if n % 2:
        return float(sorted_vals[mid])
    return (float(sorted_vals[mid - 1]) + float(sorted_vals[mid])) / 2.0


def _mean(vals: Sequence[float]) -> float | None:
    if not vals:
        return None
    return sum(vals) / len(vals)


def _tokenize(text: str) -> list[str]:
    return [m.group(0).lower() for m in _WORD_RE.finditer(text or "")]


def _thread_length_bucket(n: int) -> str:
    for label, lo, hi in _THREAD_LENGTH_BUCKETS:
        if n < lo:
            continue
        if hi is None or n <= hi:
            return label
    return "21+"


def _opening_ngrams(tokens: Sequence[str], n: int) -> str | None:
    if len(tokens) < n:
        return None
    return " ".join(tokens[:n])


def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
    if max_chars <= 0:
        return ("", True) if text else ("", False)
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def _row_cite(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    get = row.__getitem__
    return {
        "id": get("id"),
        "source": get("source"),
        "created_at": get("created_at"),
        "url": get("url"),
        "thread_id": get("thread_id"),
        "role": get("role"),
    }


def _fit_response(payload: dict[str, Any], max_bytes: int = MAX_RESPONSE_BYTES) -> dict[str, Any]:
    """Ensure JSON payload stays under max_bytes by trimming list fields."""
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(raw.encode("utf-8")) <= max_bytes:
        payload["truncated_response"] = False
        return payload

    for key in ("hits", "records", "items"):
        if key not in payload or not isinstance(payload[key], list):
            continue
        items = payload[key]
        lo, hi = 0, len(items)
        best = 0
        while lo <= hi:
            mid = (lo + hi) // 2
            trial = dict(payload)
            trial[key] = items[:mid]
            trial["truncated_response"] = True
            trial["returned"] = mid
            enc = json.dumps(trial, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
            if len(enc) <= max_bytes:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        payload[key] = items[:best]
        payload["truncated_response"] = True
        payload["returned"] = best
        return payload

    # Fallback: drop heavy fields
    payload["truncated_response"] = True
    if "text" in payload:
        text, _ = _truncate(str(payload["text"]), 1000)
        payload["text"] = text
        payload["text_truncated"] = True
    return payload


def _meta_from_row(row: sqlite3.Row, include_meta: bool) -> dict[str, Any] | None:
    if not include_meta:
        return None
    try:
        return json.loads(row["meta_json"])
    except (TypeError, json.JSONDecodeError):
        return None


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    sources: str | Sequence[str] | None = None,
    role: str | None = None,
    since: str | None = None,
    until: str | None = None,
    index: str | None = None,
    limit: int | None = None,
    offset: int = 0,
    max_chars: int | None = None,
    full_text: bool = False,
    include_meta: bool = False,
) -> dict[str, Any]:
    """FTS search. Default: cite fields + snippet only."""
    q = (query or "").strip()
    if not q:
        raise ValueError("query is required")
    lim = _clamp_limit(limit, DEFAULT_SEARCH_LIMIT)
    off = max(0, int(offset))
    srcs = _parse_sources(sources)
    snippet_chars = _clamp_chars(
        max_chars,
        DEFAULT_FULL_CHARS if full_text else DEFAULT_SNIPPET_CHARS,
        MAX_FULL_CHARS if full_text else MAX_SNIPPET_CHARS,
    )

    where = ["records_fts MATCH ?"]
    params: list[Any] = [q]
    if srcs:
        where.append(f"r.source IN ({','.join('?' * len(srcs))})")
        params.extend(srcs)
    if role is not None:
        where.append("r.role IS ?" if role == "" else "r.role = ?")
        params.append(None if role == "" else role)
    if since:
        where.append("r.created_at IS NOT NULL AND r.created_at >= ?")
        params.append(since)
    if until:
        where.append("r.created_at IS NOT NULL AND r.created_at < ?")
        params.append(until)
    index_meta = _apply_index_scope(conn, where, params, index, table_alias="r")

    sql = f"""
        SELECT r.id, r.source, r.created_at, r.url, r.thread_id, r.role, r.text, r.meta_json,
               bm25(records_fts) AS score,
               snippet(records_fts, 0, '', '', '…', 32) AS fts_snippet
        FROM records_fts
        JOIN records r ON r.rowid = records_fts.rowid
        WHERE {' AND '.join(where)}
        ORDER BY score
        LIMIT ? OFFSET ?
    """
    params.extend([lim, off])
    rows = conn.execute(sql, params).fetchall()

    hits: list[dict[str, Any]] = []
    for row in rows:
        item = _row_cite(row)
        item["score"] = round(float(row["score"]), 4)
        if full_text:
            text, trunc = _truncate(row["text"] or "", snippet_chars)
            item["text"] = text
            item["text_truncated"] = trunc
        else:
            # Prefer FTS snippet; fall back to head of text
            snip = (row["fts_snippet"] or "").strip()
            if not snip:
                snip, _ = _truncate(row["text"] or "", snippet_chars)
            else:
                snip, _ = _truncate(snip, snippet_chars)
            item["snippet"] = snip
        meta = _meta_from_row(row, include_meta)
        if meta is not None:
            item["meta"] = meta
        hits.append(item)

    out: dict[str, Any] = {
        "op": "search",
        "query": q,
        "limit": lim,
        "offset": off,
        "returned": len(hits),
        "full_text": full_text,
        "hits": hits,
    }
    if index_meta is not None:
        out["index"] = index_meta
    return _fit_response(out)


def get_one(
    conn: sqlite3.Connection,
    record_id: str,
    *,
    max_chars: int | None = None,
    include_meta: bool = True,
) -> dict[str, Any]:
    max_c = _clamp_chars(max_chars, DEFAULT_FULL_CHARS, MAX_FULL_CHARS)
    row = conn.execute(
        """
        SELECT id, source, created_at, url, thread_id, role, text, meta_json
        FROM records WHERE id = ?
        """,
        (record_id,),
    ).fetchone()
    if row is None:
        return _fit_response(
            {"op": "get", "id": record_id, "found": False, "record": None}
        )
    text, trunc = _truncate(row["text"] or "", max_c)
    rec = _row_cite(row)
    rec["text"] = text
    rec["text_truncated"] = trunc
    meta = _meta_from_row(row, include_meta)
    if meta is not None:
        rec["meta"] = meta
    return _fit_response({"op": "get", "id": record_id, "found": True, "record": rec})


def get_many(
    conn: sqlite3.Connection,
    ids: Sequence[str],
    *,
    max_chars: int | None = None,
    include_meta: bool = False,
) -> dict[str, Any]:
    clean = [str(i) for i in ids if str(i).strip()]
    if len(clean) > MAX_GET_MANY:
        clean = clean[:MAX_GET_MANY]
    max_c = _clamp_chars(max_chars, DEFAULT_FULL_CHARS, MAX_FULL_CHARS)
    if not clean:
        return _fit_response(
            {"op": "get_many", "requested": 0, "returned": 0, "records": []}
        )
    placeholders = ",".join("?" * len(clean))
    rows = conn.execute(
        f"""
        SELECT id, source, created_at, url, thread_id, role, text, meta_json
        FROM records WHERE id IN ({placeholders})
        """,
        clean,
    ).fetchall()
    by_id = {r["id"]: r for r in rows}
    records: list[dict[str, Any]] = []
    missing: list[str] = []
    for rid in clean:
        row = by_id.get(rid)
        if row is None:
            missing.append(rid)
            continue
        text, trunc = _truncate(row["text"] or "", max_c)
        rec = _row_cite(row)
        rec["text"] = text
        rec["text_truncated"] = trunc
        meta = _meta_from_row(row, include_meta)
        if meta is not None:
            rec["meta"] = meta
        records.append(rec)
    return _fit_response(
        {
            "op": "get_many",
            "requested": len(clean),
            "returned": len(records),
            "missing": missing,
            "records": records,
        }
    )


def thread(
    conn: sqlite3.Connection,
    thread_id: str,
    *,
    limit: int | None = None,
    offset: int = 0,
    max_chars: int | None = None,
    include_meta: bool = False,
) -> dict[str, Any]:
    lim = _clamp_limit(limit, DEFAULT_THREAD_LIMIT)
    off = max(0, int(offset))
    max_c = _clamp_chars(max_chars, DEFAULT_FULL_CHARS, MAX_FULL_CHARS)
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM records WHERE thread_id = ?",
        (thread_id,),
    ).fetchone()["n"]
    rows = conn.execute(
        """
        SELECT id, source, created_at, url, thread_id, role, text, meta_json
        FROM records
        WHERE thread_id = ?
        ORDER BY
          CASE WHEN created_at IS NULL THEN 1 ELSE 0 END,
          created_at ASC,
          id ASC
        LIMIT ? OFFSET ?
        """,
        (thread_id, lim, off),
    ).fetchall()
    records: list[dict[str, Any]] = []
    for row in rows:
        text, trunc = _truncate(row["text"] or "", max_c)
        rec = _row_cite(row)
        rec["text"] = text
        rec["text_truncated"] = trunc
        meta = _meta_from_row(row, include_meta)
        if meta is not None:
            rec["meta"] = meta
        records.append(rec)
    return _fit_response(
        {
            "op": "thread",
            "thread_id": thread_id,
            "total": int(total),
            "limit": lim,
            "offset": off,
            "returned": len(records),
            "records": records,
        }
    )


def browse(
    conn: sqlite3.Connection,
    *,
    since: str | None = None,
    until: str | None = None,
    sources: str | Sequence[str] | None = None,
    role: str | None = None,
    index: str | None = None,
    limit: int | None = None,
    offset: int = 0,
    max_chars: int | None = None,
    include_meta: bool = False,
) -> dict[str, Any]:
    """Keywordless chronological window."""
    lim = _clamp_limit(limit, DEFAULT_BROWSE_LIMIT)
    off = max(0, int(offset))
    max_c = _clamp_chars(
        max_chars, DEFAULT_SNIPPET_CHARS, MAX_SNIPPET_CHARS
    )
    srcs = _parse_sources(sources)
    where = ["1=1"]
    params: list[Any] = []
    if since:
        where.append("created_at IS NOT NULL AND created_at >= ?")
        params.append(since)
    if until:
        where.append("created_at IS NOT NULL AND created_at < ?")
        params.append(until)
    if srcs:
        where.append(f"source IN ({','.join('?' * len(srcs))})")
        params.extend(srcs)
    if role is not None:
        where.append("role IS ?" if role == "" else "role = ?")
        params.append(None if role == "" else role)
    index_meta = _apply_index_scope(conn, where, params, index)

    where_sql = " AND ".join(where)
    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM records WHERE {where_sql}",
        params,
    ).fetchone()["n"]
    rows = conn.execute(
        f"""
        SELECT id, source, created_at, url, thread_id, role, text, meta_json
        FROM records
        WHERE {where_sql}
        ORDER BY
          CASE WHEN created_at IS NULL THEN 1 ELSE 0 END,
          created_at ASC,
          id ASC
        LIMIT ? OFFSET ?
        """,
        [*params, lim, off],
    ).fetchall()
    hits: list[dict[str, Any]] = []
    for row in rows:
        item = _row_cite(row)
        text, trunc = _truncate(row["text"] or "", max_c)
        item["snippet"] = text
        item["text_truncated"] = trunc
        meta = _meta_from_row(row, include_meta)
        if meta is not None:
            item["meta"] = meta
        hits.append(item)
    out: dict[str, Any] = {
        "op": "browse",
        "since": since,
        "until": until,
        "total": int(total),
        "limit": lim,
        "offset": off,
        "returned": len(hits),
        "hits": hits,
    }
    if index_meta is not None:
        out["index"] = index_meta
    return _fit_response(out)


def stats(
    conn: sqlite3.Connection,
    *,
    breakdown: str | None = "source",
    index: str | None = None,
    sources: str | Sequence[str] | None = None,
) -> dict[str, Any]:
    where = ["1=1"]
    params: list[Any] = []
    srcs = _parse_sources(sources)
    if srcs:
        where.append(f"source IN ({','.join('?' * len(srcs))})")
        params.extend(srcs)
    index_meta = _apply_index_scope(conn, where, params, index)
    where_sql = " AND ".join(where)

    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM records WHERE {where_sql}",
        params,
    ).fetchone()["n"]
    meta_rows = {
        r["key"]: r["value"]
        for r in conn.execute("SELECT key, value FROM map_meta").fetchall()
    }
    out: dict[str, Any] = {
        "op": "stats",
        "records": int(total),
        "built_at": meta_rows.get("built_at"),
        "fingerprint": None,
    }
    if sources is not None:
        out["sources"] = sources
    if index_meta is not None:
        out["index"] = index_meta
    if "fingerprint" in meta_rows:
        try:
            out["fingerprint"] = json.loads(meta_rows["fingerprint"])
        except json.JSONDecodeError:
            out["fingerprint"] = meta_rows["fingerprint"]

    bd = (breakdown or "source").lower()
    if bd in ("source", "both"):
        rows = conn.execute(
            f"""
            SELECT source, COUNT(*) AS n FROM records
            WHERE {where_sql}
            GROUP BY source ORDER BY n DESC
            """,
            params,
        ).fetchall()
        out["by_source"] = {r["source"]: int(r["n"]) for r in rows}
    if bd in ("role", "both"):
        rows = conn.execute(
            f"""
            SELECT COALESCE(role, 'null') AS role, COUNT(*) AS n
            FROM records
            WHERE {where_sql}
            GROUP BY role ORDER BY n DESC
            """,
            params,
        ).fetchall()
        out["by_role"] = {r["role"]: int(r["n"]) for r in rows}
    if bd == "month":
        rows = conn.execute(
            f"""
            SELECT substr(created_at, 1, 7) AS month, COUNT(*) AS n
            FROM records
            WHERE {where_sql}
              AND created_at IS NOT NULL AND length(created_at) >= 7
            GROUP BY month
            ORDER BY month
            """,
            params,
        ).fetchall()
        out["by_month"] = {r["month"]: int(r["n"]) for r in rows}
    elif bd not in ("source", "role", "both", "month", "none", ""):
        out["warning"] = f"unknown breakdown {breakdown!r}; used source"
        rows = conn.execute(
            f"""
            SELECT source, COUNT(*) AS n FROM records
            WHERE {where_sql}
            GROUP BY source ORDER BY n DESC
            """,
            params,
        ).fetchall()
        out["by_source"] = {r["source"]: int(r["n"]) for r in rows}

    return _fit_response(out)


def sample(
    conn: sqlite3.Connection,
    *,
    n: int | None = None,
    role: str | None = None,
    source: str | Sequence[str] | None = None,
    sources: str | Sequence[str] | None = None,
    index: str | None = None,
    stratify_by: str | None = "none",
    seed: int | None = None,
    max_chars: int | None = None,
    min_thread_length: int | None = None,
    has_refine: bool | None = None,
    has_critique: bool | None = None,
    min_code_density: float | None = None,
) -> dict[str, Any]:
    """Prior-free stratified random sample (snippet-shaped like search)."""
    lim = _clamp_limit(n, DEFAULT_SAMPLE_N)
    strat = (stratify_by or "none").strip().lower()
    if strat not in {"none", "time", "thread_length"}:
        raise ValueError("stratify_by must be none|time|thread_length")
    max_c = _clamp_chars(max_chars, DEFAULT_SNIPPET_CHARS, MAX_SNIPPET_CHARS)
    src_arg = source if source is not None else sources
    where_sql, params = _filter_where(sources=src_arg, role=role)
    where_parts = [where_sql]
    index_meta = _apply_index_scope(conn, where_parts, params, index)
    where_sql = " AND ".join(where_parts)
    feature_filters = {
        "min_thread_length": min_thread_length,
        "has_refine": has_refine,
        "has_critique": has_critique,
        "min_code_density": min_code_density,
    }
    thread_allow = _matching_thread_ids(
        conn,
        min_thread_length=min_thread_length,
        has_refine=has_refine,
        has_critique=has_critique,
        min_code_density=min_code_density,
    )

    rows = conn.execute(
        f"""
        SELECT id, source, created_at, url, thread_id, role, text
        FROM records
        WHERE {where_sql}
        """,
        params,
    ).fetchall()
    if thread_allow is not None:
        rows = [r for r in rows if r["thread_id"] in thread_allow]
    total = len(rows)
    rng = random.Random(seed)

    selected: list[sqlite3.Row] = []
    buckets_meta: dict[str, Any] = {}

    if total == 0 or lim == 0:
        selected = []
    elif strat == "none":
        selected = rng.sample(list(rows), k=min(lim, total))
        buckets_meta = {"none": {"eligible": total, "picked": len(selected)}}
    else:
        groups: dict[str, list[sqlite3.Row]] = {}
        if strat == "time":
            for row in rows:
                created = row["created_at"]
                if isinstance(created, str) and len(created) >= 7:
                    key = created[:7]
                else:
                    key = "unknown"
                groups.setdefault(key, []).append(row)
        else:  # thread_length
            if thread_allow is None:
                length_rows = conn.execute(
                    f"""
                    SELECT r.thread_id AS thread_id, COUNT(*) AS n
                    FROM records r
                    WHERE r.thread_id IN (
                        SELECT DISTINCT thread_id FROM records WHERE {where_sql}
                    )
                    GROUP BY r.thread_id
                    """,
                    params,
                ).fetchall()
                lengths = {r["thread_id"]: int(r["n"]) for r in length_rows}
            else:
                lengths = {
                    tid: int(feat["message_count"])
                    for tid, feat in _thread_features_map(conn).items()
                    if tid in thread_allow
                }
            for row in rows:
                key = _thread_length_bucket(lengths.get(row["thread_id"], 1))
                groups.setdefault(key, []).append(row)

        keys = sorted(groups.keys())
        if not keys:
            selected = []
        else:
            # Equal quotas across non-empty buckets; leftover goes round-robin.
            base = lim // len(keys)
            rem = lim % len(keys)
            quota = {k: base + (1 if i < rem else 0) for i, k in enumerate(keys)}
            leftover_pool: list[sqlite3.Row] = []
            for key in keys:
                pool = list(groups[key])
                rng.shuffle(pool)
                take = min(quota[key], len(pool))
                picked = pool[:take]
                selected.extend(picked)
                leftover_pool.extend(pool[take:])
                buckets_meta[key] = {
                    "eligible": len(groups[key]),
                    "quota": quota[key],
                    "picked": take,
                }
            need = lim - len(selected)
            if need > 0 and leftover_pool:
                rng.shuffle(leftover_pool)
                extra = leftover_pool[:need]
                selected.extend(extra)
                buckets_meta["_fill"] = {"picked": len(extra)}

    # Stable order within response: by created_at then id (sample already chosen).
    selected.sort(
        key=lambda r: (
            1 if r["created_at"] is None else 0,
            r["created_at"] or "",
            r["id"],
        )
    )

    hits: list[dict[str, Any]] = []
    for row in selected:
        item = _row_cite(row)
        text, trunc = _truncate(row["text"] or "", max_c)
        item["snippet"] = text
        item["text_truncated"] = trunc
        hits.append(item)

    out_sample: dict[str, Any] = {
        "op": "sample",
        "n": lim,
        "role": role,
        "source": src_arg,
        "stratify_by": strat,
        "seed": seed,
        "feature_filters": feature_filters,
        "eligible": total,
        "returned": len(hits),
        "buckets": buckets_meta,
        "hits": hits,
    }
    if index_meta is not None:
        out_sample["index"] = index_meta
    return _fit_response(out_sample)


def style_stats(
    conn: sqlite3.Connection,
    *,
    source: str | Sequence[str] | None = None,
    sources: str | Sequence[str] | None = None,
    role: str | None = None,
    since: str | None = None,
    until: str | None = None,
    index: str | None = None,
    opening_ngrams_top: int = DEFAULT_OPENING_NGRAM_TOP,
    opening_role: str | None = None,
) -> dict[str, Any]:
    """Cheap deterministic style aggregates (no full-text dump)."""
    src_arg = source if source is not None else sources
    where_sql, params = _filter_where(
        sources=src_arg, role=role, since=since, until=until
    )
    where_parts = [where_sql]
    index_meta = _apply_index_scope(conn, where_parts, params, index)
    where_sql = " AND ".join(where_parts)
    top_n = max(0, min(int(opening_ngrams_top), 50))
    # Default: opening n-grams from human turns; if caller already filters a
    # role (e.g. X `self`), use that slice instead of empty n-grams.
    ngram_role = opening_role if opening_role is not None else (role if role else "human")

    # First message per thread (full corpus) — pure Python to avoid window-fn
    # crashes seen with this SQLite build over the FTS-backed DB.
    best_key: dict[str, tuple[int, str, str]] = {}
    for tid, rid, created in conn.execute(
        "SELECT thread_id, id, created_at FROM records"
    ):
        key = (1 if created is None else 0, created or "", rid)
        prev = best_key.get(tid)
        if prev is None or key < prev:
            best_key[tid] = key
    first_ids = {key[2] for key in best_key.values()}

    rows = conn.execute(
        f"""
        SELECT id, source, role, thread_id, text
        FROM records
        WHERE {where_sql}
        """,
        params,
    ).fetchall()

    count = len(rows)
    char_lens: list[float] = []
    word_lens: list[float] = []
    qmark_chars = 0
    total_chars = 0
    followups = 0
    contractions = 0
    total_words = 0
    imperative_hits = 0
    bigrams: Counter[str] = Counter()
    trigrams: Counter[str] = Counter()
    ngram_docs = 0

    for row in rows:
        text = row["text"] or ""
        char_lens.append(float(len(text)))
        tokens = _tokenize(text)
        word_lens.append(float(len(tokens)))
        total_chars += len(text)
        qmark_chars += text.count("?")
        if row["id"] not in first_ids:
            followups += 1
        total_words += len(tokens)
        contractions += len(_CONTRACTION_RE.findall(text))
        if tokens and tokens[0] in _IMPERATIVE_VERBS:
            imperative_hits += 1

        use_ngrams = ngram_role is None or (row["role"] == ngram_role)
        if use_ngrams:
            ngram_docs += 1
            bg = _opening_ngrams(tokens, 2)
            tg = _opening_ngrams(tokens, 3)
            if bg:
                bigrams[bg] += 1
            if tg:
                trigrams[tg] += 1

    char_sorted = sorted(char_lens)
    word_sorted = sorted(word_lens)

    def _top(counter: Counter[str]) -> list[dict[str, Any]]:
        return [{"ngram": k, "count": int(v)} for k, v in counter.most_common(top_n)]

    filters_out: dict[str, Any] = {
        "source": src_arg,
        "role": role,
        "since": since,
        "until": until,
    }
    if index_meta is not None:
        filters_out["index"] = index_meta
    out: dict[str, Any] = {
        "op": "style_stats",
        "filters": filters_out,
        "count": count,
        "char_length": {
            "mean": None if count == 0 else round(_mean(char_lens) or 0.0, 3),
            "median": None if count == 0 else _median(char_sorted),
        },
        "word_length": {
            "mean": None if count == 0 else round(_mean(word_lens) or 0.0, 3),
            "median": None if count == 0 else _median(word_sorted),
        },
        "question_mark_density": (
            None if total_chars == 0 else round(qmark_chars / total_chars, 6)
        ),
        "followup_share": None if count == 0 else round(followups / count, 6),
        "opening_ngrams": {
            "role": ngram_role,
            "docs": ngram_docs,
            "top_bigrams": _top(bigrams),
            "top_trigrams": _top(trigrams),
            "note": (
                "Opening n-grams default to human-role messages; when a role "
                "filter is set, n-grams use that role instead."
            ),
        },
        "register_approx": {
            "contraction_rate": (
                None if total_words == 0 else round(contractions / total_words, 6)
            ),
            "imperative_open_share": (
                None if count == 0 else round(imperative_hits / count, 6)
            ),
            "note": (
                "Approximate heuristics only (contraction regex + crude imperative "
                "verb list on first token)."
            ),
        },
    }
    return _fit_response(out)


def contrast(
    conn: sqlite3.Connection,
    *,
    source_a: str | Sequence[str] | None = None,
    source_b: str | Sequence[str] | None = None,
    since_a: str | None = None,
    until_a: str | None = None,
    since_b: str | None = None,
    until_b: str | None = None,
    role: str | None = None,
    role_a: str | None = None,
    role_b: str | None = None,
) -> dict[str, Any]:
    """Compare two slices on the same metrics as style_stats, with deltas."""
    ra = role_a if role_a is not None else role
    rb = role_b if role_b is not None else role
    if (
        source_a is None
        and source_b is None
        and since_a is None
        and until_a is None
        and since_b is None
        and until_b is None
        and ra is None
        and rb is None
    ):
        raise ValueError(
            "contrast needs at least one differing slice "
            "(source_a/source_b and/or time windows)"
        )

    slice_a = style_stats(
        conn, source=source_a, role=ra, since=since_a, until=until_a
    )
    slice_b = style_stats(
        conn, source=source_b, role=rb, since=since_b, until=until_b
    )

    def _num(path: Sequence[str], payload: dict[str, Any]) -> float | None:
        cur: Any = payload
        for key in path:
            if not isinstance(cur, dict) or key not in cur:
                return None
            cur = cur[key]
        if cur is None:
            return None
        try:
            return float(cur)
        except (TypeError, ValueError):
            return None

    metric_paths: list[tuple[str, tuple[str, ...]]] = [
        ("count", ("count",)),
        ("char_length_mean", ("char_length", "mean")),
        ("char_length_median", ("char_length", "median")),
        ("word_length_mean", ("word_length", "mean")),
        ("word_length_median", ("word_length", "median")),
        ("question_mark_density", ("question_mark_density",)),
        ("followup_share", ("followup_share",)),
        ("contraction_rate", ("register_approx", "contraction_rate")),
        ("imperative_open_share", ("register_approx", "imperative_open_share")),
    ]
    deltas: dict[str, Any] = {}
    for name, path in metric_paths:
        a = _num(path, slice_a)
        b = _num(path, slice_b)
        if a is None and b is None:
            deltas[name] = {"a": None, "b": None, "delta_b_minus_a": None}
        else:
            aa = 0.0 if a is None else a
            bb = 0.0 if b is None else b
            deltas[name] = {
                "a": a,
                "b": b,
                "delta_b_minus_a": None if (a is None or b is None) else round(bb - aa, 6),
            }

    # Drop heavy n-gram lists from nested copies; keep counts only.
    def _slim(stats_payload: dict[str, Any]) -> dict[str, Any]:
        slim = dict(stats_payload)
        slim.pop("truncated_response", None)
        ong = dict(slim.get("opening_ngrams") or {})
        ong["top_bigrams"] = (ong.get("top_bigrams") or [])[:5]
        ong["top_trigrams"] = (ong.get("top_trigrams") or [])[:5]
        slim["opening_ngrams"] = ong
        return slim

    return _fit_response(
        {
            "op": "contrast",
            "slice_a": {
                "source": source_a,
                "role": ra,
                "since": since_a,
                "until": until_a,
                "stats": _slim(slice_a),
            },
            "slice_b": {
                "source": source_b,
                "role": rb,
                "since": since_b,
                "until": until_b,
                "stats": _slim(slice_b),
            },
            "deltas": deltas,
        }
    )


def _embed_artifact_paths(
    embeddings_path: Path, meta_path: Path
) -> list[Path]:
    paths = [embeddings_path, meta_path]
    # Atomic write leftovers / temp siblings in the same directory.
    parent = embeddings_path.parent
    stem = embeddings_path.name
    for p in parent.glob(f".{stem}.*"):
        paths.append(p)
    for p in parent.glob(f"{meta_path.name}.*"):
        paths.append(p)
    # De-dupe while preserving order.
    seen: set[Path] = set()
    out: list[Path] = []
    for p in paths:
        rp = p.resolve() if p.exists() else p
        if rp in seen:
            continue
        seen.add(rp)
        out.append(p)
    return out


def embed_index_bytes(embeddings_path: Path, meta_path: Path) -> int:
    total = 0
    for p in _embed_artifact_paths(embeddings_path, meta_path):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def assert_embed_disk_budget(
    *,
    embeddings_path: Path,
    meta_path: Path,
    projected_bytes: int,
    hard_limit: int = EMBED_DISK_HARD_LIMIT,
) -> None:
    """Warn and stop before the vector-index disk hard limit would be exceeded."""
    current = embed_index_bytes(embeddings_path, meta_path)
    # Rebuild replaces artifacts; budget against projected final size, not sum.
    planned = max(int(projected_bytes), 0)
    if planned > hard_limit:
        raise RuntimeError(
            f"refusing embed write: projected index {planned} bytes exceeds "
            f"disk hard limit {hard_limit} bytes "
            f"(current artifacts {current} bytes). Warn Austen before raising the cap."
        )


def _embed_fingerprint_payload(
    fp: InputFingerprint,
    *,
    model_name: str,
    embed_schema_version: int = EMBED_SCHEMA_VERSION,
) -> dict[str, Any]:
    return {
        "inputs": fp.to_dict(),
        "model": model_name,
        "embed_schema_version": embed_schema_version,
        "chunking": "whole_message_truncated_to_model_max",
        "normalize": "l2",
    }


def _embed_fingerprint_digest(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _require_numpy() -> Any:
    try:
        import numpy as np  # noqa: WPS433 — optional Phase 2′b dep
    except ImportError as exc:
        raise RuntimeError(
            "numpy is required for semantic search; "
            "install into repo .venv (see docs/map.md)"
        ) from exc
    return np


def _require_sentence_transformer(model_name: str) -> Any:
    if model_name in _EMBED_MODEL_CACHE:
        return _EMBED_MODEL_CACHE[model_name]
    try:
        from sentence_transformers import SentenceTransformer  # noqa: WPS433
    except ImportError as exc:
        raise RuntimeError(
            "sentence-transformers is required for semantic search; "
            "install into repo .venv (see docs/map.md)"
        ) from exc
    model = SentenceTransformer(model_name)
    _EMBED_MODEL_CACHE[model_name] = model
    return model


def _load_embed_index(embeddings_path: Path, meta_path: Path) -> dict[str, Any]:
    key = str(embeddings_path.resolve())
    cached = _EMBED_INDEX_CACHE.get(key)
    if cached is not None:
        return cached
    np = _require_numpy()
    if not embeddings_path.is_file():
        raise FileNotFoundError(
            f"embeddings index missing: {embeddings_path} "
            "(run: python scripts/build_map_embeddings.py)"
        )
    data = np.load(embeddings_path, allow_pickle=False)
    ids = data["ids"]
    vectors = np.asarray(data["vectors"], dtype=np.float32)
    meta: dict[str, Any] = {}
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}
    packed = {
        "ids": ids,
        "vectors": vectors,
        "id_to_row": {str(i): n for n, i in enumerate(ids.tolist())},
        "meta": meta,
        "model": meta.get("model") or DEFAULT_EMBED_MODEL,
    }
    _EMBED_INDEX_CACHE[key] = packed
    return packed


def clear_embed_caches() -> None:
    _EMBED_INDEX_CACHE.clear()
    _EMBED_MODEL_CACHE.clear()


def build_embeddings(
    *,
    grok: Path = DEFAULT_GROK,
    x: Path = DEFAULT_X,
    db_path: Path = DEFAULT_DB,
    embeddings_path: Path = DEFAULT_EMBEDDINGS,
    meta_path: Path = DEFAULT_EMBED_META,
    model_name: str = DEFAULT_EMBED_MODEL,
    force: bool = False,
    batch_size: int = DEFAULT_EMBED_BATCH,
    progress_every: int = 2000,
    disk_hard_limit: int = EMBED_DISK_HARD_LIMIT,
) -> dict[str, Any]:
    """
    Embed all records from the FTS sqlite (keyed by id). Fingerprint skip like FTS.
    Whole-message vectors; long texts truncated to the model max sequence length.
    """
    np = _require_numpy()
    grok = Path(grok)
    x = Path(x)
    db_path = Path(db_path)
    embeddings_path = Path(embeddings_path)
    meta_path = Path(meta_path)
    if not db_path.is_file():
        raise FileNotFoundError(
            f"FTS index missing: {db_path} (run: python scripts/build_map_index.py)"
        )

    fp = fingerprint_inputs(grok, x)
    fp_payload = _embed_fingerprint_payload(fp, model_name=model_name)
    digest = _embed_fingerprint_digest(fp_payload)

    if (
        not force
        and embeddings_path.is_file()
        and meta_path.is_file()
    ):
        try:
            old = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            old = {}
        if old.get("digest") == digest:
            n = int(old.get("records") or 0)
            return {
                "skipped": True,
                "reason": "fingerprint_match",
                "records": n,
                "embeddings": str(embeddings_path),
                "meta": str(meta_path),
                "fingerprint": fp_payload,
                "digest": digest,
                "bytes": embed_index_bytes(embeddings_path, meta_path),
                "disk_hard_limit": disk_hard_limit,
            }

    t0 = time.perf_counter()
    conn = connect(db_path, readonly=True)
    try:
        rows = conn.execute(
            "SELECT id, text FROM records ORDER BY rowid"
        ).fetchall()
    finally:
        conn.close()

    ids = [str(r["id"]) for r in rows]
    texts = [r["text"] or "" for r in rows]
    n = len(ids)
    if n == 0:
        raise ValueError("no records to embed")

    # Project float32 matrix + id array (+ json meta headroom).
    # Dims unknown until model loads; MiniLM-L6-v2 is 384 — use that for budget
    # pre-check, then re-check with actual dims before write.
    projected = n * 384 * 4 + n * 64 + 65_536
    assert_embed_disk_budget(
        embeddings_path=embeddings_path,
        meta_path=meta_path,
        projected_bytes=projected,
        hard_limit=disk_hard_limit,
    )

    print(
        f"loading model {model_name} (local cache / download; no corpus upload)…",
        file=sys.stderr,
        flush=True,
    )
    model = _require_sentence_transformer(model_name)
    try:
        import torch  # noqa: WPS433

        device = "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        device = "cpu"
    if device == "cuda":
        model = model.to(device)

    if hasattr(model, "get_embedding_dimension"):
        dims = int(model.get_embedding_dimension())
    else:
        dims = int(model.get_sentence_embedding_dimension())
    projected = n * dims * 4 + n * 64 + 65_536
    assert_embed_disk_budget(
        embeddings_path=embeddings_path,
        meta_path=meta_path,
        projected_bytes=projected,
        hard_limit=disk_hard_limit,
    )

    vectors = np.zeros((n, dims), dtype=np.float32)
    bs = max(1, int(batch_size))
    encoded = 0
    for start in range(0, n, bs):
        batch = texts[start : start + bs]
        emb = model.encode(
            batch,
            batch_size=len(batch),
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=True,
        )
        emb = np.asarray(emb, dtype=np.float32)
        vectors[start : start + len(batch)] = emb
        encoded += len(batch)
        if progress_every and (
            encoded % progress_every < bs or encoded == n
        ):
            elapsed = time.perf_counter() - t0
            rate = encoded / elapsed if elapsed > 0 else 0.0
            print(
                f"embedded {encoded}/{n} ({rate:.1f} rows/s, device={device})",
                file=sys.stderr,
                flush=True,
            )

    embeddings_path.parent.mkdir(parents=True, exist_ok=True)
    id_arr = np.asarray(ids, dtype=np.str_)
    # Final size check against serialized estimate.
    projected_write = int(vectors.nbytes + id_arr.nbytes + 131_072)
    assert_embed_disk_budget(
        embeddings_path=embeddings_path,
        meta_path=meta_path,
        projected_bytes=projected_write,
        hard_limit=disk_hard_limit,
    )

    tmp = embeddings_path.with_name(
        f".{embeddings_path.name}.{os.getpid()}.tmp.npz"
    )
    try:
        np.savez_compressed(tmp, ids=id_arr, vectors=vectors)
        tmp.replace(embeddings_path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass

    elapsed = round(time.perf_counter() - t0, 3)
    bytes_used = embed_index_bytes(embeddings_path, meta_path)
    # Meta written after npz so bytes include both once meta exists; recompute after.
    meta_obj = {
        "op": "build_embeddings",
        "model": model_name,
        "dims": dims,
        "records": n,
        "digest": digest,
        "fingerprint": fp_payload,
        "chunking": "whole_message_truncated_to_model_max",
        "normalize": "l2",
        "device": device,
        "batch_size": bs,
        "elapsed_s": elapsed,
        "embeddings": str(embeddings_path.resolve()),
        "db": str(db_path.resolve()),
        "disk_hard_limit": disk_hard_limit,
        "ram_guidance_bytes": EMBED_RAM_GUIDANCE,
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    meta_path.write_text(
        json.dumps(meta_obj, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    bytes_used = embed_index_bytes(embeddings_path, meta_path)
    meta_obj["bytes"] = bytes_used
    meta_path.write_text(
        json.dumps(meta_obj, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    clear_embed_caches()
    return {
        "skipped": False,
        **meta_obj,
        "meta_path": str(meta_path),
    }


def semantic_search(
    conn: sqlite3.Connection,
    query: str,
    *,
    sources: str | Sequence[str] | None = None,
    role: str | None = None,
    since: str | None = None,
    until: str | None = None,
    index: str | None = None,
    limit: int | None = None,
    max_chars: int | None = None,
    embeddings_path: Path = DEFAULT_EMBEDDINGS,
    meta_path: Path = DEFAULT_EMBED_META,
) -> dict[str, Any]:
    """Local embedding nearest-neighbor search; cite + snippet like FTS search."""
    q = (query or "").strip()
    if not q:
        raise ValueError("query is required")
    lim = _clamp_limit(limit, DEFAULT_SEARCH_LIMIT)
    snippet_chars = _clamp_chars(max_chars, DEFAULT_SNIPPET_CHARS, MAX_SNIPPET_CHARS)
    np = _require_numpy()

    embed_index = _load_embed_index(Path(embeddings_path), Path(meta_path))
    model_name = str(embed_index.get("model") or DEFAULT_EMBED_MODEL)
    model = _require_sentence_transformer(model_name)
    q_vec = model.encode(
        [q],
        convert_to_numpy=True,
        normalize_embeddings=True,
    )
    q_vec = np.asarray(q_vec, dtype=np.float32).reshape(-1)

    vectors = embed_index["vectors"]
    ids = embed_index["ids"]

    def _empty_sem(*, index_meta: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "op": "semantic_search",
            "query": q,
            "limit": lim,
            "returned": 0,
            "model": model_name,
            "hits": [],
        }
        if index_meta is not None:
            payload["index"] = index_meta
        return _fit_response(payload)

    if vectors.shape[0] == 0:
        return _empty_sem()

    where_sql, params = _filter_where(
        sources=sources, role=role, since=since, until=until
    )
    where_parts = [where_sql]
    index_meta = _apply_index_scope(conn, where_parts, params, index)
    where_sql = " AND ".join(where_parts)
    # If any filter is active, restrict to matching ids.
    filter_active = bool(
        _parse_sources(sources) is not None
        or role is not None
        or since
        or until
        or index_meta is not None
    )
    candidate_rows: np.ndarray | None = None
    if filter_active:
        allowed = {
            str(r["id"])
            for r in conn.execute(
                f"SELECT id FROM records WHERE {where_sql}", params
            )
        }
        if not allowed:
            return _empty_sem(index_meta=index_meta)
        id_to_row = embed_index["id_to_row"]
        rows_list = [id_to_row[i] for i in allowed if i in id_to_row]
        if not rows_list:
            return _empty_sem(index_meta=index_meta)
        candidate_rows = np.asarray(rows_list, dtype=np.int64)
        scores = vectors[candidate_rows] @ q_vec
        top_n = min(lim, scores.shape[0])
        if top_n <= 0:
            pick_local = np.asarray([], dtype=np.int64)
        elif top_n == scores.shape[0]:
            pick_local = np.argsort(-scores)
        else:
            part = np.argpartition(-scores, top_n - 1)[:top_n]
            pick_local = part[np.argsort(-scores[part])]
        pick = candidate_rows[pick_local]
        pick_scores = scores[pick_local]
    else:
        scores = vectors @ q_vec
        top_n = min(lim, scores.shape[0])
        if top_n == scores.shape[0]:
            pick = np.argsort(-scores)
        else:
            part = np.argpartition(-scores, top_n - 1)[:top_n]
            pick = part[np.argsort(-scores[part])]
        pick_scores = scores[pick]

    hit_ids = [str(ids[int(i)]) for i in pick]
    placeholders = ",".join("?" * len(hit_ids)) if hit_ids else ""
    by_id: dict[str, sqlite3.Row] = {}
    if hit_ids:
        for row in conn.execute(
            f"""
            SELECT id, source, created_at, url, thread_id, role, text
            FROM records WHERE id IN ({placeholders})
            """,
            hit_ids,
        ):
            by_id[str(row["id"])] = row

    hits: list[dict[str, Any]] = []
    for rid, score in zip(hit_ids, pick_scores.tolist()):
        row = by_id.get(rid)
        if row is None:
            continue
        item = _row_cite(row)
        item["score"] = round(float(score), 6)
        snip, _ = _truncate(row["text"] or "", snippet_chars)
        item["snippet"] = snip
        hits.append(item)

    out: dict[str, Any] = {
        "op": "semantic_search",
        "query": q,
        "limit": lim,
        "returned": len(hits),
        "model": model_name,
        "hits": hits,
    }
    if index_meta is not None:
        out["index"] = index_meta
    return _fit_response(out)


def _is_humanish_role(role: str | None) -> bool:
    return role in ("human", "self")


def _message_cue_flags(text: str, *, humanish: bool) -> tuple[int, int, int, int]:
    """Return (refine, critique, code, tech) 0/1 flags for one message."""
    body = text or ""
    refine = 1 if humanish and _REFINE_CUE_RE.search(body) else 0
    critique = 1 if humanish and _CRITIQUE_CUE_RE.search(body) else 0
    code = 1 if _CODE_MARK_RE.search(body) else 0
    tech = 1 if _TECH_MARK_RE.search(body) else 0
    return refine, critique, code, tech


def _finalize_thread_feature(acc: dict[str, Any]) -> dict[str, Any]:
    message_count = int(acc["message_count"])
    humanish = int(acc["human_count"]) + int(acc["self_count"])
    assistant = int(acc["assistant_count"])
    ratio = None if assistant == 0 else round(humanish / assistant, 6)
    code_density = (
        0.0 if message_count == 0 else round(int(acc["code_msg_count"]) / message_count, 6)
    )
    tech_density = (
        0.0 if message_count == 0 else round(int(acc["tech_msg_count"]) / message_count, 6)
    )
    source_counts: Counter[str] = acc["source_counts"]
    source_primary = source_counts.most_common(1)[0][0] if source_counts else None
    return {
        "thread_id": acc["thread_id"],
        "message_count": message_count,
        "human_count": int(acc["human_count"]),
        "assistant_count": assistant,
        "self_count": int(acc["self_count"]),
        "other_role_count": int(acc["other_role_count"]),
        "human_assistant_ratio": ratio,
        "refine_cue_count": int(acc["refine_cue_count"]),
        "critique_cue_count": int(acc["critique_cue_count"]),
        "code_msg_count": int(acc["code_msg_count"]),
        "code_density": code_density,
        "tech_msg_count": int(acc["tech_msg_count"]),
        "tech_density": tech_density,
        "has_refine": 1 if int(acc["refine_cue_count"]) > 0 else 0,
        "has_critique": 1 if int(acc["critique_cue_count"]) > 0 else 0,
        "source_primary": source_primary,
        "first_created_at": acc["first_created_at"],
        "last_created_at": acc["last_created_at"],
    }


def _empty_thread_acc(thread_id: str) -> dict[str, Any]:
    return {
        "thread_id": thread_id,
        "message_count": 0,
        "human_count": 0,
        "assistant_count": 0,
        "self_count": 0,
        "other_role_count": 0,
        "refine_cue_count": 0,
        "critique_cue_count": 0,
        "code_msg_count": 0,
        "tech_msg_count": 0,
        "source_counts": Counter(),
        "first_created_at": None,
        "last_created_at": None,
        "_first_key": None,
        "_last_key": None,
    }


def _accumulate_message(acc: dict[str, Any], row: sqlite3.Row | dict[str, Any]) -> None:
    get = row.__getitem__
    role = get("role")
    text = get("text") or ""
    created = get("created_at")
    source = get("source")
    rid = get("id")
    acc["message_count"] += 1
    if role == "human":
        acc["human_count"] += 1
    elif role == "assistant":
        acc["assistant_count"] += 1
    elif role == "self":
        acc["self_count"] += 1
    else:
        acc["other_role_count"] += 1
    humanish = _is_humanish_role(role)
    refine, critique, code, tech = _message_cue_flags(text, humanish=humanish)
    acc["refine_cue_count"] += refine
    acc["critique_cue_count"] += critique
    acc["code_msg_count"] += code
    acc["tech_msg_count"] += tech
    if source:
        acc["source_counts"][source] += 1
    key = (1 if created is None else 0, created or "", rid or "")
    if acc["_first_key"] is None or key < acc["_first_key"]:
        acc["_first_key"] = key
        acc["first_created_at"] = created
    if acc["_last_key"] is None or key > acc["_last_key"]:
        acc["_last_key"] = key
        acc["last_created_at"] = created


def compute_thread_feature(
    conn: sqlite3.Connection, thread_id: str
) -> dict[str, Any] | None:
    """Compute heuristic features for one thread (no table required)."""
    if not thread_id:
        raise ValueError("thread_id is required")
    rows = conn.execute(
        """
        SELECT id, source, created_at, role, text
        FROM records
        WHERE thread_id = ?
        ORDER BY
          CASE WHEN created_at IS NULL THEN 1 ELSE 0 END,
          created_at ASC,
          id ASC
        """,
        (thread_id,),
    ).fetchall()
    if not rows:
        return None
    acc = _empty_thread_acc(thread_id)
    for row in rows:
        _accumulate_message(acc, row)
    return _finalize_thread_feature(acc)


def _compute_all_thread_features(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    accs: dict[str, dict[str, Any]] = {}
    for row in conn.execute(
        "SELECT id, source, created_at, thread_id, role, text FROM records"
    ):
        tid = row["thread_id"]
        acc = accs.get(tid)
        if acc is None:
            acc = _empty_thread_acc(tid)
            accs[tid] = acc
        _accumulate_message(acc, row)
    return {tid: _finalize_thread_feature(acc) for tid, acc in accs.items()}


def _thread_features_table_ready(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='thread_features'"
    ).fetchone()
    return row is not None


def _records_fingerprint_digest(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT value FROM map_meta WHERE key='fingerprint'"
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row["value"])
    except (TypeError, json.JSONDecodeError):
        return None
    digest = payload.get("digest")
    return digest if isinstance(digest, str) else None


def _thread_features_meta(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT value FROM map_meta WHERE key=?",
        (THREAD_FEATURES_META_KEY,),
    ).fetchone()
    if row is None:
        return None
    try:
        meta = json.loads(row["value"])
    except (TypeError, json.JSONDecodeError):
        return None
    return meta if isinstance(meta, dict) else None


def build_thread_features(
    conn: sqlite3.Connection,
    *,
    force: bool = False,
    records_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Rebuild thread_features table (writable connection required)."""
    fp = records_fingerprint or _records_fingerprint_digest(conn)
    prior = _thread_features_meta(conn)
    if (
        not force
        and prior is not None
        and prior.get("schema_version") == THREAD_FEATURES_SCHEMA_VERSION
        and prior.get("records_fingerprint") == fp
        and _thread_features_table_ready(conn)
    ):
        count_row = conn.execute("SELECT COUNT(*) AS n FROM thread_features").fetchone()
        return {
            "skipped": True,
            "reason": "fingerprint_match",
            "threads": int(count_row["n"]) if count_row else 0,
            "schema_version": THREAD_FEATURES_SCHEMA_VERSION,
            "records_fingerprint": fp,
        }

    t0 = time.perf_counter()
    features = _compute_all_thread_features(conn)
    conn.executescript(
        """
        DROP TABLE IF EXISTS thread_features;
        """
    )
    conn.executescript(_THREAD_FEATURES_DDL)
    rows = [
        (
            f["thread_id"],
            f["message_count"],
            f["human_count"],
            f["assistant_count"],
            f["self_count"],
            f["other_role_count"],
            f["human_assistant_ratio"],
            f["refine_cue_count"],
            f["critique_cue_count"],
            f["code_msg_count"],
            f["code_density"],
            f["tech_msg_count"],
            f["tech_density"],
            f["has_refine"],
            f["has_critique"],
            f["source_primary"],
            f["first_created_at"],
            f["last_created_at"],
        )
        for f in features.values()
    ]
    conn.executemany(
        """
        INSERT INTO thread_features (
            thread_id, message_count, human_count, assistant_count, self_count,
            other_role_count, human_assistant_ratio, refine_cue_count,
            critique_cue_count, code_msg_count, code_density, tech_msg_count,
            tech_density, has_refine, has_critique, source_primary,
            first_created_at, last_created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    built_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    meta = {
        "schema_version": THREAD_FEATURES_SCHEMA_VERSION,
        "records_fingerprint": fp,
        "threads": len(rows),
        "built_at": built_at,
        "note": (
            "Heuristic only (regex refine/critique on human|self; "
            "code/tech density over all turns). No LLM labels."
        ),
    }
    conn.execute(
        "INSERT INTO map_meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (THREAD_FEATURES_META_KEY, json.dumps(meta, separators=(",", ":"))),
    )
    conn.commit()
    return {
        "skipped": False,
        "threads": len(rows),
        "schema_version": THREAD_FEATURES_SCHEMA_VERSION,
        "records_fingerprint": fp,
        "built_at": built_at,
        "elapsed_s": round(time.perf_counter() - t0, 3),
    }


def _feature_row_to_dict(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    get = row.__getitem__
    return {
        "thread_id": get("thread_id"),
        "message_count": int(get("message_count")),
        "human_count": int(get("human_count")),
        "assistant_count": int(get("assistant_count")),
        "self_count": int(get("self_count")),
        "other_role_count": int(get("other_role_count")),
        "human_assistant_ratio": get("human_assistant_ratio"),
        "refine_cue_count": int(get("refine_cue_count")),
        "critique_cue_count": int(get("critique_cue_count")),
        "code_msg_count": int(get("code_msg_count")),
        "code_density": float(get("code_density")),
        "tech_msg_count": int(get("tech_msg_count")),
        "tech_density": float(get("tech_density")),
        "has_refine": bool(int(get("has_refine"))),
        "has_critique": bool(int(get("has_critique"))),
        "source_primary": get("source_primary"),
        "first_created_at": get("first_created_at"),
        "last_created_at": get("last_created_at"),
    }


def _thread_features_map(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Load features from table when fresh; else compute in-memory (readonly-safe)."""
    fp = _records_fingerprint_digest(conn)
    meta = _thread_features_meta(conn)
    if (
        _thread_features_table_ready(conn)
        and meta is not None
        and meta.get("schema_version") == THREAD_FEATURES_SCHEMA_VERSION
        and meta.get("records_fingerprint") == fp
    ):
        out: dict[str, dict[str, Any]] = {}
        for row in conn.execute("SELECT * FROM thread_features"):
            feat = _feature_row_to_dict(row)
            out[feat["thread_id"]] = feat
        return out
    return _compute_all_thread_features(conn)


def _matching_thread_ids(
    conn: sqlite3.Connection,
    *,
    min_thread_length: int | None = None,
    max_thread_length: int | None = None,
    has_refine: bool | None = None,
    has_critique: bool | None = None,
    min_code_density: float | None = None,
    min_tech_density: float | None = None,
    source: str | Sequence[str] | None = None,
) -> set[str] | None:
    """Return matching thread_ids, or None when no feature filters are set."""
    srcs = _parse_sources(source)
    if (
        min_thread_length is None
        and max_thread_length is None
        and has_refine is None
        and has_critique is None
        and min_code_density is None
        and min_tech_density is None
        and srcs is None
    ):
        return None

    allow: set[str] = set()
    for tid, feat in _thread_features_map(conn).items():
        if min_thread_length is not None and feat["message_count"] < int(min_thread_length):
            continue
        if max_thread_length is not None and feat["message_count"] > int(max_thread_length):
            continue
        if has_refine is not None and bool(feat["has_refine"]) != bool(has_refine):
            continue
        if has_critique is not None and bool(feat["has_critique"]) != bool(has_critique):
            continue
        if min_code_density is not None and float(feat["code_density"]) < float(
            min_code_density
        ):
            continue
        if min_tech_density is not None and float(feat["tech_density"]) < float(
            min_tech_density
        ):
            continue
        if srcs is not None and feat.get("source_primary") not in srcs:
            continue
        allow.add(tid)
    return allow


def thread_features(
    conn: sqlite3.Connection,
    *,
    thread_id: str | None = None,
    source: str | Sequence[str] | None = None,
    min_thread_length: int | None = None,
    max_thread_length: int | None = None,
    has_refine: bool | None = None,
    has_critique: bool | None = None,
    min_code_density: float | None = None,
    min_tech_density: float | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> dict[str, Any]:
    """Heuristic per-thread features (single id or filtered list, ≤50)."""
    lim = _clamp_limit(limit, DEFAULT_THREAD_FEATURES_LIMIT)
    off = max(0, int(offset))
    note = (
        "Approximate heuristics only — refine/critique cues are regex on "
        "human|self turns; code/tech density is marker share across all turns. "
        "No LLM labels."
    )

    if thread_id:
        feat = None
        fmap = None
        fp = _records_fingerprint_digest(conn)
        meta = _thread_features_meta(conn)
        if (
            _thread_features_table_ready(conn)
            and meta is not None
            and meta.get("schema_version") == THREAD_FEATURES_SCHEMA_VERSION
            and meta.get("records_fingerprint") == fp
        ):
            row = conn.execute(
                "SELECT * FROM thread_features WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            if row is not None:
                feat = _feature_row_to_dict(row)
        if feat is None:
            feat = compute_thread_feature(conn, thread_id)
        if feat is None:
            return _fit_response(
                {
                    "op": "thread_features",
                    "thread_id": thread_id,
                    "found": False,
                    "note": note,
                }
            )
        # Normalize bools for single-thread payload consistency with list mode.
        out_feat = dict(feat)
        out_feat["has_refine"] = bool(out_feat["has_refine"])
        out_feat["has_critique"] = bool(out_feat["has_critique"])
        return _fit_response(
            {
                "op": "thread_features",
                "thread_id": thread_id,
                "found": True,
                "feature": out_feat,
                "note": note,
                "schema_version": THREAD_FEATURES_SCHEMA_VERSION,
            }
        )

    fmap = _thread_features_map(conn)
    allow = _matching_thread_ids(
        conn,
        min_thread_length=min_thread_length,
        max_thread_length=max_thread_length,
        has_refine=has_refine,
        has_critique=has_critique,
        min_code_density=min_code_density,
        min_tech_density=min_tech_density,
        source=source,
    )
    items = list(fmap.values()) if allow is None else [fmap[t] for t in allow if t in fmap]
    items.sort(
        key=lambda f: (
            -int(f["message_count"]),
            f.get("last_created_at") or "",
            f["thread_id"],
        )
    )
    total = len(items)
    page = items[off : off + lim]
    hits = []
    for feat in page:
        item = dict(feat)
        item["has_refine"] = bool(item["has_refine"])
        item["has_critique"] = bool(item["has_critique"])
        hits.append(item)

    return _fit_response(
        {
            "op": "thread_features",
            "filters": {
                "source": source,
                "min_thread_length": min_thread_length,
                "max_thread_length": max_thread_length,
                "has_refine": has_refine,
                "has_critique": has_critique,
                "min_code_density": min_code_density,
                "min_tech_density": min_tech_density,
            },
            "total": total,
            "limit": lim,
            "offset": off,
            "returned": len(hits),
            "hits": hits,
            "note": note,
            "schema_version": THREAD_FEATURES_SCHEMA_VERSION,
        }
    )


def _index_member_count(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM analysis_index_members WHERE index_name = ?",
        (name,),
    ).fetchone()
    return int(row["n"]) if row else 0


def _index_summary(
    conn: sqlite3.Connection, row: sqlite3.Row | dict[str, Any]
) -> dict[str, Any]:
    get = row.__getitem__
    name = get("name")
    kind = get("kind")
    filt = None
    if get("filter_json"):
        try:
            filt = json.loads(get("filter_json"))
        except (TypeError, json.JSONDecodeError):
            filt = get("filter_json")
    out: dict[str, Any] = {
        "name": name,
        "version": int(get("version")),
        "kind": kind,
        "description": get("description"),
        "filter": filt,
        "member_count": (
            _index_member_count(conn, name) if kind in ("message_ids", "thread_ids") else None
        ),
        "created_at": get("created_at"),
        "updated_at": get("updated_at"),
    }
    return out


def index_create(
    conn: sqlite3.Connection,
    name: str,
    *,
    kind: str = "message_ids",
    description: str | None = None,
    ids: Sequence[str] | None = None,
    filter: dict[str, Any] | str | None = None,
    sources: str | Sequence[str] | None = None,
    role: str | None = None,
    since: str | None = None,
    until: str | None = None,
    replace: bool = False,
) -> dict[str, Any]:
    """Create a named analysis index (optional; competing names OK)."""
    ensure_analysis_index_schema(conn)
    iname = _normalize_index_name(name)
    ikind = _normalize_index_kind(kind)
    desc = _normalize_description(description)
    members = _normalize_member_ids(ids)
    if len(members) > MAX_INDEX_MEMBERS_TOTAL:
        raise ValueError(
            f"index members capped at {MAX_INDEX_MEMBERS_TOTAL} "
            f"(got {len(members)})"
        )
    filt = _filter_expr_dict(
        sources=sources, role=role, since=since, until=until, filter_obj=filter
    )
    if ikind == "filter":
        if filt is None:
            raise ValueError("filter kind requires a filter expression")
        if members:
            raise ValueError("filter kind does not take ids (use message_ids/thread_ids)")
    else:
        # id-based indexes may start empty; filter_json optional provenance only
        pass

    existing = _load_analysis_index(conn, iname)
    now = _utc_now_iso()
    if existing is not None:
        if not replace:
            raise ValueError(
                f"analysis index {iname!r} already exists "
                f"(version {int(existing['version'])}); pass replace=true"
            )
        version = int(existing["version"]) + 1
        created_at = existing["created_at"]
        conn.execute("DELETE FROM analysis_index_members WHERE index_name = ?", (iname,))
        conn.execute(
            """
            UPDATE analysis_indexes
            SET version = ?, kind = ?, description = ?, filter_json = ?,
                updated_at = ?
            WHERE name = ?
            """,
            (
                version,
                ikind,
                desc,
                json.dumps(filt, separators=(",", ":")) if filt else None,
                now,
                iname,
            ),
        )
    else:
        n_indexes = conn.execute(
            "SELECT COUNT(*) AS n FROM analysis_indexes"
        ).fetchone()["n"]
        if int(n_indexes) >= MAX_INDEX_COUNT:
            raise ValueError(
                f"at most {MAX_INDEX_COUNT} named analysis indexes allowed"
            )
        version = 1
        created_at = now
        conn.execute(
            """
            INSERT INTO analysis_indexes
                (name, version, kind, description, filter_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                iname,
                version,
                ikind,
                desc,
                json.dumps(filt, separators=(",", ":")) if filt else None,
                created_at,
                now,
            ),
        )

    if members and ikind in ("message_ids", "thread_ids"):
        conn.executemany(
            """
            INSERT OR IGNORE INTO analysis_index_members (index_name, member_id)
            VALUES (?, ?)
            """,
            [(iname, mid) for mid in members],
        )

    row = _load_analysis_index(conn, iname)
    assert row is not None
    summary = _index_summary(conn, row)
    return _fit_response(
        {
            "op": "index_create",
            "created": existing is None,
            "replaced": existing is not None,
            "index": summary,
            "schema_version": ANALYSIS_INDEX_SCHEMA_VERSION,
        }
    )


def index_list(
    conn: sqlite3.Connection,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> dict[str, Any]:
    lim = _clamp_limit(limit, DEFAULT_INDEX_LIST_LIMIT)
    off = max(0, int(offset))
    if not _has_analysis_index_tables(conn):
        return _fit_response(
            {
                "op": "index_list",
                "total": 0,
                "limit": lim,
                "offset": off,
                "returned": 0,
                "indexes": [],
                "schema_version": ANALYSIS_INDEX_SCHEMA_VERSION,
            }
        )
    total = conn.execute("SELECT COUNT(*) AS n FROM analysis_indexes").fetchone()["n"]
    rows = conn.execute(
        """
        SELECT name, version, kind, description, filter_json, created_at, updated_at
        FROM analysis_indexes
        ORDER BY name ASC
        LIMIT ? OFFSET ?
        """,
        (lim, off),
    ).fetchall()
    items = [_index_summary(conn, r) for r in rows]
    return _fit_response(
        {
            "op": "index_list",
            "total": int(total),
            "limit": lim,
            "offset": off,
            "returned": len(items),
            "indexes": items,
            "schema_version": ANALYSIS_INDEX_SCHEMA_VERSION,
        }
    )


def index_get(
    conn: sqlite3.Connection,
    name: str,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> dict[str, Any]:
    iname = _normalize_index_name(name)
    row = _load_analysis_index(conn, iname)
    if row is None:
        return _fit_response(
            {
                "op": "index_get",
                "name": iname,
                "found": False,
                "index": None,
                "members": [],
            }
        )
    lim = _clamp_limit(limit, DEFAULT_INDEX_MEMBERS_LIMIT)
    off = max(0, int(offset))
    summary = _index_summary(conn, row)
    members: list[str] = []
    member_total = 0
    if row["kind"] in ("message_ids", "thread_ids"):
        member_total = _index_member_count(conn, iname)
        members = [
            str(r["member_id"])
            for r in conn.execute(
                """
                SELECT member_id FROM analysis_index_members
                WHERE index_name = ?
                ORDER BY member_id ASC
                LIMIT ? OFFSET ?
                """,
                (iname, lim, off),
            )
        ]
    return _fit_response(
        {
            "op": "index_get",
            "name": iname,
            "found": True,
            "index": summary,
            "member_total": member_total if row["kind"] != "filter" else None,
            "limit": lim,
            "offset": off,
            "returned": len(members),
            "members": members,
            "schema_version": ANALYSIS_INDEX_SCHEMA_VERSION,
        }
    )


def index_delete(conn: sqlite3.Connection, name: str) -> dict[str, Any]:
    iname = _normalize_index_name(name)
    if not _has_analysis_index_tables(conn):
        return _fit_response(
            {
                "op": "index_delete",
                "name": iname,
                "deleted": False,
                "found": False,
            }
        )
    row = _load_analysis_index(conn, iname)
    if row is None:
        return _fit_response(
            {
                "op": "index_delete",
                "name": iname,
                "deleted": False,
                "found": False,
            }
        )
    summary = _index_summary(conn, row)
    conn.execute("DELETE FROM analysis_index_members WHERE index_name = ?", (iname,))
    conn.execute("DELETE FROM analysis_indexes WHERE name = ?", (iname,))
    return _fit_response(
        {
            "op": "index_delete",
            "name": iname,
            "deleted": True,
            "found": True,
            "index": summary,
        }
    )


def index_add_ids(
    conn: sqlite3.Connection,
    name: str,
    ids: Sequence[str],
) -> dict[str, Any]:
    """Append members to a message_ids/thread_ids index; bumps version."""
    ensure_analysis_index_schema(conn)
    iname = _normalize_index_name(name)
    members = _normalize_member_ids(ids)
    if not members:
        raise ValueError("ids required")
    row = _load_analysis_index(conn, iname)
    if row is None:
        raise ValueError(f"unknown analysis index: {iname}")
    if row["kind"] == "filter":
        raise ValueError("cannot add ids to a filter-kind index")
    before = _index_member_count(conn, iname)
    if before >= MAX_INDEX_MEMBERS_TOTAL:
        raise ValueError(
            f"index members capped at {MAX_INDEX_MEMBERS_TOTAL} "
            f"(already {before})"
        )
    conn.executemany(
        """
        INSERT OR IGNORE INTO analysis_index_members (index_name, member_id)
        VALUES (?, ?)
        """,
        [(iname, mid) for mid in members],
    )
    after = _index_member_count(conn, iname)
    if after > MAX_INDEX_MEMBERS_TOTAL:
        # Roll back via raising inside caller's transaction.
        raise ValueError(
            f"index members capped at {MAX_INDEX_MEMBERS_TOTAL} "
            f"(would be {after} after add)"
        )
    now = _utc_now_iso()
    version = int(row["version"]) + 1
    conn.execute(
        "UPDATE analysis_indexes SET version = ?, updated_at = ? WHERE name = ?",
        (version, now, iname),
    )
    row2 = _load_analysis_index(conn, iname)
    assert row2 is not None
    return _fit_response(
        {
            "op": "index_add_ids",
            "name": iname,
            "added": after - before,
            "submitted": len(members),
            "index": _index_summary(conn, row2),
        }
    )


def index_replace_members(
    conn: sqlite3.Connection,
    name: str,
    ids: Sequence[str],
) -> dict[str, Any]:
    """Replace all members of a message_ids/thread_ids index; bumps version."""
    ensure_analysis_index_schema(conn)
    iname = _normalize_index_name(name)
    members = _normalize_member_ids(ids)
    if len(members) > MAX_INDEX_MEMBERS_TOTAL:
        raise ValueError(
            f"index members capped at {MAX_INDEX_MEMBERS_TOTAL} "
            f"(got {len(members)})"
        )
    row = _load_analysis_index(conn, iname)
    if row is None:
        raise ValueError(f"unknown analysis index: {iname}")
    if row["kind"] == "filter":
        raise ValueError("cannot replace members on a filter-kind index")
    conn.execute("DELETE FROM analysis_index_members WHERE index_name = ?", (iname,))
    if members:
        conn.executemany(
            """
            INSERT OR IGNORE INTO analysis_index_members (index_name, member_id)
            VALUES (?, ?)
            """,
            [(iname, mid) for mid in members],
        )
    now = _utc_now_iso()
    version = int(row["version"]) + 1
    conn.execute(
        "UPDATE analysis_indexes SET version = ?, updated_at = ? WHERE name = ?",
        (version, now, iname),
    )
    row2 = _load_analysis_index(conn, iname)
    assert row2 is not None
    return _fit_response(
        {
            "op": "index_replace_members",
            "name": iname,
            "member_count": len(members),
            "index": _index_summary(conn, row2),
        }
    )


def open_db(db_path: Path = DEFAULT_DB, *, readonly: bool = True) -> sqlite3.Connection:
    conn = connect(db_path, readonly=readonly)
    if not readonly:
        ensure_analysis_index_schema(conn)
    return conn


def open_db_rw(db_path: Path = DEFAULT_DB) -> sqlite3.Connection:
    """Writable connection; ensures analysis-index schema exists."""
    return open_db(db_path, readonly=False)
