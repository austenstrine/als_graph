#!/usr/bin/env python3
"""
stdio MCP server for the Total ALS Map (Phase 1′ + 2′a + 2′b product surface).

Tools (hard-capped via als_map_lib):
  als_search, als_get, als_get_many, als_thread, als_browse, als_stats,
  als_sample, als_style_stats, als_contrast, als_semantic_search

Protocol: MCP over JSON-RPC 2.0 on stdin/stdout (Content-Length framing
compatible with the MCP stdio transport). Core tools are stdlib-only;
als_semantic_search needs repo .venv deps (numpy + sentence-transformers).

Cursor MCP config snippet (user settings):

  {
    "mcpServers": {
      "als-map": {
        "command": "/ABS/PATH/TO/als_graph/.venv/bin/python",
        "args": ["/ABS/PATH/TO/als_graph/scripts/als_map_mcp.py"],
        "env": {}
      }
    }
  }

Optional env: ALS_MAP_DB=/path/to/als_map.sqlite
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from als_map_lib import (  # noqa: E402
    DEFAULT_DB,
    browse,
    contrast,
    get_many,
    get_one,
    open_db,
    sample,
    search,
    semantic_search,
    stats,
    style_stats,
    thread,
)

SERVER_NAME = "als-map"
SERVER_VERSION = "0.3.0"
PROTOCOL_VERSION = "2024-11-05"

# Log to stderr only — stdout is the MCP transport.
def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def db_path() -> Path:
    env = os.environ.get("ALS_MAP_DB")
    return Path(env) if env else DEFAULT_DB


TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": "als_search",
        "description": (
            "Keyword search over Austen’s X + Grok corpus (FTS5). "
            "Default returns cite fields + short snippet (not full text). "
            "Hard limit ≤50. Use als_get / als_get_many for full text."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "FTS5 query string"},
                "sources": {
                    "type": "string",
                    "description": "Comma-separated sources, e.g. x_post,x_reply,grok_chat",
                },
                "role": {"type": "string", "description": "Filter role (human|assistant|self)"},
                "since": {"type": "string", "description": "ISO-8601 lower bound (inclusive)"},
                "until": {"type": "string", "description": "ISO-8601 upper bound (exclusive)"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "offset": {"type": "integer", "minimum": 0},
                "max_chars": {
                    "type": "integer",
                    "description": "Snippet char cap (default 400, max 2000)",
                },
                "full_text": {
                    "type": "boolean",
                    "description": "If true, return truncated text instead of snippet",
                },
                "include_meta": {"type": "boolean"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "als_get",
        "description": "Fetch one record by id with full text (capped).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "max_chars": {
                    "type": "integer",
                    "description": "Text char cap (default 32000, max 64000)",
                },
                "include_meta": {"type": "boolean", "default": True},
            },
            "required": ["id"],
        },
    },
    {
        "name": "als_get_many",
        "description": "Batch fetch by ids (≤50) with full text (capped).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 50,
                },
                "max_chars": {"type": "integer"},
                "include_meta": {"type": "boolean"},
            },
            "required": ["ids"],
        },
    },
    {
        "name": "als_thread",
        "description": (
            "Ordered records for a thread_id (Grok conversation or X thread key). "
            "Default window ≤30 turns; use offset to page."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "thread_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "offset": {"type": "integer", "minimum": 0},
                "max_chars": {"type": "integer"},
                "include_meta": {"type": "boolean"},
            },
            "required": ["thread_id"],
        },
    },
    {
        "name": "als_browse",
        "description": (
            "Keywordless chronological browse over a time window. "
            "Use for timeline slices when you do not know the magic keyword."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "since": {"type": "string"},
                "until": {"type": "string"},
                "sources": {"type": "string"},
                "role": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "offset": {"type": "integer", "minimum": 0},
                "max_chars": {"type": "integer"},
                "include_meta": {"type": "boolean"},
            },
        },
    },
    {
        "name": "als_stats",
        "description": "Index orientation: record counts and optional breakdown.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "breakdown": {
                    "type": "string",
                    "enum": ["source", "role", "month", "both", "none"],
                    "default": "source",
                },
            },
        },
    },
    {
        "name": "als_sample",
        "description": (
            "Prior-free random sample of records (snippet-shaped like als_search). "
            "Optional stratification by time (year-month) or thread_length. "
            "source accepts x|grok shorthand or specific source names. n≤50."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "n": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
                "role": {"type": "string", "description": "Filter role (human|assistant|self)"},
                "source": {
                    "type": "string",
                    "description": "x|grok shorthand, or comma-separated sources",
                },
                "stratify_by": {
                    "type": "string",
                    "enum": ["none", "time", "thread_length"],
                    "default": "none",
                },
                "seed": {
                    "type": "integer",
                    "description": "RNG seed for reproducible samples",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Snippet char cap (default 400, max 2000)",
                },
            },
        },
    },
    {
        "name": "als_style_stats",
        "description": (
            "Cheap deterministic style aggregates for a filtered slice: count, "
            "mean/median char & word length, question-mark density, follow-up share, "
            "opening bigrams/trigrams (human role by default), approximate register markers. "
            "No full-text dump."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": "x|grok shorthand, or comma-separated sources",
                },
                "role": {"type": "string"},
                "since": {"type": "string", "description": "ISO-8601 lower bound (inclusive)"},
                "until": {"type": "string", "description": "ISO-8601 upper bound (exclusive)"},
            },
        },
    },
    {
        "name": "als_contrast",
        "description": (
            "Compare two slices (source_a vs source_b and/or time windows) on the same "
            "metrics as als_style_stats, with delta_b_minus_a columns."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source_a": {"type": "string"},
                "source_b": {"type": "string"},
                "since_a": {"type": "string"},
                "until_a": {"type": "string"},
                "since_b": {"type": "string"},
                "until_b": {"type": "string"},
                "role": {
                    "type": "string",
                    "description": "Shared role filter for both slices",
                },
                "role_a": {"type": "string"},
                "role_b": {"type": "string"},
            },
        },
    },
    {
        "name": "als_semantic_search",
        "description": (
            "Local embedding nearest-neighbor search over the corpus "
            "(sentence-transformers; no remote embed API). "
            "Returns cite fields + snippet + cosine score. Hard limit ≤50. "
            "Use for concept queries that FTS keywords miss."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language query"},
                "sources": {
                    "type": "string",
                    "description": "Comma-separated sources, e.g. x_post,x_reply,grok_chat",
                },
                "source": {
                    "type": "string",
                    "description": "Alias of sources; also accepts x|grok shorthand",
                },
                "role": {"type": "string", "description": "Filter role (human|assistant|self)"},
                "since": {"type": "string", "description": "ISO-8601 lower bound (inclusive)"},
                "until": {"type": "string", "description": "ISO-8601 upper bound (exclusive)"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "max_chars": {
                    "type": "integer",
                    "description": "Snippet char cap (default 400, max 2000)",
                },
            },
            "required": ["query"],
        },
    },
]


def _tool_result(payload: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    return {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
        "structuredContent": payload,
    }


def call_tool(name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    args = arguments or {}
    try:
        conn = open_db(db_path())
    except FileNotFoundError as exc:
        return _tool_result(
            {
                "error": str(exc),
                "hint": "run: python scripts/build_map_index.py",
            },
            is_error=True,
        )

    try:
        with conn:
            if name == "als_search":
                payload = search(
                    conn,
                    str(args.get("query") or ""),
                    sources=args.get("sources"),
                    role=args.get("role"),
                    since=args.get("since"),
                    until=args.get("until"),
                    limit=args.get("limit"),
                    offset=int(args.get("offset") or 0),
                    max_chars=args.get("max_chars"),
                    full_text=bool(args.get("full_text")),
                    include_meta=bool(args.get("include_meta")),
                )
            elif name == "als_get":
                payload = get_one(
                    conn,
                    str(args.get("id") or ""),
                    max_chars=args.get("max_chars"),
                    include_meta=bool(args.get("include_meta", True)),
                )
            elif name == "als_get_many":
                ids = args.get("ids") or []
                if not isinstance(ids, list):
                    raise ValueError("ids must be an array of strings")
                payload = get_many(
                    conn,
                    [str(i) for i in ids],
                    max_chars=args.get("max_chars"),
                    include_meta=bool(args.get("include_meta")),
                )
            elif name == "als_thread":
                payload = thread(
                    conn,
                    str(args.get("thread_id") or ""),
                    limit=args.get("limit"),
                    offset=int(args.get("offset") or 0),
                    max_chars=args.get("max_chars"),
                    include_meta=bool(args.get("include_meta")),
                )
            elif name == "als_browse":
                payload = browse(
                    conn,
                    since=args.get("since"),
                    until=args.get("until"),
                    sources=args.get("sources"),
                    role=args.get("role"),
                    limit=args.get("limit"),
                    offset=int(args.get("offset") or 0),
                    max_chars=args.get("max_chars"),
                    include_meta=bool(args.get("include_meta")),
                )
            elif name == "als_stats":
                payload = stats(conn, breakdown=args.get("breakdown") or "source")
            elif name == "als_sample":
                seed = args.get("seed")
                payload = sample(
                    conn,
                    n=args.get("n"),
                    role=args.get("role"),
                    source=args.get("source") or args.get("sources"),
                    stratify_by=args.get("stratify_by") or "none",
                    seed=None if seed is None else int(seed),
                    max_chars=args.get("max_chars"),
                )
            elif name == "als_style_stats":
                payload = style_stats(
                    conn,
                    source=args.get("source") or args.get("sources"),
                    role=args.get("role"),
                    since=args.get("since"),
                    until=args.get("until"),
                )
            elif name == "als_contrast":
                payload = contrast(
                    conn,
                    source_a=args.get("source_a"),
                    source_b=args.get("source_b"),
                    since_a=args.get("since_a"),
                    until_a=args.get("until_a"),
                    since_b=args.get("since_b"),
                    until_b=args.get("until_b"),
                    role=args.get("role"),
                    role_a=args.get("role_a"),
                    role_b=args.get("role_b"),
                )
            elif name == "als_semantic_search":
                payload = semantic_search(
                    conn,
                    str(args.get("query") or ""),
                    sources=args.get("source") or args.get("sources"),
                    role=args.get("role"),
                    since=args.get("since"),
                    until=args.get("until"),
                    limit=args.get("limit"),
                    max_chars=args.get("max_chars"),
                )
            else:
                return _tool_result({"error": f"unknown tool: {name}"}, is_error=True)
        return _tool_result(payload)
    except ValueError as exc:
        return _tool_result({"error": str(exc)}, is_error=True)
    except Exception as exc:  # noqa: BLE001 — surface to MCP client
        log(traceback.format_exc())
        return _tool_result({"error": f"{type(exc).__name__}: {exc}"}, is_error=True)


def handle_message(msg: dict[str, Any]) -> dict[str, Any] | None:
    """Handle one JSON-RPC request; return response or None for notifications."""
    method = msg.get("method")
    msg_id = msg.get("id", None)
    params = msg.get("params") or {}

    # Notifications (no id) — acknowledge silently
    if msg_id is None and method is not None:
        if method == "notifications/initialized":
            return None
        return None

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"tools": TOOL_DEFS},
        }

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        result = call_tool(str(name), arguments if isinstance(arguments, dict) else {})
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def _read_message() -> dict[str, Any] | None:
    """Read one MCP stdio message (Content-Length framed or newline-delimited JSON)."""
    # Prefer Content-Length framing; also accept NDJSON for simple smoke tests.
    header_line = sys.stdin.buffer.readline()
    if not header_line:
        return None
    # NDJSON path: line starts with '{'
    if header_line.lstrip().startswith(b"{"):
        return json.loads(header_line.decode("utf-8"))

    headers: dict[str, str] = {}
    line = header_line
    while True:
        if not line:
            return None
        text = line.decode("utf-8", errors="replace").strip()
        if text == "":
            break
        if ":" in text:
            k, v = text.split(":", 1)
            headers[k.strip().lower()] = v.strip()
        line = sys.stdin.buffer.readline()

    length_s = headers.get("content-length")
    if not length_s:
        raise ValueError("missing Content-Length header")
    length = int(length_s)
    body = sys.stdin.buffer.read(length)
    if len(body) < length:
        return None
    return json.loads(body.decode("utf-8"))


def _write_message(msg: dict[str, Any]) -> None:
    raw = json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii"))
    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()


def main() -> int:
    log(f"{SERVER_NAME} {SERVER_VERSION} starting; db={db_path()}")
    while True:
        try:
            msg = _read_message()
        except Exception as exc:  # noqa: BLE001
            log(f"read error: {exc}")
            continue
        if msg is None:
            break
        try:
            resp = handle_message(msg)
        except Exception as exc:  # noqa: BLE001
            log(traceback.format_exc())
            if "id" in msg:
                _write_message(
                    {
                        "jsonrpc": "2.0",
                        "id": msg.get("id"),
                        "error": {"code": -32603, "message": str(exc)},
                    }
                )
            continue
        if resp is not None:
            _write_message(resp)
    log(f"{SERVER_NAME} exiting")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
