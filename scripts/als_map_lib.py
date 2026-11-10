#!/usr/bin/env python3
"""
Shared library for the Total ALS Map retrieval substrate.

SQLite + external-content FTS5 over normalized Grok/X JSONL.
Used by build_map_index.py, map_query.py (debug CLI), and als_map_mcp.py.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GROK = REPO_ROOT / "data" / "normalized" / "grok.jsonl"
DEFAULT_X = REPO_ROOT / "data" / "normalized" / "x.jsonl"
DEFAULT_DB = REPO_ROOT / "data" / "index" / "als_map.sqlite"
DEFAULT_STATS = REPO_ROOT / "data" / "index" / "als_map-stats.json"

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
META_SCHEMA_VERSION = 1

CITE_FIELDS = ("id", "source", "created_at", "url", "thread_id", "role")


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
    return parts or None


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


def open_db(db_path: Path = DEFAULT_DB) -> sqlite3.Connection:
    return connect(db_path, readonly=True)
