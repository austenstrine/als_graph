#!/usr/bin/env python3
"""
Build / refresh the Phase 2′c thread_features table in als_map.sqlite.

Heuristic only (no LLM). Skips when schema + records fingerprint match
unless --force.

Usage:
  python scripts/build_thread_features.py
  python scripts/build_thread_features.py --force
  python scripts/build_thread_features.py --db PATH
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from als_map_lib import (  # noqa: E402
    DEFAULT_DB,
    build_thread_features,
    connect,
)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Build ALS map heuristic thread_features table"
    )
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if not args.db.is_file():
        print(f"error: index missing: {args.db}", file=sys.stderr)
        print("run: python scripts/build_map_index.py", file=sys.stderr)
        return 1

    try:
        with connect(args.db, readonly=False) as conn:
            result = build_thread_features(conn, force=args.force)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result.get("skipped"):
        print(
            f"skipped rebuild ({result.get('reason')}); "
            f"{result.get('threads')} threads in {args.db}",
            file=sys.stderr,
        )
    else:
        print(
            f"built {result.get('threads')} threads → {args.db} "
            f"in {result.get('elapsed_s')}s",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
