#!/usr/bin/env python3
"""
Build data/index/als_map.sqlite from normalized Grok + X JSONL.

Uses external-content FTS5 (one text body + posting lists).
Skips rebuild when input fingerprints match (unless --force).

Usage:
  python scripts/build_map_index.py
  python scripts/build_map_index.py --force
  python scripts/build_map_index.py --grok PATH --x PATH --db PATH --stats PATH
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow `python scripts/build_map_index.py` without installing a package.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from als_map_lib import (  # noqa: E402
    DEFAULT_DB,
    DEFAULT_GROK,
    DEFAULT_STATS,
    DEFAULT_X,
    build_index,
)


def main() -> int:
    ap = argparse.ArgumentParser(description="Build ALS map SQLite FTS index")
    ap.add_argument("--grok", type=Path, default=DEFAULT_GROK)
    ap.add_argument("--x", type=Path, default=DEFAULT_X)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument(
        "--stats",
        type=Path,
        default=DEFAULT_STATS,
        help="optional stats sidecar JSON (default: data/index/als_map-stats.json)",
    )
    ap.add_argument("--no-stats", action="store_true", help="skip writing stats sidecar")
    ap.add_argument("--force", action="store_true", help="rebuild even if fingerprint matches")
    ap.add_argument("--progress-every", type=int, default=5000)
    args = ap.parse_args()

    stats_path = None if args.no_stats else args.stats
    try:
        result = build_index(
            grok=args.grok,
            x=args.x,
            db_path=args.db,
            stats_path=stats_path,
            force=args.force,
            progress_every=args.progress_every,
        )
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result.get("skipped"):
        print(
            f"skipped rebuild ({result.get('reason')}); "
            f"{result.get('records')} records in {args.db}",
            file=sys.stderr,
        )
    else:
        print(
            f"built {result.get('records')} records → {args.db} "
            f"in {result.get('elapsed_s')}s",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
