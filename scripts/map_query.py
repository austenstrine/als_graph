#!/usr/bin/env python3
"""
Debug CLI for the ALS map — thin wrapper over als_map_lib (not the product).

The product surface is the stdio MCP server (scripts/als_map_mcp.py).

Usage:
  python scripts/map_query.py stats
  python scripts/map_query.py search "ordo amoris" --source x_post,x_reply
  python scripts/map_query.py get <id>
  python scripts/map_query.py get-many id1 id2
  python scripts/map_query.py thread <thread_id>
  python scripts/map_query.py browse --since 2024-01-01 --until 2024-02-01
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from als_map_lib import (  # noqa: E402
    DEFAULT_DB,
    browse,
    get_many,
    get_one,
    open_db,
    search,
    stats,
    thread,
)


def _print(payload: dict) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def main() -> int:
    ap = argparse.ArgumentParser(description="ALS map debug CLI")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_search = sub.add_parser("search", help="FTS search (snippet-default)")
    p_search.add_argument("query")
    p_search.add_argument("--source", "--sources", dest="sources", default=None)
    p_search.add_argument("--role", default=None)
    p_search.add_argument("--since", default=None)
    p_search.add_argument("--until", default=None)
    p_search.add_argument("--limit", type=int, default=None)
    p_search.add_argument("--offset", type=int, default=0)
    p_search.add_argument("--max-chars", type=int, default=None)
    p_search.add_argument(
        "--full",
        action="store_true",
        help="return truncated full text instead of snippet",
    )
    p_search.add_argument("--meta", action="store_true")

    p_get = sub.add_parser("get", help="full text by id")
    p_get.add_argument("id")
    p_get.add_argument("--max-chars", type=int, default=None)
    p_get.add_argument("--no-meta", action="store_true")

    p_many = sub.add_parser("get-many", help="batch full text")
    p_many.add_argument("ids", nargs="+")
    p_many.add_argument("--max-chars", type=int, default=None)
    p_many.add_argument("--meta", action="store_true")

    p_thread = sub.add_parser("thread", help="ordered records for thread_id")
    p_thread.add_argument("thread_id")
    p_thread.add_argument("--limit", type=int, default=None)
    p_thread.add_argument("--offset", type=int, default=0)
    p_thread.add_argument("--max-chars", type=int, default=None)
    p_thread.add_argument("--meta", action="store_true")

    p_browse = sub.add_parser("browse", help="keywordless time window")
    p_browse.add_argument("--since", default=None)
    p_browse.add_argument("--until", default=None)
    p_browse.add_argument("--source", "--sources", dest="sources", default=None)
    p_browse.add_argument("--role", default=None)
    p_browse.add_argument("--limit", type=int, default=None)
    p_browse.add_argument("--offset", type=int, default=0)
    p_browse.add_argument("--max-chars", type=int, default=None)
    p_browse.add_argument("--meta", action="store_true")

    p_stats = sub.add_parser("stats", help="index counts")
    p_stats.add_argument(
        "--breakdown",
        choices=("source", "role", "month", "both", "none"),
        default="source",
    )

    args = ap.parse_args()
    try:
        conn = open_db(args.db)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("run: python scripts/build_map_index.py", file=sys.stderr)
        return 1

    with conn:
        if args.cmd == "search":
            try:
                payload = search(
                    conn,
                    args.query,
                    sources=args.sources,
                    role=args.role,
                    since=args.since,
                    until=args.until,
                    limit=args.limit,
                    offset=args.offset,
                    max_chars=args.max_chars,
                    full_text=args.full,
                    include_meta=args.meta,
                )
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
        elif args.cmd == "get":
            payload = get_one(
                conn,
                args.id,
                max_chars=args.max_chars,
                include_meta=not args.no_meta,
            )
        elif args.cmd == "get-many":
            payload = get_many(
                conn,
                args.ids,
                max_chars=args.max_chars,
                include_meta=args.meta,
            )
        elif args.cmd == "thread":
            payload = thread(
                conn,
                args.thread_id,
                limit=args.limit,
                offset=args.offset,
                max_chars=args.max_chars,
                include_meta=args.meta,
            )
        elif args.cmd == "browse":
            payload = browse(
                conn,
                since=args.since,
                until=args.until,
                sources=args.sources,
                role=args.role,
                limit=args.limit,
                offset=args.offset,
                max_chars=args.max_chars,
                include_meta=args.meta,
            )
        elif args.cmd == "stats":
            payload = stats(conn, breakdown=args.breakdown)
        else:
            print(f"unknown command: {args.cmd}", file=sys.stderr)
            return 1

    _print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
