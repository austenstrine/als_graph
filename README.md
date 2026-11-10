# ALS graph

Personal corpus tools for Austen Strine (ALS = initials). Normalize Grok chat exports and X archives into uniform JSONL for retrieval.

## Quick start

```bash
pip install -r requirements.txt
python scripts/normalize_grok.py
python scripts/normalize_x.py
```

Writes:

- `data/normalized/grok.jsonl` / `x.jsonl` — **local only** (gitignored; large)
- `data/normalized/grok-stats.json` / `x-stats.json` — counts / roles / sources

See [docs/normalization.md](docs/normalization.md) for the record schema and re-run notes.

**Do not commit** the xAI export tree, X archive folders (`twitter-*/`), or `*.jsonl` corpora — they are hundreds of MB–GB and will lock up git. Only scripts, docs, and small stats sidecars belong in version control.

## Layout

| Path | Purpose |
|------|---------|
| `scripts/normalize_grok.py` | Stream `prod-grok-backend.json` → JSONL |
| `scripts/normalize_x.py` | Parse X archive `window.YTD.*` JS → JSONL |
| `docs/normalization.md` | Schema + how to re-run |
| `data/normalized/` | Generated outputs (JSONL gitignored) |
| `10b2e090-…/export_data/…` | Raw Grok export (gitignored; read-only) |
| `twitter-*/` | Official X archive (gitignored; read-only) |
