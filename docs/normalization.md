# ALS corpus normalization

Uniform JSONL records for retrieval. Top-level fields are stable across sources; `meta` may grow per source.

## Record schema

One JSON object per line (one message/turn or X interaction):

| Field | Type | Notes |
|-------|------|--------|
| `id` | string | Stable unique id (Grok: response `_id`; X tweet: `id_str`; likes: `like:{tweetId}`; notes: `note:{noteTweetId}`) |
| `source` | string | `grok_chat` · `x_post` · `x_reply` · `x_like` · `x_repost` · `x_note` |
| `created_at` | string \| null | ISO-8601 UTC (`…Z`) when available (likes often null) |
| `url` | string \| null | Public URL when known (X status URLs; Grok export has none) |
| `thread_id` | string | Conversation / thread id (Grok conversation; X: self id for posts/reposts, parent id for replies, liked tweet id for likes) |
| `role` | string \| null | Grok: `human` \| `assistant` \| …; X authored: `self`; X likes: `null` |
| `text` | string | Message / post body |
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

### `meta` for X sources (`x_post` / `x_reply` / `x_repost` / `x_like` / `x_note`)

**Authored tweets** (`x_post`, `x_reply`, `x_repost`):

- `lang`, `favorite_count`, `retweet_count`, `source_client`, `truncated`, `username`
- Reply fields when present: `in_reply_to_status_id`, `in_reply_to_user_id`, `in_reply_to_screen_name`
- `user_mentions`, `media_urls` when non-empty

**Classification:** `full_text` starting with `RT @` → `x_repost`; else `in_reply_to_status_id_str` → `x_reply`; else `x_post`.

**Likes** (`x_like`): `liked_tweet_id`, `expanded_url`, `liker_username`. Archive has no like timestamp → `created_at: null`. `role: null` (liked content is not self-authored).

**Notes** (`x_note`): long-form `note-tweet.js` bodies. `note_tweet_id`, `updated_at`, `lifecycle`. Archive does not join `noteTweetId` → tweet `id_str`.

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

### Grok run stats

| Metric | Count |
|--------|------:|
| Conversations | 1,987 |
| Records (turns) | **14,994** |
| Projects indexed | 7 |
| Conversations with workspace | 77 |
| Empty-text records | 147 |

**Roles:** human 7,467 · assistant 7,527

## X half — how to re-run

Input: official X “Download your archive” folder (unzipped) under the repo root, e.g.:

```text
twitter-2026-11-08-d3b063904b28d3226f33bc4beaa26808e19632023745c6bd0c3eb41cdd96d748/
```

Markers: `Your archive.html`, `data/tweets.js`, `data/like.js`, `data/manifest.js`.

```bash
python scripts/normalize_x.py
# optional:
#   --archive PATH --output PATH --stats PATH --progress-every 2000
#   --include-deleted   # also emit deleted-tweets.js
#   --no-notes          # skip note-tweet.js
```

Stdlib only (parses `window.YTD.*.partN = [...]` wrappers). Progress prints while scanning tweets/likes.

Outputs:

- `data/normalized/x.jsonl` — one record per interaction (~19 MB)  
- `data/normalized/x-stats.json` — per-source counts, section inventory  

### X run stats (@AustenStrine)

| Source | Count |
|--------|------:|
| `x_post` | 2,438 |
| `x_reply` | 7,670 |
| `x_repost` | 9,170 |
| `x_like` | 13,903 |
| `x_note` | 159 |
| **Total** | **33,340** |

Archive sections used: `tweets.js` (19,278 · 34 MB), `like.js` (13,903 · 4.3 MB), `note-tweet.js` (159 · 220 KB). `deleted-tweets.js` present (10) but not emitted by default.

**Roles:** `self` 19,437 · `null` 13,903 (likes).

Export archives + JSONL are **gitignored** so they are never committed. Stats sidecars may be committed.
