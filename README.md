# ALS graph

Personal corpus tools for Austen Strine (ALS = initials). Normalize Grok chat exports (and later X archive) into uniform JSONL for retrieval.

## Quick start (Grok half)

```bash
pip install -r requirements.txt
python scripts/normalize_grok.py
```

Writes:

- `data/normalized/grok.jsonl` — **local only** (gitignored; large)
- `data/normalized/grok-stats.json` — counts / roles / models

See [docs/normalization.md](docs/normalization.md) for the record schema and re-run notes.

**Do not commit** the xAI export tree or `*.jsonl` corpora — they are hundreds of MB and will lock up git. Only scripts, docs, and small stats sidecars belong in version control.

## Layout

| Path | Purpose |
|------|---------|
| `scripts/normalize_grok.py` | Stream `prod-grok-backend.json` → JSONL |
| `docs/normalization.md` | Schema + how to re-run |
| `data/normalized/` | Generated outputs (JSONL gitignored) |
| `10b2e090-…/export_data/…` | Raw export (gitignored; read-only) |

## Future: X posts

Classic X “Download your archive” is **not** in the current export. Add a sibling normalizer with `source: x_post` (etc.) when that archive is available — same top-level fields as Grok records.
