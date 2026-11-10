#!/usr/bin/env python3
"""
Normalize an official X/Twitter "Download your archive" folder → JSONL.

Parses window.YTD.*.partN = [...] wrappers. Emits one record per tweet,
reply, repost, like, and (when present) long-form note-tweet.

Usage:
  python scripts/normalize_x.py
  python scripts/normalize_x.py --archive PATH --output PATH --stats PATH
  python scripts/normalize_x.py --progress-every 2000
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterator

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARCHIVE = REPO_ROOT / (
    "twitter-2026-11-08-d3b063904b28d3226f33bc4beaa26808e19632023745c6bd0c3eb41cdd96d748"
)
DEFAULT_OUTPUT = REPO_ROOT / "data" / "normalized" / "x.jsonl"
DEFAULT_STATS = REPO_ROOT / "data" / "normalized" / "x-stats.json"

YTD_ASSIGN_RE = re.compile(
    r"^window\.YTD\.(?P<ns>[^.]+)\.part(?P<part>\d+)\s*=\s*",
    re.MULTILINE,
)

# Twitter classic created_at: "Sun Nov 08 17:08:48 +0000 2026"
SOURCES = ("x_post", "x_reply", "x_like", "x_repost", "x_note")


def find_archive(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    if DEFAULT_ARCHIVE.is_dir():
        return DEFAULT_ARCHIVE
    matches = sorted(REPO_ROOT.glob("twitter-*"))
    dirs = [p for p in matches if p.is_dir() and (p / "data").is_dir()]
    if len(dirs) == 1:
        return dirs[0]
    if not dirs:
        raise FileNotFoundError(
            f"No twitter-* archive directory under {REPO_ROOT}. "
            "Pass --archive PATH."
        )
    raise FileNotFoundError(
        f"Multiple twitter-* archives found; pass --archive. Candidates: {dirs}"
    )


def parse_created_at(value: Any) -> str | None:
    """Normalize archive timestamps to ISO-8601 UTC (Z)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return (
            datetime.fromtimestamp(ts, tz=timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    if not isinstance(value, str):
        return str(value)
    s = value.strip()
    if not s:
        return None
    # Already ISO-ish
    if "T" in s and (s.endswith("Z") or "+" in s[10:] or s.endswith("z")):
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return (
                dt.astimezone(timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
        except ValueError:
            pass
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (
            dt.astimezone(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    except (TypeError, ValueError, IndexError):
        return s


def status_url(username: str | None, tweet_id: str) -> str:
    handle = username or "i"
    return f"https://x.com/{handle}/status/{tweet_id}"


def load_account(data_dir: Path) -> dict[str, Any]:
    path = data_dir / "account.js"
    if not path.is_file():
        return {}
    items = list(iter_ytd_items(path))
    if not items:
        return {}
    first = items[0]
    if isinstance(first, dict) and "account" in first:
        return first["account"] or {}
    return first if isinstance(first, dict) else {}


def iter_ytd_parts(path: Path) -> Iterator[tuple[str, int, Any]]:
    """
    Yield (namespace, part_index, parsed_value) for each window.YTD assignment
    in a file. Prefer one assignment per file (classic archive layout).
    """
    text = path.read_text(encoding="utf-8")
    matches = list(YTD_ASSIGN_RE.finditer(text))
    if not matches:
        raise ValueError(f"No window.YTD.*.partN assignment in {path}")
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        payload = text[start:end].strip().rstrip(";").strip()
        yield m.group("ns"), int(m.group("part")), json.loads(payload)


def iter_ytd_items(path: Path) -> Iterator[Any]:
    for _ns, _part, value in iter_ytd_parts(path):
        if isinstance(value, list):
            yield from value
        else:
            yield value


def classify_tweet(tweet: dict[str, Any]) -> str:
    text = tweet.get("full_text") or tweet.get("text") or ""
    if not isinstance(text, str):
        text = str(text)
    if text.startswith("RT @") or tweet.get("retweeted_status_id_str"):
        return "x_repost"
    if tweet.get("in_reply_to_status_id_str") or tweet.get("in_reply_to_status_id"):
        return "x_reply"
    return "x_post"


def entity_screen_names(entities: Any) -> list[str]:
    if not isinstance(entities, dict):
        return []
    mentions = entities.get("user_mentions") or []
    out: list[str] = []
    for m in mentions:
        if isinstance(m, dict) and m.get("screen_name"):
            out.append(str(m["screen_name"]))
    return out


def media_urls(tweet: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    extended = tweet.get("extended_entities") or {}
    media = extended.get("media") if isinstance(extended, dict) else None
    if not media:
        entities = tweet.get("entities") or {}
        media = entities.get("media") if isinstance(entities, dict) else None
    if not isinstance(media, list):
        return urls
    for m in media:
        if not isinstance(m, dict):
            continue
        u = m.get("media_url_https") or m.get("media_url") or m.get("expanded_url")
        if u:
            urls.append(str(u))
    return urls


def build_tweet_record(
    tweet: dict[str, Any],
    *,
    username: str | None,
    deleted: bool = False,
) -> dict[str, Any]:
    tweet_id = str(tweet.get("id_str") or tweet.get("id") or "")
    source = classify_tweet(tweet)
    text = tweet.get("full_text")
    if text is None:
        text = tweet.get("text") or ""
    if not isinstance(text, str):
        text = str(text)

    parent_id = tweet.get("in_reply_to_status_id_str") or tweet.get(
        "in_reply_to_status_id"
    )
    parent_id_s = str(parent_id) if parent_id else None

    if source == "x_reply" and parent_id_s:
        thread_id = parent_id_s
    else:
        thread_id = tweet_id

    meta: dict[str, Any] = {
        "lang": tweet.get("lang"),
        "favorite_count": _maybe_int(tweet.get("favorite_count")),
        "retweet_count": _maybe_int(tweet.get("retweet_count")),
        "source_client": tweet.get("source"),
        "in_reply_to_status_id": parent_id_s,
        "in_reply_to_user_id": (
            str(tweet["in_reply_to_user_id_str"])
            if tweet.get("in_reply_to_user_id_str")
            else (
                str(tweet["in_reply_to_user_id"])
                if tweet.get("in_reply_to_user_id")
                else None
            )
        ),
        "in_reply_to_screen_name": tweet.get("in_reply_to_screen_name"),
        "user_mentions": entity_screen_names(tweet.get("entities")),
        "media_urls": media_urls(tweet),
        "truncated": bool(tweet.get("truncated")),
        "username": username,
    }
    if deleted:
        meta["deleted"] = True
        if tweet.get("deleted_at"):
            meta["deleted_at"] = parse_created_at(tweet.get("deleted_at"))

    # Drop empty media list noise? keep for stability of shape — omit empties
    if not meta["media_urls"]:
        del meta["media_urls"]
    if not meta["user_mentions"]:
        del meta["user_mentions"]
    if meta.get("in_reply_to_status_id") is None:
        del meta["in_reply_to_status_id"]
    if meta.get("in_reply_to_user_id") is None:
        del meta["in_reply_to_user_id"]
    if meta.get("in_reply_to_screen_name") is None:
        del meta["in_reply_to_screen_name"]

    return {
        "id": tweet_id,
        "source": source,
        "created_at": parse_created_at(tweet.get("created_at")),
        "url": status_url(username, tweet_id) if tweet_id else None,
        "thread_id": thread_id,
        "role": "self",
        "text": text,
        "meta": meta,
    }


def build_like_record(like: dict[str, Any], *, username: str | None) -> dict[str, Any]:
    tweet_id = str(like.get("tweetId") or like.get("tweet_id") or "")
    text = like.get("fullText")
    if text is None:
        text = like.get("full_text") or ""
    if not isinstance(text, str):
        text = str(text)
    expanded = like.get("expandedUrl") or like.get("expanded_url")
    url = str(expanded) if expanded else (status_url(None, tweet_id) if tweet_id else None)
    if url and "twitter.com" in url:
        url = url.replace("https://twitter.com/", "https://x.com/", 1)

    return {
        "id": f"like:{tweet_id}" if tweet_id else "like:unknown",
        "source": "x_like",
        "created_at": None,  # archive likes have no like timestamp
        "url": url,
        "thread_id": tweet_id or None,
        "role": None,  # liked content authored by others (consistent omit)
        "text": text,
        "meta": {
            "liked_tweet_id": tweet_id or None,
            "expanded_url": expanded,
            "liker_username": username,
        },
    }


def build_note_record(note: dict[str, Any], *, username: str | None) -> dict[str, Any]:
    note_id = str(note.get("noteTweetId") or note.get("id") or "")
    core = note.get("core") if isinstance(note.get("core"), dict) else {}
    text = core.get("text") if isinstance(core, dict) else None
    if text is None:
        text = note.get("text") or ""
    if not isinstance(text, str):
        text = str(text)
    lifecycle = note.get("lifecycle") if isinstance(note.get("lifecycle"), dict) else {}

    return {
        "id": f"note:{note_id}" if note_id else "note:unknown",
        "source": "x_note",
        "created_at": parse_created_at(note.get("createdAt") or note.get("created_at")),
        "url": None,  # no stable public URL in this archive section
        "thread_id": note_id or None,
        "role": "self",
        "text": text,
        "meta": {
            "note_tweet_id": note_id or None,
            "updated_at": parse_created_at(note.get("updatedAt")),
            "lifecycle": lifecycle.get("name") if lifecycle else None,
            "username": username,
            # Archive does not join noteTweetId → tweet id_str
            "tweet_id": None,
        },
    }


def _maybe_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def write_record(out, record: dict[str, Any]) -> None:
    out.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
    out.write("\n")


def normalize(
    archive_path: Path,
    output_path: Path,
    stats_path: Path | None,
    progress_every: int = 2000,
    include_deleted: bool = False,
    include_notes: bool = True,
) -> dict[str, Any]:
    data_dir = archive_path / "data"
    if not data_dir.is_dir():
        raise FileNotFoundError(f"Archive data/ not found: {data_dir}")

    tweets_path = data_dir / "tweets.js"
    likes_path = data_dir / "like.js"
    notes_path = data_dir / "note-tweet.js"
    deleted_path = data_dir / "deleted-tweets.js"

    if not tweets_path.is_file():
        raise FileNotFoundError(f"Missing {tweets_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if stats_path:
        stats_path.parent.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    account = load_account(data_dir)
    username = account.get("username")
    account_id = account.get("accountId")
    print(
        f"Account: @{username} ({account_id}) from {archive_path.name}",
        flush=True,
    )

    source_counts: Counter[str] = Counter()
    role_counts: Counter[str] = Counter()
    empty_text = 0
    records = 0
    sections: dict[str, Any] = {}

    def bump(record: dict[str, Any]) -> None:
        nonlocal records, empty_text
        records += 1
        source_counts[record["source"]] += 1
        role = record.get("role")
        role_counts["null" if role is None else str(role)] += 1
        if not record.get("text"):
            empty_text += 1

    print(f"Writing -> {output_path}", flush=True)
    with output_path.open("w", encoding="utf-8", newline="\n") as out:
        # --- tweets ---
        print(f"Parsing tweets: {tweets_path} ({tweets_path.stat().st_size} bytes)", flush=True)
        n_tweets = 0
        for item in iter_ytd_items(tweets_path):
            tweet = item.get("tweet", item) if isinstance(item, dict) else {}
            if not isinstance(tweet, dict):
                continue
            record = build_tweet_record(tweet, username=username)
            write_record(out, record)
            bump(record)
            n_tweets += 1
            if progress_every and n_tweets % progress_every == 0:
                elapsed = time.perf_counter() - t0
                print(
                    f"  ... tweets {n_tweets}, total records {records} ({elapsed:.1f}s)",
                    flush=True,
                )
        sections["tweets.js"] = {"items": n_tweets, "bytes": tweets_path.stat().st_size}
        print(f"  tweets done: {n_tweets}", flush=True)

        # --- likes ---
        n_likes = 0
        if likes_path.is_file():
            print(
                f"Parsing likes: {likes_path} ({likes_path.stat().st_size} bytes)",
                flush=True,
            )
            for item in iter_ytd_items(likes_path):
                like = item.get("like", item) if isinstance(item, dict) else {}
                if not isinstance(like, dict):
                    continue
                record = build_like_record(like, username=username)
                write_record(out, record)
                bump(record)
                n_likes += 1
                if progress_every and n_likes % progress_every == 0:
                    elapsed = time.perf_counter() - t0
                    print(
                        f"  ... likes {n_likes}, total records {records} ({elapsed:.1f}s)",
                        flush=True,
                    )
            sections["like.js"] = {
                "items": n_likes,
                "bytes": likes_path.stat().st_size,
            }
            print(f"  likes done: {n_likes}", flush=True)
        else:
            sections["like.js"] = {"items": 0, "missing": True}

        # --- note tweets (long-form) ---
        n_notes = 0
        if include_notes and notes_path.is_file():
            print(
                f"Parsing note-tweets: {notes_path} ({notes_path.stat().st_size} bytes)",
                flush=True,
            )
            for item in iter_ytd_items(notes_path):
                note = item.get("noteTweet", item) if isinstance(item, dict) else {}
                if not isinstance(note, dict):
                    continue
                record = build_note_record(note, username=username)
                write_record(out, record)
                bump(record)
                n_notes += 1
            sections["note-tweet.js"] = {
                "items": n_notes,
                "bytes": notes_path.stat().st_size,
            }
            print(f"  notes done: {n_notes}", flush=True)
        elif notes_path.is_file():
            sections["note-tweet.js"] = {
                "items": 0,
                "skipped": True,
                "bytes": notes_path.stat().st_size,
            }

        # --- optional deleted tweets ---
        n_deleted = 0
        if include_deleted and deleted_path.is_file():
            print(f"Parsing deleted-tweets: {deleted_path}", flush=True)
            for item in iter_ytd_items(deleted_path):
                tweet = item.get("tweet", item) if isinstance(item, dict) else {}
                if not isinstance(tweet, dict):
                    continue
                record = build_tweet_record(tweet, username=username, deleted=True)
                write_record(out, record)
                bump(record)
                n_deleted += 1
            sections["deleted-tweets.js"] = {
                "items": n_deleted,
                "bytes": deleted_path.stat().st_size,
            }
        elif deleted_path.is_file():
            # Count only for inventory; not emitted by default (avoid dup with live set)
            try:
                n_deleted_present = sum(1 for _ in iter_ytd_items(deleted_path))
            except Exception:
                n_deleted_present = None
            sections["deleted-tweets.js"] = {
                "items_present": n_deleted_present,
                "emitted": 0,
                "bytes": deleted_path.stat().st_size,
                "note": "present but not emitted (use --include-deleted)",
            }

    elapsed = time.perf_counter() - t0
    stats: dict[str, Any] = {
        "source": "x_archive",
        "sources_emitted": list(SOURCES),
        "archive": str(archive_path),
        "output": str(output_path),
        "account": {
            "username": username,
            "account_id": account_id,
            "display_name": account.get("accountDisplayName"),
            "created_at": account.get("createdAt"),
        },
        "records": records,
        "by_source": dict(source_counts),
        "roles": dict(role_counts),
        "empty_text_records": empty_text,
        "sections": sections,
        "role_policy": (
            "role=self for authored posts/replies/reposts/notes; "
            "role=null for likes of others"
        ),
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
        f"Done: {records} records "
        f"({', '.join(f'{k}={v}' for k, v in sorted(source_counts.items()))}) "
        f"in {elapsed:.1f}s",
        flush=True,
    )
    return stats


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--archive",
        type=Path,
        default=None,
        help="Path to unzipped X archive root (contains data/ and Your archive.html)",
    )
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    p.add_argument("--stats", type=Path, default=DEFAULT_STATS)
    p.add_argument("--no-stats", action="store_true")
    p.add_argument("--progress-every", type=int, default=2000)
    p.add_argument(
        "--include-deleted",
        action="store_true",
        help="Also emit deleted-tweets.js (off by default)",
    )
    p.add_argument(
        "--no-notes",
        action="store_true",
        help="Skip note-tweet.js long-form records",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    archive = find_archive(args.archive)
    stats_path = None if args.no_stats else args.stats
    normalize(
        archive_path=archive,
        output_path=args.output,
        stats_path=stats_path,
        progress_every=args.progress_every,
        include_deleted=args.include_deleted,
        include_notes=not args.no_notes,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
