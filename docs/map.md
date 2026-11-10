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

### Cursor MCP config (stdio / local)

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
scripts/als_map_stack start          # or: status | stop | restart | logs -f | doctor | watch
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
| `scripts/als_map_mcp_http.py` | Streamable HTTP MCP + Bearer auth |
| `scripts/als_map_stack` | One-command start/stop/status/logs/doctor + desktop launcher |
| `assets/als-map-mcp.svg` | Desktop icon for ALS Map MCP |
| `docs/cloudflare-tunnel.example.yml` | Tunnel stub (DNS not ready) |
| `data/index/` | Generated index (gitignored) |
