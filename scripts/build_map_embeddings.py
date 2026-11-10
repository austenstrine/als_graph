#!/usr/bin/env python3
"""
Build local embedding vectors for the Total ALS Map (Phase 2′b).

Reads records from data/index/als_map.sqlite, embeds with a local
sentence-transformers model (no corpus upload / no remote embed API),
writes beside the FTS index:

  data/index/als_map_embeddings.npz
  data/index/als_map_embeddings.meta.json

Skips rebuild when the embed fingerprint matches (unless --force).
Respects the Raider vector-index disk hard limit (see docs/embed-disk-budget.md).

Usage:
  .venv/bin/python scripts/build_map_embeddings.py
  .venv/bin/python scripts/build_map_embeddings.py --force
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from als_map_lib import (  # noqa: E402
    DEFAULT_DB,
    DEFAULT_EMBED_BATCH,
    DEFAULT_EMBED_META,
    DEFAULT_EMBED_MODEL,
    DEFAULT_EMBEDDINGS,
    DEFAULT_GROK,
    DEFAULT_X,
    EMBED_DISK_HARD_LIMIT,
    EMBED_RAM_GUIDANCE,
    build_embeddings,
)


SOFT_TIMEBOX_S = 90 * 60


def main() -> int:
    ap = argparse.ArgumentParser(description="Build ALS map local embedding index")
    ap.add_argument("--grok", type=Path, default=DEFAULT_GROK)
    ap.add_argument("--x", type=Path, default=DEFAULT_X)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--embeddings", type=Path, default=DEFAULT_EMBEDDINGS)
    ap.add_argument("--meta", type=Path, default=DEFAULT_EMBED_META)
    ap.add_argument("--model", default=DEFAULT_EMBED_MODEL)
    ap.add_argument("--batch-size", type=int, default=DEFAULT_EMBED_BATCH)
    ap.add_argument("--progress-every", type=int, default=2000)
    ap.add_argument("--force", action="store_true")
    ap.add_argument(
        "--disk-hard-limit",
        type=int,
        default=EMBED_DISK_HARD_LIMIT,
        help="refuse write if projected index bytes would exceed this",
    )
    args = ap.parse_args()

    print(
        f"disk hard limit={args.disk_hard_limit} B; "
        f"RAM guidance={EMBED_RAM_GUIDANCE} B (not pre-claimed)",
        file=sys.stderr,
        flush=True,
    )
    t0 = time.perf_counter()
    try:
        result = build_embeddings(
            grok=args.grok,
            x=args.x,
            db_path=args.db,
            embeddings_path=args.embeddings,
            meta_path=args.meta,
            model_name=args.model,
            force=args.force,
            batch_size=args.batch_size,
            progress_every=args.progress_every,
            disk_hard_limit=args.disk_hard_limit,
        )
    except (FileNotFoundError, ValueError, OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    elapsed = time.perf_counter() - t0
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result.get("skipped"):
        print(
            f"skipped rebuild ({result.get('reason')}); "
            f"{result.get('records')} vectors in {args.embeddings}",
            file=sys.stderr,
        )
    else:
        print(
            f"built {result.get('records')} vectors → {args.embeddings} "
            f"({result.get('bytes')} bytes) in {result.get('elapsed_s')}s",
            file=sys.stderr,
        )
    if elapsed > SOFT_TIMEBOX_S:
        print(
            f"SOFT TIMEBOX: embed run took {elapsed / 60:.1f}m (>90m); escalate with progress notes",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
