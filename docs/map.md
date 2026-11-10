# Total ALS Map (Phase 1′)

Queryable personal corpus: **X posts/replies/reposts/likes/notes + every Grok turn** from the normalized JSONL, indexed in local SQLite FTS5 for **retrieval-backed analysis by Grok** (via Cursor MCP tools).

This is a **retrieval map substrate**, not a UI/graph/embeddings product.

## Corpus boundary (Total v1)

| In | Out |
|----|-----|
| Normalized `grok.jsonl` + `x.jsonl` | DMs, media blobs, live X/Grok API sync |
| Agent tools over the index | Marketing UI, embeddings, opinion graph |

## Build the index

Prereq: normalized JSONL already present (`python scripts/normalize_grok.py` / `normalize_x.py`).

```bash
python scripts/build_map_index.py
# rebuild even if inputs unchanged:
python scripts/build_map_index.py --force
```

Writes (gitignored under `data/index/`):

- `data/index/als_map.sqlite` — records + **external-content FTS5**
- `data/index/als_map-stats.json` — optional sidecar (counts, fingerprint, elapsed)

Rebuild is skipped when the input fingerprint (paths + sizes + mtimes + schema version) matches the value stored in `map_meta`.

Expected: ~48,334 rows; build well under a minute on this machine.

## Debug CLI (not the product)

```bash
python scripts/map_query.py stats
python scripts/map_query.py search "ordo amoris" --source x_post,x_reply --limit 10
python scripts/map_query.py get <id>
python scripts/map_query.py get-many id1 id2
python scripts/map_query.py thread <thread_id>
python scripts/map_query.py browse --since 2024-06-01T00:00:00Z --until 2024-07-01T00:00:00Z
```

Search defaults to **cite fields + snippet**. Pass `--full` for truncated body text.

## MCP server (product surface)

Stdio MCP, stdlib JSON-RPC (no `mcp` PyPI package required):

```bash
python scripts/als_map_mcp.py
```

Optional: `ALS_MAP_DB=/path/to/als_map.sqlite`.

### Cursor MCP config

Add to Cursor MCP settings (path may vary by machine):

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

### Tools

| Tool | Role |
|------|------|
| `als_search` | FTS; snippet-default; filters `sources` / `role` / `since` / `until`; limit ≤50 |
| `als_get` | Full text by id (char-capped) |
| `als_get_many` | Batch get ≤50 ids |
| `als_thread` | Ordered turns for `thread_id` (default window ≤30) |
| `als_browse` | Keywordless chronological window |
| `als_stats` | Counts; breakdown `source` \| `role` \| `month` \| `both` |

Every hit includes cite fields: `id`, `source`, `created_at`, `url`, `thread_id`, `role`.

### Caps (token burn)

| Cap | Default | Hard max |
|-----|--------:|---------:|
| Row `limit` | 20–30 by op | 50 |
| Search snippet | 400 chars | 2 000 |
| Full text (`get` / thread) | 32 000 chars | 64 000 |
| JSON response | — | ~64 KB |

Primary ongoing cost is **agent/Grok context tokens**, not index CPU.

## Layout

| Path | Purpose |
|------|---------|
| `scripts/als_map_lib.py` | Shared build + query library |
| `scripts/build_map_index.py` | JSONL → sqlite |
| `scripts/map_query.py` | Debug CLI |
| `scripts/als_map_mcp.py` | Stdio MCP server |
| `data/index/` | Generated index (gitignored) |
