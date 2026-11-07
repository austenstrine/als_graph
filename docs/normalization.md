# ALS corpus normalization

Uniform JSONL records for retrieval. Top-level fields are stable across sources; `meta` may grow per source.

## Record schema

One JSON object per line (one message/turn):

| Field | Type | Notes |
|-------|------|--------|
| `id` | string | Stable unique id (Grok: response `_id`, else `{conversation_id}:{index}`) |
| `source` | string | `grok_chat` today; future: `x_post`, `x_like`, `x_reply`, … |
| `created_at` | string \| null | ISO-8601 UTC (`…Z`) when available |
| `url` | null | Reserved; Grok export has no public URL (omit-or-null) |
| `thread_id` | string | Conversation / thread id |
| `role` | string | `human` \| `assistant` \| `system` \| other raw values |
| `text` | string | Message body |
| `meta` | object | Source-specific extras (see below) |

### `meta` for `source: grok_chat`

- `title` — conversation title  
- `conversation_create_time` / `conversation_modify_time`  
- `sender_raw` — export sender before role mapping (`human`, `assistant`, `ASSISTANT`, `model`, …)  
- `model` — model id when present (`grok-3`, `grok-4`, …) else `null`  
- `parent_response_id`, `response_index`  
- `workspace_id`, `project_name`, `project_create_time` — when linked to a Grok project  
- `creation_source`, `starred`, `temporary`  
- `asset_ids`, `root_asset_id` — when present  
- `share_link` — when present  

Role mapping: `ASSISTANT` / `model` / `user` → `assistant` / `human`; raw value kept in `meta.sender_raw`.

Timestamps in the export often use Mongo extended JSON (`{"$date":{"$numberLong":"…"}}` ms); the normalizer converts them to ISO-8601 Z.

## Grok half — how to re-run

Input (read-only; do not modify):

```text
10b2e090-7b79-4786-bf86-6e2da0cd437e/ttl/30d/export_data/d9fcc965-ed34-4cb2-aad0-45f6f9a550fd/prod-grok-backend.json
```

```bash
pip install -r requirements.txt
python scripts/normalize_grok.py
# optional flags:
#   --input PATH --output PATH --stats PATH --progress-every 50 --no-stats
```

Outputs:

- `data/normalized/grok.jsonl` — one record per turn  
- `data/normalized/grok-stats.json` — conversation/record counts, roles, models  

The script streams with `ijson` (does not load the ~287 MB file into memory). Progress prints every N conversations.

## Extension point (X archive — not started)

When a classic X “Download your archive” is available, add a sibling normalizer (e.g. `scripts/normalize_x.py`) that writes the **same top-level fields** with `source: x_post` / `x_like` / … into `data/normalized/x.jsonl` (or append to a combined corpus). Do not overload `normalize_grok.py` with X parsing.

See also the completeness audit: Grok chats are in this export; classic X activity is not.
