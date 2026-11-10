# ALS graph

Personal corpus tools for Austen Strine (ALS = initials). Normalize Grok chat exports and X archives into uniform JSONL, then index them as a local **retrieval map** for Grok/Cursor (FTS5 + stdio MCP).

## Quick start

```bash
pip install -r requirements.txt
python scripts/normalize_grok.py
python scripts/normalize_x.py
python scripts/build_map_index.py
```

Writes:

- `data/normalized/grok.jsonl` / `x.jsonl` — **local only** (gitignored; large)
- `data/normalized/grok-stats.json` / `x-stats.json` — counts / roles / sources
- `data/index/als_map.sqlite` — FTS map index (**gitignored**)

See [docs/normalization.md](docs/normalization.md) for the record schema and [docs/map.md](docs/map.md) for the map index + MCP tools.

**Do not commit** the xAI export tree, X archive folders (`twitter-*/`), `*.jsonl` corpora, or `data/index/` — they are large and local-only. Only scripts, docs, and small stats sidecars belong in version control.

## Map (Phase 1′)

```bash
# debug CLI
python scripts/map_query.py search "your phrase" --limit 10
python scripts/map_query.py stats

# product: stdio MCP for Cursor
python scripts/als_map_mcp.py

# remote/custom MCP (Streamable HTTP; Bearer token required)
export ALS_MAP_MCP_TOKEN="$(openssl rand -hex 32)"
python scripts/als_map_mcp_http.py   # 127.0.0.1:8787/mcp
```

See [docs/map.md](docs/map.md) for curl smoke, Cloudflare tunnel stub, and Grok/Cursor remote paste shapes. **`mcp.austenstrine.dev` DNS is not ready yet.**

Cursor MCP snippet (stdio):

```json
{
  "mcpServers": {
    "als-map": {
      "command": "python3",
      "args": [
        "/home/austen-loren-strine/Development/als_graph/scripts/als_map_mcp.py"
      ]
    }
  }
}
```

## Layout

| Path | Purpose |
|------|---------|
| `scripts/normalize_grok.py` | Stream `prod-grok-backend.json` → JSONL |
| `scripts/normalize_x.py` | Parse X archive `window.YTD.*` JS → JSONL |
| `scripts/als_map_lib.py` | Shared FTS build/query library |
| `scripts/build_map_index.py` | JSONL → `data/index/als_map.sqlite` |
| `scripts/map_query.py` | Debug CLI (not the product) |
| `scripts/als_map_mcp.py` | Stdio MCP: search / get / thread / browse / stats |
| `scripts/als_map_mcp_http.py` | Streamable HTTP MCP + Bearer auth |
| `docs/normalization.md` | Schema + how to re-run normalizers |
| `docs/map.md` | Map index, caps, MCP how-to (stdio + remote) |
| `docs/cloudflare-tunnel.example.yml` | Cloudflare Tunnel stub (hostname future) |
| `data/normalized/` | Generated JSONL (gitignored) |
| `data/index/` | Generated sqlite index (gitignored) |
| `10b2e090-…/export_data/…` | Raw Grok export (gitignored; read-only) |
| `twitter-*/` | Official X archive (gitignored; read-only) |
