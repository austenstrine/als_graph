#!/usr/bin/env python3
"""
stdio MCP server for the Total ALS Map (Phase 1′ + 2′a + 2′b + 2′c surface).

Tools (hard-capped via als_map_lib):
  als_search, als_get, als_get_many, als_thread, als_browse, als_stats,
  als_sample, als_style_stats, als_contrast, als_semantic_search,
  als_thread_features

Plus named Google Takeout/Gmail/Drive/Calendar/Contacts/Photos stubs
(and a few letter expansion stubs). Same single ALS map index — future
rows use sources like gmail / drive / calendar / contacts / photos.
Stubs are self-describing and unimplemented today.

Protocol: MCP over JSON-RPC 2.0 on stdin/stdout (Content-Length framing
compatible with the MCP stdio transport). Core tools are stdlib-only;
als_semantic_search needs repo .venv deps (numpy + sentence-transformers).

Cursor MCP config snippet (user settings):

  {
    "mcpServers": {
      "als-map": {
        "command": "/ABS/PATH/TO/als_graph/.venv/bin/python",
        "args": ["/ABS/PATH/TO/als_graph/scripts/als_map_mcp.py"],
        "env": {}
      }
    }
  }

Optional env: ALS_MAP_DB=/path/to/als_map.sqlite
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from als_map_lib import (  # noqa: E402
    DEFAULT_DB,
    browse,
    contrast,
    get_many,
    get_one,
    open_db,
    sample,
    search,
    semantic_search,
    stats,
    style_stats,
    thread,
    thread_features,
)

SERVER_NAME = "als-map"
SERVER_VERSION = "0.6.0"
PROTOCOL_VERSION = "2024-11-05"

# Working corpus tools already live above; stubs are named future surface on
# the *same* ALS map (one index). When Takeout lands, rows join als_map.sqlite
# with source values such as gmail, drive, calendar, contacts, photos.
STUB_STATUS = "This route does nothing yet."

# Remaining generic expansion slots (not cryptic A–Z; keep a few letters).
EXPANSION_LETTER_STUB_NAMES: tuple[str, ...] = (
    "als_u",
    "als_v",
    "als_w",
    "als_x",
    "als_y",
    "als_z",
)


def _working_tools_hint() -> str:
    return (
        "For today's X+Grok corpus use working tools: `als_search`, `als_get`, "
        "`als_get_many`, `als_thread`, `als_browse`, `als_stats`, `als_sample`, "
        "`als_style_stats`, `als_contrast`, `als_semantic_search`, "
        "`als_thread_features`."
    )


def _named_stub_description(
    *,
    name: str,
    will_do: str,
    once_data: str,
    how_to_call: str,
) -> str:
    """Long zero-context description for an unimplemented named Google stub."""
    return (
        f"`{name}` is a reserved tool on the single ALS Map MCP (one local "
        "index: `als_map.sqlite`). It is not implemented yet — there is no "
        "separate Google map. "
        f"Intended job: {will_do} "
        f"Once Google Takeout / export data is ingested into this same index "
        f"(future `source` values such as gmail, drive, calendar, contacts, "
        f"photos): {once_data} "
        f"When to call today: only to read this self-description or to probe "
        f"whether `{name}` has been filled in. Do not expect mail, Drive, "
        f"calendar, contacts, or photos results yet. {_working_tools_hint()} "
        f"How to call: {how_to_call} "
        "What you get today: JSON whose `status` is exactly "
        f'"{STUB_STATUS}" and `implemented` is false, plus '
        "`how_it_will_work` describing the future path. "
        f"How to call again later: re-invoke `{name}` the same way after the "
        "server/index is updated; if still a stub you get the same message, "
        "and if implemented the description and result shape will change."
    )


def _expansion_stub_description(name: str) -> str:
    letter = name.removeprefix("als_")
    return (
        f"`{name}` is a reserved letter expansion slot on the ALS Map MCP "
        f"(slot `{letter}`; leftover from the old `als_a`…`als_z` bank). "
        "It is a placeholder for a future corpus capability on this same "
        "single index — not a separate map. "
        f"When to call: only to probe whether `{name}` exists or has been "
        f"filled in. Do not call it for real retrieval. {_working_tools_hint()} "
        "How to call: invoke with `{}` or optional `{\"note\": \"…\"}` "
        "(ignored). No fields required. "
        "What you get today: JSON whose `status` is exactly "
        f'"{STUB_STATUS}" and `implemented` is false. '
        f"How to call again later: re-invoke `{name}` the same way after an "
        "update; stub → same message, or a real tool with a new shape."
    )


def _note_property() -> dict[str, Any]:
    return {
        "note": {
            "type": "string",
            "description": (
                "Optional free-text note. Accepted and echoed as "
                "`note_ignored`; does not change behavior. Omit for a bare probe."
            ),
        },
    }


def _named_stub_spec(
    *,
    name: str,
    will_do: str,
    once_data: str,
    how_to_call: str,
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> dict[str, Any]:
    # Properties are advisory for the future shape; nothing is required while
    # stubbed so callers can probe with {}.
    props = {**properties, **_note_property()}
    schema: dict[str, Any] = {"type": "object", "properties": props}
    return {
        "name": name,
        "will_do": will_do,
        "once_data": once_data,
        "how_to_call": how_to_call,
        "future_required": list(required or []),
        "description": _named_stub_description(
            name=name,
            will_do=will_do,
            once_data=once_data,
            how_to_call=how_to_call,
        ),
        "inputSchema": schema,
    }


# Named future Google / Takeout tools (same MCP + same index).
NAMED_STUB_SPECS: tuple[dict[str, Any], ...] = (
    _named_stub_spec(
        name="als_gmail_search",
        will_do=(
            "Keyword / filter search over ingested Gmail messages in the ALS "
            "map (FTS-style; snippet-default; hard limit ≤50)."
        ),
        once_data=(
            "query the unified index with `source=gmail` (and related mail "
            "sources), returning cite fields + short snippet — use "
            "`als_gmail_get` / `als_get` for full body."
        ),
        how_to_call=(
            "Eventually pass `query` (required later) plus optional `label`, "
            "`from_addr`, `to_addr`, `since`, `until`, `limit`, `offset`, "
            "`max_chars`. Today any args are accepted but ignored — `{}` is fine."
        ),
        properties={
            "query": {"type": "string", "description": "Future FTS/mail query"},
            "label": {"type": "string", "description": "Future Gmail label filter"},
            "from_addr": {"type": "string"},
            "to_addr": {"type": "string"},
            "since": {"type": "string", "description": "ISO-8601 lower bound"},
            "until": {"type": "string", "description": "ISO-8601 upper bound"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            "offset": {"type": "integer", "minimum": 0},
            "max_chars": {"type": "integer"},
        },
        required=["query"],
    ),
    _named_stub_spec(
        name="als_gmail_thread",
        will_do=(
            "Return ordered messages for one Gmail conversation / thread_id "
            "from the ALS map."
        ),
        once_data=(
            "page turns for that thread (default window ≤30) with cite fields "
            "+ capped text, same caps as `als_thread`."
        ),
        how_to_call=(
            "Eventually pass `thread_id` (required later) plus optional "
            "`limit`, `offset`, `max_chars`. Today `{}` probes only."
        ),
        properties={
            "thread_id": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            "offset": {"type": "integer", "minimum": 0},
            "max_chars": {"type": "integer"},
        },
        required=["thread_id"],
    ),
    _named_stub_spec(
        name="als_gmail_labels",
        will_do=(
            "List Gmail labels (and optional per-label counts) present in the "
            "ingested mail slice of the ALS map."
        ),
        once_data=(
            "return label names / ids derived from Takeout mail metadata in "
            "this same index — not a live Gmail API call."
        ),
        how_to_call=(
            "Eventually optional `prefix` filter and `limit`. Today `{}` probes only."
        ),
        properties={
            "prefix": {"type": "string", "description": "Optional label-name prefix"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        },
    ),
    _named_stub_spec(
        name="als_gmail_get",
        will_do="Fetch one Gmail message by id with full text (capped).",
        once_data=(
            "load the mail record from this same SQLite map (`source=gmail`) "
            "with cite fields + body; attachments via `als_gmail_attachments`."
        ),
        how_to_call=(
            "Eventually pass `id` (required later) plus optional `max_chars`, "
            "`include_meta`. Today `{}` probes only."
        ),
        properties={
            "id": {"type": "string"},
            "max_chars": {"type": "integer"},
            "include_meta": {"type": "boolean"},
        },
        required=["id"],
    ),
    _named_stub_spec(
        name="als_gmail_attachments",
        will_do=(
            "List attachment metadata (and later bounded excerpts) for a "
            "Gmail message in the ALS map."
        ),
        once_data=(
            "return filename, mime, size, and ids from ingested Takeout "
            "attachment sidecar data — not live Gmail download."
        ),
        how_to_call=(
            "Eventually pass `message_id` (required later) plus optional "
            "`limit`. Today `{}` probes only."
        ),
        properties={
            "message_id": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        required=["message_id"],
    ),
    _named_stub_spec(
        name="als_drive_search",
        will_do=(
            "Search Drive file metadata / extracted text in the ALS map "
            "(snippet-default; limit ≤50)."
        ),
        once_data=(
            "query unified index with `source=drive`, returning cite fields + "
            "snippet for matching files/docs."
        ),
        how_to_call=(
            "Eventually pass `query` (required later) plus optional `mime_type`, "
            "`since`, `until`, `limit`, `offset`, `max_chars`. Today `{}` probes."
        ),
        properties={
            "query": {"type": "string"},
            "mime_type": {"type": "string"},
            "since": {"type": "string"},
            "until": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            "offset": {"type": "integer", "minimum": 0},
            "max_chars": {"type": "integer"},
        },
        required=["query"],
    ),
    _named_stub_spec(
        name="als_drive_get",
        will_do=(
            "Fetch one Drive file record by id (metadata + capped text "
            "excerpt when available)."
        ),
        once_data=(
            "load from this same index (`source=drive`); binary blobs stay on "
            "disk / out of tool payloads except small text extracts."
        ),
        how_to_call=(
            "Eventually pass `id` (required later) plus optional `max_chars`, "
            "`include_meta`. Today `{}` probes only."
        ),
        properties={
            "id": {"type": "string"},
            "max_chars": {"type": "integer"},
            "include_meta": {"type": "boolean"},
        },
        required=["id"],
    ),
    _named_stub_spec(
        name="als_calendar_events",
        will_do=(
            "Browse calendar events in a time window from ingested Google "
            "Calendar Takeout data in the ALS map."
        ),
        once_data=(
            "return event cite fields + summary for `source=calendar` rows "
            "between `since`/`until` (chronological; limit ≤50)."
        ),
        how_to_call=(
            "Eventually pass optional `since`, `until`, `calendar_id`, `query`, "
            "`limit`, `offset`. Today `{}` probes only."
        ),
        properties={
            "since": {"type": "string"},
            "until": {"type": "string"},
            "calendar_id": {"type": "string"},
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            "offset": {"type": "integer", "minimum": 0},
        },
    ),
    _named_stub_spec(
        name="als_contacts_lookup",
        will_do=(
            "Lookup contacts by name, email, or phone from ingested Google "
            "Contacts in the ALS map."
        ),
        once_data=(
            "return matching contact records (`source=contacts`) with "
            "display name + addresses; limit ≤50."
        ),
        how_to_call=(
            "Eventually pass at least one of `query`, `email`, `phone` plus "
            "optional `limit`. Today `{}` probes only."
        ),
        properties={
            "query": {"type": "string", "description": "Name / free-text match"},
            "email": {"type": "string"},
            "phone": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
    ),
    _named_stub_spec(
        name="als_photos_search",
        will_do=(
            "Search Google Photos library metadata (titles, descriptions, "
            "albums, times) in the ALS map — not pixel payloads by default."
        ),
        once_data=(
            "query `source=photos` metadata rows; return cite fields + short "
            "caption/album snippet (limit ≤50)."
        ),
        how_to_call=(
            "Eventually pass `query` and/or `since`/`until`/`album`, plus "
            "`limit`, `offset`. Today `{}` probes only."
        ),
        properties={
            "query": {"type": "string"},
            "album": {"type": "string"},
            "since": {"type": "string"},
            "until": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            "offset": {"type": "integer", "minimum": 0},
        },
    ),
    _named_stub_spec(
        name="als_photos_meta",
        will_do=(
            "Get metadata for one photo / media item (EXIF-ish fields, album "
            "membership, capture time) from the ALS map."
        ),
        once_data=(
            "load one `source=photos` record by id; image bytes stay out of "
            "the default tool response."
        ),
        how_to_call=(
            "Eventually pass `id` (required later) plus optional "
            "`include_meta`. Today `{}` probes only."
        ),
        properties={
            "id": {"type": "string"},
            "include_meta": {"type": "boolean"},
        },
        required=["id"],
    ),
    _named_stub_spec(
        name="als_takeout_status",
        will_do=(
            "Report Google Takeout import / ingest status for this same ALS "
            "map (what archives are present, row counts by future source, "
            "last build fingerprint)."
        ),
        once_data=(
            "read map_meta / ingest sidecars and return counts for gmail, "
            "drive, calendar, contacts, photos alongside existing x/grok — "
            "still one database."
        ),
        how_to_call=(
            "Eventually optional `breakdown` (`source`|`none`). Today `{}` "
            "probes only."
        ),
        properties={
            "breakdown": {
                "type": "string",
                "enum": ["source", "none"],
                "description": "Future status breakdown mode",
            },
        },
    ),
)

NAMED_STUB_BY_NAME: dict[str, dict[str, Any]] = {
    s["name"]: s for s in NAMED_STUB_SPECS
}
NAMED_STUB_NAMES: tuple[str, ...] = tuple(s["name"] for s in NAMED_STUB_SPECS)
STUB_TOOL_NAMES: frozenset[str] = frozenset(
    NAMED_STUB_NAMES + EXPANSION_LETTER_STUB_NAMES
)


def _expansion_stub_tool_def(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "description": _expansion_stub_description(name),
        "inputSchema": {
            "type": "object",
            "properties": _note_property(),
        },
    }


def _named_stub_tool_def(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": spec["name"],
        "description": spec["description"],
        "inputSchema": spec["inputSchema"],
    }


def stub_payload(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Self-description payload for unimplemented named or letter stubs."""
    args = arguments or {}
    note = args.get("note")
    if name in NAMED_STUB_BY_NAME:
        spec = NAMED_STUB_BY_NAME[name]
        purpose = spec["description"]
        how_it_will_work = (
            f"{spec['will_do']} {spec['once_data']} Same single ALS map "
            "index — no separate Google map. "
            f"{spec['how_to_call']}"
        )
        fut = spec.get("future_required") or []
        fut_s = (
            f" Future required fields (once implemented): {', '.join(fut)}."
            if fut
            else ""
        )
        how_to_interact = (
            f"Today: call `{name}` with `{{}}` (or any of the documented "
            "future fields; they are ignored)."
            f"{fut_s} Later: same names/fields once Takeout rows exist in "
            "als_map.sqlite."
        )
    else:
        purpose = _expansion_stub_description(name)
        how_it_will_work = (
            "Reserved expansion slot on the single ALS map MCP. When filled, "
            "it will become a real retrieval/analysis tool over the same "
            "index; until then it only self-describes."
        )
        how_to_interact = (
            f"Call `{name}` with `{{}}` or optional `{{\"note\": \"…\"}}`. "
            "No required fields. Re-call later to see if the slot was implemented."
        )
    payload: dict[str, Any] = {
        "tool": name,
        "purpose": purpose,
        "how_to_interact": how_to_interact,
        "how_it_will_work": how_it_will_work,
        "status": STUB_STATUS,
        "implemented": False,
        "map": "als_map",
        "separate_map": False,
    }
    if note is not None:
        payload["note_ignored"] = str(note)
    return payload

# Log to stderr only — stdout is the MCP transport.
def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def db_path() -> Path:
    env = os.environ.get("ALS_MAP_DB")
    return Path(env) if env else DEFAULT_DB


TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": "als_search",
        "description": (
            "Keyword search over Austen’s X + Grok corpus (FTS5). "
            "Default returns cite fields + short snippet (not full text). "
            "Hard limit ≤50. Use als_get / als_get_many for full text."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "FTS5 query string"},
                "sources": {
                    "type": "string",
                    "description": "Comma-separated sources, e.g. x_post,x_reply,grok_chat",
                },
                "role": {"type": "string", "description": "Filter role (human|assistant|self)"},
                "since": {"type": "string", "description": "ISO-8601 lower bound (inclusive)"},
                "until": {"type": "string", "description": "ISO-8601 upper bound (exclusive)"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "offset": {"type": "integer", "minimum": 0},
                "max_chars": {
                    "type": "integer",
                    "description": "Snippet char cap (default 400, max 2000)",
                },
                "full_text": {
                    "type": "boolean",
                    "description": "If true, return truncated text instead of snippet",
                },
                "include_meta": {"type": "boolean"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "als_get",
        "description": "Fetch one record by id with full text (capped).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "max_chars": {
                    "type": "integer",
                    "description": "Text char cap (default 32000, max 64000)",
                },
                "include_meta": {"type": "boolean", "default": True},
            },
            "required": ["id"],
        },
    },
    {
        "name": "als_get_many",
        "description": "Batch fetch by ids (≤50) with full text (capped).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 50,
                },
                "max_chars": {"type": "integer"},
                "include_meta": {"type": "boolean"},
            },
            "required": ["ids"],
        },
    },
    {
        "name": "als_thread",
        "description": (
            "Ordered records for a thread_id (Grok conversation or X thread key). "
            "Default window ≤30 turns; use offset to page."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "thread_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "offset": {"type": "integer", "minimum": 0},
                "max_chars": {"type": "integer"},
                "include_meta": {"type": "boolean"},
            },
            "required": ["thread_id"],
        },
    },
    {
        "name": "als_browse",
        "description": (
            "Keywordless chronological browse over a time window. "
            "Use for timeline slices when you do not know the magic keyword."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "since": {"type": "string"},
                "until": {"type": "string"},
                "sources": {"type": "string"},
                "role": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "offset": {"type": "integer", "minimum": 0},
                "max_chars": {"type": "integer"},
                "include_meta": {"type": "boolean"},
            },
        },
    },
    {
        "name": "als_stats",
        "description": "Index orientation: record counts and optional breakdown.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "breakdown": {
                    "type": "string",
                    "enum": ["source", "role", "month", "both", "none"],
                    "default": "source",
                },
            },
        },
    },
    {
        "name": "als_sample",
        "description": (
            "Prior-free random sample of records (snippet-shaped like als_search). "
            "Optional stratification by time (year-month) or thread_length. "
            "source accepts x|grok shorthand or specific source names. n≤50."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "n": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
                "role": {"type": "string", "description": "Filter role (human|assistant|self)"},
                "source": {
                    "type": "string",
                    "description": "x|grok shorthand, or comma-separated sources",
                },
                "stratify_by": {
                    "type": "string",
                    "enum": ["none", "time", "thread_length"],
                    "default": "none",
                },
                "seed": {
                    "type": "integer",
                    "description": "RNG seed for reproducible samples",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Snippet char cap (default 400, max 2000)",
                },
                "min_thread_length": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Only records whose thread has at least this many messages",
                },
                "has_refine": {
                    "type": "boolean",
                    "description": "Filter by heuristic edit/refine cues on the thread",
                },
                "has_critique": {
                    "type": "boolean",
                    "description": "Filter by heuristic critique-request cues on the thread",
                },
                "min_code_density": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "description": "Min share of messages with code-ish markers (0–1)",
                },
            },
        },
    },
    {
        "name": "als_style_stats",
        "description": (
            "Cheap deterministic style aggregates for a filtered slice: count, "
            "mean/median char & word length, question-mark density, follow-up share, "
            "opening bigrams/trigrams (human role by default), approximate register markers. "
            "No full-text dump."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": "x|grok shorthand, or comma-separated sources",
                },
                "role": {"type": "string"},
                "since": {"type": "string", "description": "ISO-8601 lower bound (inclusive)"},
                "until": {"type": "string", "description": "ISO-8601 upper bound (exclusive)"},
            },
        },
    },
    {
        "name": "als_contrast",
        "description": (
            "Compare two slices (source_a vs source_b and/or time windows) on the same "
            "metrics as als_style_stats, with delta_b_minus_a columns."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source_a": {"type": "string"},
                "source_b": {"type": "string"},
                "since_a": {"type": "string"},
                "until_a": {"type": "string"},
                "since_b": {"type": "string"},
                "until_b": {"type": "string"},
                "role": {
                    "type": "string",
                    "description": "Shared role filter for both slices",
                },
                "role_a": {"type": "string"},
                "role_b": {"type": "string"},
            },
        },
    },
    {
        "name": "als_semantic_search",
        "description": (
            "Local embedding nearest-neighbor search over the corpus "
            "(sentence-transformers; no remote embed API). "
            "Returns cite fields + snippet + cosine score. Hard limit ≤50. "
            "Use for concept queries that FTS keywords miss."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language query"},
                "sources": {
                    "type": "string",
                    "description": "Comma-separated sources, e.g. x_post,x_reply,grok_chat",
                },
                "source": {
                    "type": "string",
                    "description": "Alias of sources; also accepts x|grok shorthand",
                },
                "role": {"type": "string", "description": "Filter role (human|assistant|self)"},
                "since": {"type": "string", "description": "ISO-8601 lower bound (inclusive)"},
                "until": {"type": "string", "description": "ISO-8601 upper bound (exclusive)"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "max_chars": {
                    "type": "integer",
                    "description": "Snippet char cap (default 400, max 2000)",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "als_thread_features",
        "description": (
            "Heuristic per-thread features (no LLM): message length, "
            "human/assistant/self counts + ratio, edit/refine cues, "
            "critique-request cues, code/tech density. "
            "Pass thread_id for one thread, or filter/list threads (limit≤50)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "thread_id": {
                    "type": "string",
                    "description": "If set, return features for this thread only",
                },
                "source": {
                    "type": "string",
                    "description": "x|grok shorthand or specific source (filters source_primary)",
                },
                "min_thread_length": {"type": "integer", "minimum": 1},
                "max_thread_length": {"type": "integer", "minimum": 1},
                "has_refine": {"type": "boolean"},
                "has_critique": {"type": "boolean"},
                "min_code_density": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                },
                "min_tech_density": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "offset": {"type": "integer", "minimum": 0},
            },
        },
    },
]

# Append named Google/Takeout stubs, then leftover letter expansion slots.
TOOL_DEFS.extend(_named_stub_tool_def(s) for s in NAMED_STUB_SPECS)
TOOL_DEFS.extend(_expansion_stub_tool_def(n) for n in EXPANSION_LETTER_STUB_NAMES)


def _tool_result(payload: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    return {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
        "structuredContent": payload,
    }


def call_tool(name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    args = arguments or {}
    if name in STUB_TOOL_NAMES:
        return _tool_result(stub_payload(name, args))

    try:
        conn = open_db(db_path())
    except FileNotFoundError as exc:
        return _tool_result(
            {
                "error": str(exc),
                "hint": "run: python scripts/build_map_index.py",
            },
            is_error=True,
        )

    try:
        with conn:
            if name == "als_search":
                payload = search(
                    conn,
                    str(args.get("query") or ""),
                    sources=args.get("sources"),
                    role=args.get("role"),
                    since=args.get("since"),
                    until=args.get("until"),
                    limit=args.get("limit"),
                    offset=int(args.get("offset") or 0),
                    max_chars=args.get("max_chars"),
                    full_text=bool(args.get("full_text")),
                    include_meta=bool(args.get("include_meta")),
                )
            elif name == "als_get":
                payload = get_one(
                    conn,
                    str(args.get("id") or ""),
                    max_chars=args.get("max_chars"),
                    include_meta=bool(args.get("include_meta", True)),
                )
            elif name == "als_get_many":
                ids = args.get("ids") or []
                if not isinstance(ids, list):
                    raise ValueError("ids must be an array of strings")
                payload = get_many(
                    conn,
                    [str(i) for i in ids],
                    max_chars=args.get("max_chars"),
                    include_meta=bool(args.get("include_meta")),
                )
            elif name == "als_thread":
                payload = thread(
                    conn,
                    str(args.get("thread_id") or ""),
                    limit=args.get("limit"),
                    offset=int(args.get("offset") or 0),
                    max_chars=args.get("max_chars"),
                    include_meta=bool(args.get("include_meta")),
                )
            elif name == "als_browse":
                payload = browse(
                    conn,
                    since=args.get("since"),
                    until=args.get("until"),
                    sources=args.get("sources"),
                    role=args.get("role"),
                    limit=args.get("limit"),
                    offset=int(args.get("offset") or 0),
                    max_chars=args.get("max_chars"),
                    include_meta=bool(args.get("include_meta")),
                )
            elif name == "als_stats":
                payload = stats(conn, breakdown=args.get("breakdown") or "source")
            elif name == "als_sample":
                seed = args.get("seed")
                min_len = args.get("min_thread_length")
                min_code = args.get("min_code_density")
                payload = sample(
                    conn,
                    n=args.get("n"),
                    role=args.get("role"),
                    source=args.get("source") or args.get("sources"),
                    stratify_by=args.get("stratify_by") or "none",
                    seed=None if seed is None else int(seed),
                    max_chars=args.get("max_chars"),
                    min_thread_length=None if min_len is None else int(min_len),
                    has_refine=args.get("has_refine"),
                    has_critique=args.get("has_critique"),
                    min_code_density=(
                        None if min_code is None else float(min_code)
                    ),
                )
            elif name == "als_style_stats":
                payload = style_stats(
                    conn,
                    source=args.get("source") or args.get("sources"),
                    role=args.get("role"),
                    since=args.get("since"),
                    until=args.get("until"),
                )
            elif name == "als_contrast":
                payload = contrast(
                    conn,
                    source_a=args.get("source_a"),
                    source_b=args.get("source_b"),
                    since_a=args.get("since_a"),
                    until_a=args.get("until_a"),
                    since_b=args.get("since_b"),
                    until_b=args.get("until_b"),
                    role=args.get("role"),
                    role_a=args.get("role_a"),
                    role_b=args.get("role_b"),
                )
            elif name == "als_semantic_search":
                payload = semantic_search(
                    conn,
                    str(args.get("query") or ""),
                    sources=args.get("source") or args.get("sources"),
                    role=args.get("role"),
                    since=args.get("since"),
                    until=args.get("until"),
                    limit=args.get("limit"),
                    max_chars=args.get("max_chars"),
                )
            elif name == "als_thread_features":
                min_len = args.get("min_thread_length")
                max_len = args.get("max_thread_length")
                min_code = args.get("min_code_density")
                min_tech = args.get("min_tech_density")
                tid = args.get("thread_id")
                payload = thread_features(
                    conn,
                    thread_id=None if tid in (None, "") else str(tid),
                    source=args.get("source") or args.get("sources"),
                    min_thread_length=None if min_len is None else int(min_len),
                    max_thread_length=None if max_len is None else int(max_len),
                    has_refine=args.get("has_refine"),
                    has_critique=args.get("has_critique"),
                    min_code_density=(
                        None if min_code is None else float(min_code)
                    ),
                    min_tech_density=(
                        None if min_tech is None else float(min_tech)
                    ),
                    limit=args.get("limit"),
                    offset=int(args.get("offset") or 0),
                )
            else:
                return _tool_result({"error": f"unknown tool: {name}"}, is_error=True)
        return _tool_result(payload)
    except ValueError as exc:
        return _tool_result({"error": str(exc)}, is_error=True)
    except Exception as exc:  # noqa: BLE001 — surface to MCP client
        log(traceback.format_exc())
        return _tool_result({"error": f"{type(exc).__name__}: {exc}"}, is_error=True)


def handle_message(msg: dict[str, Any]) -> dict[str, Any] | None:
    """Handle one JSON-RPC request; return response or None for notifications."""
    method = msg.get("method")
    msg_id = msg.get("id", None)
    params = msg.get("params") or {}

    # Notifications (no id) — acknowledge silently
    if msg_id is None and method is not None:
        if method == "notifications/initialized":
            return None
        return None

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"tools": TOOL_DEFS},
        }

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        result = call_tool(str(name), arguments if isinstance(arguments, dict) else {})
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def _read_message() -> dict[str, Any] | None:
    """Read one MCP stdio message (Content-Length framed or newline-delimited JSON)."""
    # Prefer Content-Length framing; also accept NDJSON for simple smoke tests.
    header_line = sys.stdin.buffer.readline()
    if not header_line:
        return None
    # NDJSON path: line starts with '{'
    if header_line.lstrip().startswith(b"{"):
        return json.loads(header_line.decode("utf-8"))

    headers: dict[str, str] = {}
    line = header_line
    while True:
        if not line:
            return None
        text = line.decode("utf-8", errors="replace").strip()
        if text == "":
            break
        if ":" in text:
            k, v = text.split(":", 1)
            headers[k.strip().lower()] = v.strip()
        line = sys.stdin.buffer.readline()

    length_s = headers.get("content-length")
    if not length_s:
        raise ValueError("missing Content-Length header")
    length = int(length_s)
    body = sys.stdin.buffer.read(length)
    if len(body) < length:
        return None
    return json.loads(body.decode("utf-8"))


def _write_message(msg: dict[str, Any]) -> None:
    raw = json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii"))
    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()


def main() -> int:
    log(f"{SERVER_NAME} {SERVER_VERSION} starting; db={db_path()}")
    while True:
        try:
            msg = _read_message()
        except Exception as exc:  # noqa: BLE001
            log(f"read error: {exc}")
            continue
        if msg is None:
            break
        try:
            resp = handle_message(msg)
        except Exception as exc:  # noqa: BLE001
            log(traceback.format_exc())
            if "id" in msg:
                _write_message(
                    {
                        "jsonrpc": "2.0",
                        "id": msg.get("id"),
                        "error": {"code": -32603, "message": str(exc)},
                    }
                )
            continue
        if resp is not None:
            _write_message(resp)
    log(f"{SERVER_NAME} exiting")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
