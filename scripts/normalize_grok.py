#!/usr/bin/env python3
"""
Stream-normalize xAI Grok account export (prod-grok-backend.json) → JSONL.

One record per message/turn. Designed for later extension (e.g. source: x_post).

Usage:
  python scripts/normalize_grok.py
  python scripts/normalize_grok.py --input PATH --output PATH --stats PATH
  python scripts/normalize_grok.py --progress-every 100
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

try:
    import ijson
except ImportError:  # pragma: no cover
    print(
        "Missing dependency: ijson. Install with: pip install -r requirements.txt",
        file=sys.stderr,
    )
    raise SystemExit(1)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = (
    REPO_ROOT
    / "10b2e090-7b79-4786-bf86-6e2da0cd437e"
    / "ttl"
    / "30d"
    / "export_data"
    / "d9fcc965-ed34-4cb2-aad0-45f6f9a550fd"
    / "prod-grok-backend.json"
)
DEFAULT_OUTPUT = REPO_ROOT / "data" / "normalized" / "grok.jsonl"
DEFAULT_STATS = REPO_ROOT / "data" / "normalized" / "grok-stats.json"

SOURCE = "grok_chat"

# Extension point: future ingest sources share the same top-level record shape.
KNOWN_SOURCES = ("grok_chat",)  # future: "x_post", "x_like", ...

ROLE_MAP = {
    "human": "human",
    "user": "human",
    "assistant": "assistant",
    "ASSISTANT": "assistant",
    "model": "assistant",
    "system": "system",
}


def parse_timestamp(value: Any) -> str | None:
    """Normalize export timestamps to ISO-8601 UTC (Z)."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        # Heuristic: ms vs s
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return (
            datetime.fromtimestamp(ts, tz=timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    if isinstance(value, dict) and "$date" in value:
        inner = value["$date"]
        if isinstance(inner, dict) and "$numberLong" in inner:
            return parse_timestamp(int(inner["$numberLong"]))
        if isinstance(inner, (int, float, str)):
            if isinstance(inner, str) and inner.isdigit():
                return parse_timestamp(int(inner))
            if isinstance(inner, str):
                return inner
            return parse_timestamp(inner)
    return str(value)


def normalize_role(raw: Any) -> str:
    if raw is None:
        return "unknown"
    s = str(raw)
    return ROLE_MAP.get(s, s)


def load_project_index(path: Path) -> dict[str, dict[str, Any]]:
    """Map workspace_id → {name, create_time} (light metadata only)."""
    index: dict[str, dict[str, Any]] = {}
    with path.open("rb") as f:
        for proj in ijson.items(f, "projects.item"):
            wid = proj.get("workspace_id")
            if not wid:
                continue
            index[wid] = {
                "project_name": proj.get("name"),
                "project_create_time": parse_timestamp(proj.get("create_time")),
            }
    return index


def unwrap_response(wrap: Any) -> dict[str, Any]:
    if not isinstance(wrap, dict):
        return {}
    inner = wrap.get("response")
    if isinstance(inner, dict):
        return inner
    return wrap


def iter_conversation_items(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("rb") as f:
        yield from ijson.items(f, "conversations.item")


def build_record(
    *,
    response: dict[str, Any],
    conversation: dict[str, Any],
    index: int,
    projects: dict[str, dict[str, Any]],
    share_link: Any = None,
) -> dict[str, Any]:
    conv_id = conversation.get("id") or response.get("conversation_id") or "unknown"
    resp_id = response.get("_id") or response.get("id")
    record_id = str(resp_id) if resp_id else f"{conv_id}:{index}"

    raw_sender = response.get("sender")
    text = response.get("message")
    if text is None:
        text = ""
    elif not isinstance(text, str):
        text = str(text)

    workspace_id = conversation.get("workspace_id")
    project_meta = projects.get(workspace_id) if workspace_id else None

    meta: dict[str, Any] = {
        "title": conversation.get("title"),
        "conversation_create_time": parse_timestamp(conversation.get("create_time")),
        "conversation_modify_time": parse_timestamp(conversation.get("modify_time")),
        "sender_raw": raw_sender,
        "model": response.get("model") or None,
        "parent_response_id": response.get("parent_response_id"),
        "response_index": index,
        "creation_source": conversation.get("creation_source"),
        "starred": conversation.get("starred"),
        "temporary": conversation.get("temporary"),
    }
    if workspace_id:
        meta["workspace_id"] = workspace_id
    if project_meta:
        meta["project_name"] = project_meta.get("project_name")
        meta["project_create_time"] = project_meta.get("project_create_time")

    asset_ids = conversation.get("asset_ids") or []
    root_asset_id = conversation.get("root_asset_id")
    if asset_ids or root_asset_id:
        meta["asset_ids"] = asset_ids
        meta["root_asset_id"] = root_asset_id

    if share_link:
        meta["share_link"] = share_link

    # Drop empty model string → already None; strip other Nones optionally kept for stability
    if not meta.get("model"):
        meta["model"] = None

    record: dict[str, Any] = {
        "id": record_id,
        "source": SOURCE,
        "created_at": parse_timestamp(response.get("create_time")),
        "url": None,
        "thread_id": conv_id,
        "role": normalize_role(raw_sender),
        "text": text,
        "meta": meta,
    }
    return record


def normalize(
    input_path: Path,
    output_path: Path,
    stats_path: Path | None,
    progress_every: int = 50,
) -> dict[str, Any]:
    if not input_path.is_file():
        raise FileNotFoundError(f"Input not found: {input_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if stats_path:
        stats_path.parent.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    print(f"Loading project index from {input_path} ...", flush=True)
    projects = load_project_index(input_path)
    print(f"  projects indexed: {len(projects)}", flush=True)

    role_counts: Counter[str] = Counter()
    sender_raw_counts: Counter[str] = Counter()
    model_counts: Counter[str] = Counter()
    conversations = 0
    records = 0
    empty_text = 0
    with_project = 0

    print(f"Streaming conversations -> {output_path}", flush=True)
    with output_path.open("w", encoding="utf-8", newline="\n") as out:
        for item in iter_conversation_items(input_path):
            conversations += 1
            conversation = item.get("conversation") or {}
            responses = item.get("responses") or []
            if conversation.get("workspace_id"):
                with_project += 1

            for index, wrap in enumerate(responses):
                response = unwrap_response(wrap)
                share_link = wrap.get("share_link") if isinstance(wrap, dict) else None
                record = build_record(
                    response=response,
                    conversation=conversation,
                    index=index,
                    projects=projects,
                    share_link=share_link,
                )
                out.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
                out.write("\n")
                records += 1
                role_counts[record["role"]] += 1
                sender_raw_counts[str(record["meta"].get("sender_raw"))] += 1
                model = record["meta"].get("model")
                if model:
                    model_counts[str(model)] += 1
                if not record["text"]:
                    empty_text += 1

            if progress_every and conversations % progress_every == 0:
                elapsed = time.perf_counter() - t0
                print(
                    f"  ... {conversations} conversations, {records} records "
                    f"({elapsed:.1f}s)",
                    flush=True,
                )

    elapsed = time.perf_counter() - t0
    stats: dict[str, Any] = {
        "source": SOURCE,
        "known_sources_extension_point": list(KNOWN_SOURCES)
        + ["x_post", "x_like", "x_reply"],
        "input": str(input_path),
        "output": str(output_path),
        "conversations": conversations,
        "records": records,
        "projects_indexed": len(projects),
        "conversations_with_workspace": with_project,
        "empty_text_records": empty_text,
        "roles": dict(role_counts),
        "sender_raw": dict(sender_raw_counts),
        "models": dict(model_counts),
        "elapsed_seconds": round(elapsed, 3),
        "generated_at": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
    }
    if stats_path:
        stats_path.write_text(
            json.dumps(stats, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"Wrote stats -> {stats_path}", flush=True)

    print(
        f"Done: {conversations} conversations -> {records} records in {elapsed:.1f}s",
        flush=True,
    )
    return stats


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    p.add_argument("--stats", type=Path, default=DEFAULT_STATS)
    p.add_argument(
        "--no-stats",
        action="store_true",
        help="Skip writing grok-stats.json",
    )
    p.add_argument("--progress-every", type=int, default=50)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    stats_path = None if args.no_stats else args.stats
    normalize(
        input_path=args.input,
        output_path=args.output,
        stats_path=stats_path,
        progress_every=args.progress_every,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
