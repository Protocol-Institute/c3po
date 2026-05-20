"""
Batch Discord harvester for C3PO.

Polls a whitelist of channels for new messages since last run, embeds qualifying
messages into the Pinecone `discord` namespace, and posts a run summary back to
a designated Discord channel.

No persistent bot process — runs as a cron/launchd job.
Uses the Discord REST API only (no gateway). Requires Message Content Intent
enabled in the Discord Developer Portal.

Usage:
    python3 ingest/sync_discord.py
    python3 ingest/sync_discord.py --dry-run
    python3 ingest/sync_discord.py --backfill-days 30

Required env vars:
    DISCORD_BOT_TOKEN          Bot token from Developer Portal
    DISCORD_CHANNEL_IDS        Comma-separated channel IDs to harvest (whitelist)
    DISCORD_SUMMARY_CHANNEL_ID Channel to post run summaries (optional)

Optional env vars:
    DISCORD_STAR_EMOJI         Emoji name for community starring (default: ⭐)

Filtering logic:
    Original posts:   include if >= 20 chars, OR has URL, OR starred
    Replies:          include if >= 150 chars, OR has URL, OR starred
    Thread starters:  always include; full thread fetched and bundled as one chunk
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
from utils import clean_text, embed_chunks, get_voyage_client, get_pinecone_index, PINECONE_BATCH

load_dotenv(Path(__file__).parent.parent / ".env")

NAMESPACE = "discord"
STATE_PATH = Path(__file__).parent.parent / "data" / "discord_state.json"
DISCORD_API = "https://discord.com/api/v10"

ORIG_MIN_CHARS = 20    # floor for original posts (fortune-cookie thoughts)
REPLY_MIN_CHARS = 150  # floor for replies (skip short acks)
INITIAL_BACKFILL_DAYS = 30

URL_RE = re.compile(r'https?://\S+')


# ── Discord REST client ────────────────────────────────────────────────────────

def discord_request(method: str, path: str, payload: dict | None = None):
    token = os.environ["DISCORD_BOT_TOKEN"]
    url = f"{DISCORD_API}{path}"
    data = json.dumps(payload).encode() if payload else None
    headers = {
        "Authorization": f"Bot {token}",
        "User-Agent": "C3PO-Ingest/1.0",
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    while True:
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return None if r.status == 204 else json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry_after = float(json.loads(e.read()).get("retry_after", 1.0))
                print(f"  Rate limited — sleeping {retry_after:.1f}s")
                time.sleep(retry_after + 0.1)
                continue
            raise


def fetch_channel_name(channel_id: str) -> str:
    try:
        ch = discord_request("GET", f"/channels/{channel_id}")
        return ch.get("name", channel_id)
    except Exception:
        return channel_id


def fetch_messages_since(channel_id: str, after_id: str | None,
                         cutoff_dt: datetime | None = None) -> list[dict]:
    """
    Fetch messages from a channel, returning them oldest-first.

    If after_id is set: page forward with `after` (normal incremental sync).
    If after_id is None: page backward with `before` until cutoff_dt (backfill).
    """
    if after_id:
        # Incremental: page forward from checkpoint
        messages = []
        last_id = after_id
        while True:
            params = {"limit": 100, "after": last_id}
            batch = discord_request("GET", f"/channels/{channel_id}/messages?{urllib.parse.urlencode(params)}")
            if not batch:
                break
            batch.sort(key=lambda m: int(m["id"]))  # oldest-first
            messages.extend(batch)
            if len(batch) < 100:
                break
            last_id = batch[-1]["id"]
            time.sleep(1.1)
        return messages
    else:
        # Backfill: page backward using `before` until we pass cutoff_dt
        messages = []
        before_id = None
        while True:
            params = {"limit": 100}
            if before_id:
                params["before"] = before_id
            batch = discord_request("GET", f"/channels/{channel_id}/messages?{urllib.parse.urlencode(params)}")
            if not batch:
                break
            # Discord returns newest-first when using `before`; last element is oldest
            oldest_in_batch = batch[-1]
            before_id = oldest_in_batch["id"]  # cursor for next page

            if cutoff_dt:
                filtered = [m for m in batch if snowflake_to_dt(m["id"]) >= cutoff_dt]
                messages.extend(filtered)
                # Stop once the oldest message in this page is before the cutoff
                if snowflake_to_dt(oldest_in_batch["id"]) < cutoff_dt:
                    break
            else:
                messages.extend(batch)

            if len(batch) < 100:
                break
            time.sleep(1.1)

        messages.sort(key=lambda m: int(m["id"]))  # return oldest-first
        return messages


def fetch_thread_messages(thread_id: str) -> list[dict]:
    """Fetch all messages in a thread channel, oldest-first."""
    messages = []
    last_id = None
    while True:
        params = {"limit": 100}
        if last_id:
            params["after"] = last_id
        try:
            batch = discord_request("GET", f"/channels/{thread_id}/messages?{urllib.parse.urlencode(params)}")
        except urllib.error.HTTPError:
            break
        if not batch:
            break
        batch.sort(key=lambda m: int(m["id"]))
        messages.extend(batch)
        if len(batch) < 100:
            break
        last_id = batch[-1]["id"]
        time.sleep(1.1)
    return messages


def post_to_channel(channel_id: str, text: str):
    discord_request("POST", f"/channels/{channel_id}/messages", {"content": text})


# ── Message processing ─────────────────────────────────────────────────────────

def snowflake_to_dt(snowflake: str) -> datetime:
    ts_ms = (int(snowflake) >> 22) + 1420070400000
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)


def get_star_count(message: dict) -> int:
    star_emoji = os.environ.get("DISCORD_STAR_EMOJI", "⭐")
    for reaction in message.get("reactions", []):
        name = reaction.get("emoji", {}).get("name", "")
        if name in (star_emoji, "⭐"):
            return reaction.get("count", 0)
    return 0


def extract_urls(content: str) -> list[str]:
    return URL_RE.findall(content)


def is_reply(msg: dict) -> bool:
    return msg.get("type") == 19 or "message_reference" in msg


def qualifies(msg: dict, content: str, star_count: int, urls: list[str]) -> bool:
    if "thread" in msg:
        return True  # thread starters always included; full thread will be bundled
    if is_reply(msg):
        return len(content) >= REPLY_MIN_CHARS or star_count > 0 or bool(urls)
    else:
        return len(content) >= ORIG_MIN_CHARS or star_count > 0 or bool(urls)


def format_thread_chunk(starter: dict, thread_msgs: list[dict],
                        channel_name: str) -> tuple[str, dict]:
    """Bundle a thread starter + its replies into one text chunk with metadata."""
    ts = snowflake_to_dt(starter["id"]).strftime("%Y-%m-%dT%H:%M:%SZ")
    author = starter.get("author", {}).get("username", "unknown")
    starter_text = clean_text(starter.get("content", ""))
    thread_id = starter.get("thread", {}).get("id", starter["id"])

    lines = [f"Discord #{channel_name} | Thread started by @{author} | {ts}", "", starter_text]

    for msg in thread_msgs:
        if msg.get("author", {}).get("bot"):
            continue
        reply_author = msg.get("author", {}).get("username", "unknown")
        reply_text = clean_text(msg.get("content", ""))
        if reply_text:
            reply_ts = snowflake_to_dt(msg["id"]).strftime("%Y-%m-%dT%H:%M:%SZ")
            lines.append(f"\n  @{reply_author} ({reply_ts}): {reply_text}")

    all_authors = list(dict.fromkeys(
        [author] + [m.get("author", {}).get("username", "") for m in thread_msgs
                    if not m.get("author", {}).get("bot")]
    ))
    all_urls = extract_urls(starter_text) + [
        u for m in thread_msgs for u in extract_urls(m.get("content", ""))
    ]
    stars = get_star_count(starter)

    text = "\n".join(lines)
    meta = {
        "source": "discord",
        "namespace": NAMESPACE,
        "chunk_type": "thread",
        "guild_id": os.environ.get("DISCORD_GUILD_ID", ""),
        "channel_id": starter.get("channel_id", ""),
        "channel_name": channel_name,
        "thread_id": thread_id,
        "author": author,
        "all_authors": json.dumps(all_authors),
        "timestamp": ts,
        "message_id": starter["id"],
        "reply_count": len([m for m in thread_msgs if not m.get("author", {}).get("bot")]),
        "star_count": stars,
        "urls": json.dumps(all_urls[:10]),
        "has_url": bool(all_urls),
        "text": text[:1000],
    }
    return text, meta


def format_message_chunk(msg: dict, channel_name: str) -> tuple[str, dict]:
    """Format a standalone message (original post or reply) as a chunk."""
    content = msg.get("content", "")
    ts = snowflake_to_dt(msg["id"]).strftime("%Y-%m-%dT%H:%M:%SZ")
    author = msg.get("author", {}).get("username", "unknown")
    chunk_type = "reply" if is_reply(msg) else "original"
    urls = extract_urls(content)
    stars = get_star_count(msg)

    text = f"Discord #{channel_name} | @{author} | {ts}\n\n{clean_text(content)}"
    meta = {
        "source": "discord",
        "namespace": NAMESPACE,
        "chunk_type": chunk_type,
        "guild_id": os.environ.get("DISCORD_GUILD_ID", ""),
        "channel_id": msg.get("channel_id", ""),
        "channel_name": channel_name,
        "author": author,
        "timestamp": ts,
        "message_id": msg["id"],
        "star_count": stars,
        "urls": json.dumps(urls[:10]),
        "has_url": bool(urls),
        "text": text[:1000],
    }
    return text, meta


# ── State persistence ──────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"last_message_ids": {}, "last_run": None}


def save_state(state: dict):
    STATE_PATH.write_text(json.dumps(state, indent=2))


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--backfill-days", type=int,
                        help="Harvest this many days back, ignoring saved state")
    args = parser.parse_args()

    channel_ids = [c.strip() for c in os.environ.get("DISCORD_CHANNEL_IDS", "").split(",") if c.strip()]
    if not channel_ids:
        print("ERROR: DISCORD_CHANNEL_IDS not set")
        sys.exit(1)

    summary_channel_id = os.environ.get("DISCORD_SUMMARY_CHANNEL_ID", "")
    state = load_state()
    vc = None if args.dry_run else get_voyage_client()
    index = None if args.dry_run else get_pinecone_index()

    run_ts = datetime.now(timezone.utc)
    total_ingested = 0
    total_threads = 0
    total_starred = 0
    total_urls = 0
    channel_reports = []

    for channel_id in channel_ids:
        channel_name = fetch_channel_name(channel_id)

        if args.backfill_days:
            after_id = None
            cutoff_dt = run_ts - timedelta(days=args.backfill_days)
        elif channel_id in state["last_message_ids"]:
            after_id = state["last_message_ids"][channel_id]
            cutoff_dt = None
        else:
            after_id = None
            cutoff_dt = run_ts - timedelta(days=INITIAL_BACKFILL_DAYS)

        print(f"\n#{channel_name}")
        messages = fetch_messages_since(channel_id, after_id, cutoff_dt)
        print(f"  {len(messages)} messages fetched")

        new_last_id = after_id
        records = []
        ch_threads = 0
        ch_starred = 0
        ch_urls = 0

        for msg in messages:
            msg_id = msg["id"]
            if new_last_id is None or int(msg_id) > int(new_last_id):
                new_last_id = msg_id

            if msg.get("author", {}).get("bot"):
                continue

            content = msg.get("content", "")
            stars = get_star_count(msg)
            urls = extract_urls(content)

            if not qualifies(msg, content, stars, urls):
                continue

            if "thread" in msg:
                # Fetch and bundle the full thread
                thread_id = msg["thread"]["id"]
                thread_msgs = fetch_thread_messages(thread_id)
                text, meta = format_thread_chunk(msg, thread_msgs, channel_name)
                record_id = f"discord__thread__{thread_id}"
                ch_threads += 1
                ch_urls += len(json.loads(meta["urls"]))
                if stars:
                    ch_starred += 1
            else:
                text, meta = format_message_chunk(msg, channel_name)
                record_id = f"discord__{msg_id}"
                ch_urls += len(urls)
                if stars:
                    ch_starred += 1

            records.append({"id": record_id, "text": text, "meta": meta})

        if not args.dry_run and records:
            vectors = embed_chunks([r["text"] for r in records], vc)
            pinecone_records = [
                {"id": r["id"], "values": v, "metadata": r["meta"]}
                for r, v in zip(records, vectors)
            ]
            for i in range(0, len(pinecone_records), PINECONE_BATCH):
                index.upsert(vectors=pinecone_records[i:i + PINECONE_BATCH], namespace=NAMESPACE)

        if new_last_id and not args.dry_run:
            state["last_message_ids"][channel_id] = new_last_id

        n = len(records)
        total_ingested += n
        total_threads += ch_threads
        total_starred += ch_starred
        total_urls += ch_urls
        channel_reports.append((channel_name, n, len(messages), ch_threads, ch_starred, ch_urls))
        print(f"  Ingested: {n}  (threads: {ch_threads}  ⭐: {ch_starred}  🔗: {ch_urls})")

    if not args.dry_run:
        state["last_run"] = run_ts.strftime("%Y-%m-%dT%H:%M:%SZ")
        save_state(state)

    # ── Summary ────────────────────────────────────────────────────────────────
    label = "[DRY RUN] " if args.dry_run else ""
    ts_label = run_ts.strftime("%Y-%m-%d %H:%M UTC")

    ch_lines = "\n".join(
        f"  **#{name}** — {n} ingested of {scanned} new"
        + (f" ({threads} threads" + ("⭐" if starred else "") + ")" if threads or starred else "")
        for name, n, scanned, threads, starred, _ in channel_reports
    )
    summary = (
        f"**{label}C3PO Discord Harvest** — {ts_label}\n"
        f"{'─' * 40}\n"
        f"{ch_lines}\n"
        f"⭐ Starred: {total_starred}  |  🔗 URLs: {total_urls}  |  🧵 Threads: {total_threads}  |  "
        f"📥 Total: {total_ingested}"
    )

    print(f"\n{summary}")

    if not args.dry_run and summary_channel_id and total_ingested > 0:
        post_to_channel(summary_channel_id, summary)
        print(f"Summary posted to channel {summary_channel_id}")
    elif not summary_channel_id:
        print("(DISCORD_SUMMARY_CHANNEL_ID not set — skipping Discord post)")


if __name__ == "__main__":
    main()
