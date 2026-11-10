#!/usr/bin/env python3
"""
Shared library for the Total ALS Map retrieval substrate.

SQLite + external-content FTS5 over normalized Grok/X JSONL.
Optional Phase 2′b local embeddings (numpy + sentence-transformers).
Used by build_map_index.py, build_map_embeddings.py, map_query.py,
and als_map_mcp.py.
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

    return _fit_response(
        {
            "op": "search",
            "query": q,
            "limit": lim,
            "offset": off,
            "returned": len(hits),
            "full_text": full_text,
            "hits": hits,
        }
    )


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
    return _fit_response(
        {
            "op": "browse",
            "since": since,
            "until": until,
            "total": int(total),
            "limit": lim,
            "offset": off,
            "returned": len(hits),
            "hits": hits,
        }
    )


def stats(
    conn: sqlite3.Connection,
    *,
    breakdown: str | None = "source",
) -> dict[str, Any]:
    total = conn.execute("SELECT COUNT(*) AS n FROM records").fetchone()["n"]
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
    if "fingerprint" in meta_rows:
        try:
            out["fingerprint"] = json.loads(meta_rows["fingerprint"])
        except json.JSONDecodeError:
            out["fingerprint"] = meta_rows["fingerprint"]

    bd = (breakdown or "source").lower()
    if bd in ("source", "both"):
        rows = conn.execute(
            "SELECT source, COUNT(*) AS n FROM records GROUP BY source ORDER BY n DESC"
        ).fetchall()
        out["by_source"] = {r["source"]: int(r["n"]) for r in rows}
    if bd in ("role", "both"):
        rows = conn.execute(
            """
            SELECT COALESCE(role, 'null') AS role, COUNT(*) AS n
            FROM records GROUP BY role ORDER BY n DESC
            """
        ).fetchall()
        out["by_role"] = {r["role"]: int(r["n"]) for r in rows}
    if bd == "month":
        rows = conn.execute(
            """
            SELECT substr(created_at, 1, 7) AS month, COUNT(*) AS n
            FROM records
            WHERE created_at IS NOT NULL AND length(created_at) >= 7
            GROUP BY month
            ORDER BY month
            """
        ).fetchall()
        out["by_month"] = {r["month"]: int(r["n"]) for r in rows}
    elif bd not in ("source", "role", "both", "month", "none", ""):
        out["warning"] = f"unknown breakdown {breakdown!r}; used source"
        rows = conn.execute(
            "SELECT source, COUNT(*) AS n FROM records GROUP BY source ORDER BY n DESC"
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
    stratify_by: str | None = "none",
    seed: int | None = None,
    max_chars: int | None = None,
) -> dict[str, Any]:
    """Prior-free stratified random sample (snippet-shaped like search)."""
    lim = _clamp_limit(n, DEFAULT_SAMPLE_N)
    strat = (stratify_by or "none").strip().lower()
    if strat not in {"none", "time", "thread_length"}:
        raise ValueError("stratify_by must be none|time|thread_length")
    max_c = _clamp_chars(max_chars, DEFAULT_SNIPPET_CHARS, MAX_SNIPPET_CHARS)
    src_arg = source if source is not None else sources
    where_sql, params = _filter_where(sources=src_arg, role=role)

    rows = conn.execute(
        f"""
        SELECT id, source, created_at, url, thread_id, role, text
        FROM records
        WHERE {where_sql}
        """,
        params,
    ).fetchall()
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

    return _fit_response(
        {
            "op": "sample",
            "n": lim,
            "role": role,
            "source": src_arg,
            "stratify_by": strat,
            "seed": seed,
            "eligible": total,
            "returned": len(hits),
            "buckets": buckets_meta,
            "hits": hits,
        }
    )


def style_stats(
    conn: sqlite3.Connection,
    *,
    source: str | Sequence[str] | None = None,
    sources: str | Sequence[str] | None = None,
    role: str | None = None,
    since: str | None = None,
    until: str | None = None,
    opening_ngrams_top: int = DEFAULT_OPENING_NGRAM_TOP,
    opening_role: str | None = None,
) -> dict[str, Any]:
    """Cheap deterministic style aggregates (no full-text dump)."""
    src_arg = source if source is not None else sources
    where_sql, params = _filter_where(
        sources=src_arg, role=role, since=since, until=until
    )
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

    out: dict[str, Any] = {
        "op": "style_stats",
        "filters": {
            "source": src_arg,
            "role": role,
            "since": since,
            "until": until,
        },
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

    index = _load_embed_index(Path(embeddings_path), Path(meta_path))
    model_name = str(index.get("model") or DEFAULT_EMBED_MODEL)
    model = _require_sentence_transformer(model_name)
    q_vec = model.encode(
        [q],
        convert_to_numpy=True,
        normalize_embeddings=True,
    )
    q_vec = np.asarray(q_vec, dtype=np.float32).reshape(-1)

    vectors = index["vectors"]
    ids = index["ids"]
    if vectors.shape[0] == 0:
        return _fit_response(
            {
                "op": "semantic_search",
                "query": q,
                "limit": lim,
                "returned": 0,
                "model": model_name,
                "hits": [],
            }
        )

    where_sql, params = _filter_where(
        sources=sources, role=role, since=since, until=until
    )
    # If any filter is active, restrict to matching ids.
    filter_active = bool(
        _parse_sources(sources) is not None
        or role is not None
        or since
        or until
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
            return _fit_response(
                {
                    "op": "semantic_search",
                    "query": q,
                    "limit": lim,
                    "returned": 0,
                    "model": model_name,
                    "hits": [],
                }
            )
        id_to_row = index["id_to_row"]
        rows_list = [id_to_row[i] for i in allowed if i in id_to_row]
        if not rows_list:
            return _fit_response(
                {
                    "op": "semantic_search",
                    "query": q,
                    "limit": lim,
                    "returned": 0,
                    "model": model_name,
                    "hits": [],
                }
            )
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

    return _fit_response(
        {
            "op": "semantic_search",
            "query": q,
            "limit": lim,
            "returned": len(hits),
            "model": model_name,
            "hits": hits,
        }
    )


def open_db(db_path: Path = DEFAULT_DB) -> sqlite3.Connection:
    return connect(db_path, readonly=True)
