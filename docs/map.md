# Total ALS Map (Phase 1′ + 2′a + 2′b + 2′c)

Queryable personal corpus: **X posts/replies/reposts/likes/notes + every Grok turn** from the normalized JSONL, indexed in local SQLite FTS5 (+ optional local embeddings) for **retrieval-backed analysis by Grok** (via Cursor MCP tools).

This is a **retrieval map substrate**, not a UI/graph product. Phase **2′a** adds prior-free sample / style / contrast. Phase **2′b** adds **local-only** semantic search (no corpus upload / no remote embed API). Phase **2′c** adds **heuristic thread features** (no LLM labels).

## Corpus boundary (Total v1)

| In | Out |
|----|-----|
| Normalized `grok.jsonl` + `x.jsonl` | DMs, media blobs, live X/Grok API sync |
| Agent tools over the index | Marketing UI, opinion graph, cloud-hosted vectors |

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

## Build the embedding index (Phase 2′b)

Local sentence-transformers on Raider (CUDA when available). **No remote embed API; corpus stays on disk.**

| Setting | Value |
|---------|-------|
| Model | `sentence-transformers/all-MiniLM-L6-v2` (384-d) |
| Chunking | whole message; truncated to model max sequence length |
| Index paths | `data/index/als_map_embeddings.npz` + `als_map_embeddings.meta.json` |
| Disk hard limit | **74815478169** bytes for vector-index artifacts (warn + refuse before exceed; do not pre-claim) |
| RAM guidance | ~28324106240 bytes working-set guidance (not pre-allocated) |

Prereq: FTS index built; repo `.venv` with embed deps:

```bash
uv venv .venv --python python3.12
uv pip install --python .venv/bin/python -r requirements-embed.txt
# optional CUDA torch:
# uv pip install --python .venv/bin/python torch --index-url https://download.pytorch.org/whl/cu124

.venv/bin/python scripts/build_map_embeddings.py
.venv/bin/python scripts/build_map_embeddings.py --force
```

Fingerprint skip (inputs + model + embed schema) mirrors FTS. Soft timebox: escalate if model download/build exceeds ~90 minutes.

## Build thread features (Phase 2′c)

Heuristic tags stored in `thread_features` inside `als_map.sqlite` (same DB; no LLM). Built automatically by `build_map_index.py`; refresh without FTS rebuild:

```bash
python scripts/build_thread_features.py
python scripts/build_thread_features.py --force
```

Per-thread fields: message counts + human/assistant/self ratio, refine/critique cue counts (regex on `human`|`self`), code/tech density. Documented as approximate in tool output.

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
# Prefer repo .venv so als_semantic_search can load the local model
.venv/bin/python scripts/als_map_mcp.py
```

Optional: `ALS_MAP_DB=/path/to/als_map.sqlite`.

### Cursor MCP config (stdio / local)

Add to Cursor MCP settings (path may vary by machine):

```json
{
  "mcpServers": {
    "als-map": {
      "command": "/ABS/PATH/TO/als_graph/.venv/bin/python",
      "args": [
        "/ABS/PATH/TO/als_graph/scripts/als_map_mcp.py"
      ]
    }
  }
}
```

## Remote MCP (Streamable HTTP)

Same tools and caps over **Streamable HTTP** (`POST`/`GET` on `/mcp`), with **Bearer auth required**. Stdlib server — no FastAPI.

```bash
# generate a token (do not commit it)
export ALS_MAP_MCP_TOKEN="$(openssl rand -hex 32)"
# optional: ALS_MAP_MCP_HOST=127.0.0.1 ALS_MAP_MCP_PORT=8787 ALS_MAP_DB=…
python scripts/als_map_mcp_http.py
```

- Default bind: `127.0.0.1:8787` (non-loopback hosts refused unless `ALS_MAP_MCP_ALLOW_NONLOCAL=1`)
- MCP endpoint: `http://127.0.0.1:8787/mcp`
- Liveness (no auth): `http://127.0.0.1:8787/health`
- Every `/mcp` request needs `Authorization: Bearer $ALS_MAP_MCP_TOKEN`
- Auth uses timing-safe compare; request bodies capped at 1 MiB; keep the origin on loopback and put Cloudflare Tunnel in front when public

### Curl smoke

```bash
# 401 without token
curl -sS -o /tmp/als_mcp_401.json -w "%{http_code}\n" \
  -X POST http://127.0.0.1:8787/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'

# initialize + tools/list with token
curl -sS http://127.0.0.1:8787/mcp \
  -H "Authorization: Bearer $ALS_MAP_MCP_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'

curl -sS http://127.0.0.1:8787/mcp \
  -H "Authorization: Bearer $ALS_MAP_MCP_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
```

### Stack launcher (Raider)

One command for HTTP MCP + Cloudflare tunnel (systemd user units), status panel, access log, desktop icon:

```bash
scripts/als_map_stack start          # or: status | stop | restart | probe | logs -f | doctor | watch
scripts/als_map_stack install-desktop
```

Details: [`docs/als-map-stack.md`](als-map-stack.md). Access log: `~/.local/state/als_map/access.log` (no bearer values). Token: `~/.config/als_map/mcp_token`.

### Cloudflare Tunnel

Live hostname: **`https://mcp.austenstrine.dev`** (tunnel UUID managed under `~/.cloudflared/`, not in git). Example stub: [`docs/cloudflare-tunnel.example.yml`](cloudflare-tunnel.example.yml).

Keep `ALS_MAP_MCP_TOKEN` only in `~/.config/als_map/` / process env — never in git.

### Grok / Cursor remote MCP

| Field | Value |
|-------|-------|
| URL | `https://mcp.austenstrine.dev/mcp` (local: `http://127.0.0.1:8787/mcp`) |
| Header | `Authorization: Bearer <ALS_MAP_MCP_TOKEN>` |
| Transport | Streamable HTTP (JSON responses supported) |

```json
{
  "mcpServers": {
    "als-map-remote": {
      "url": "https://mcp.austenstrine.dev/mcp",
      "headers": {
        "Authorization": "Bearer ${ALS_MAP_MCP_TOKEN}"
      }
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
| `als_sample` | Prior-free random sample; `n`≤50; `role?` / `source?` (`x`\|`grok` or specific); `stratify_by` `none`\|`time`\|`thread_length`; `seed?`; optional thread-feature filters (`min_thread_length` / `has_refine` / `has_critique` / `min_code_density`) |
| `als_style_stats` | Deterministic style aggregates for a slice (`source?` / `role?` / `since?` / `until?`) |
| `als_contrast` | Two-slice compare (sources and/or time windows) with deltas on the same metrics |
| `als_semantic_search` | Local embedding NN search; `query`; filters `source(s)?` / `role?` / `since?` / `until?`; limit ≤50 |
| `als_thread_features` | Heuristic per-thread features (no LLM); `thread_id?` or filter/list (`source?` / length / refine / critique / density); limit ≤50 |

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
| `scripts/als_map_lib.py` | Shared build + query library (FTS + embeddings + thread features) |
| `scripts/build_map_index.py` | JSONL → sqlite |
| `scripts/build_map_embeddings.py` | sqlite → local embedding `.npz` |
| `scripts/build_thread_features.py` | sqlite → heuristic `thread_features` table |
| `scripts/map_query.py` | Debug CLI |
| `scripts/als_map_mcp.py` | Stdio MCP server |
| `scripts/als_map_mcp_http.py` | Streamable HTTP MCP + Bearer auth |
| `scripts/als_map_stack` | One-command start/stop/status/probe/logs/doctor + desktop launcher |
| `requirements-embed.txt` | Optional Phase 2′b venv deps |
| `assets/als-map-mcp.svg` | Desktop icon for ALS Map MCP |
| `docs/cloudflare-tunnel.example.yml` | Tunnel stub (DNS not ready) |
| `docs/embed-disk-budget.md` | Raider vector-index disk/RAM caps |
| `data/index/` | Generated FTS + embedding index (gitignored) |
| `.venv/` | Local Python env for embed + MCP HTTP (gitignored) |
