#!/usr/bin/env python3
"""
Seed the welcome queue with recent unwelcomed introductions.

Fetches the last 100 messages from #introductions via Discord REST API,
identifies top-level posts that have no bot reply, and queues the oldest
N unwelcomed ones (FIFO order).

Only considers messages newer than the queue watermark (prevents regressing
into old history on each restart). Skips messages by existing members
(joined > NEW_MEMBER_DAYS before posting) to avoid false positives.

Usage:
    source .venv/bin/activate
    python3 bin/seed_welcome_queue.py [--limit N] [--dry-run]
"""
import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import aiohttp

C3PO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(C3PO_DIR / "bin"))

import welcome_queue as wq
from dotenv import load_dotenv

load_dotenv(C3PO_DIR / ".env")

BOT_TOKEN   = os.environ["ORACLE_BOT_TOKEN"]
BOT_USER_ID = "1509294356694044722"
CHANNEL_ID  = os.environ.get("INTRODUCTIONS_CHANNEL_ID", "1082504762433490975")
GUILD_ID    = os.environ.get("DISCORD_GUILD_ID", "1082444651946049567")

NEW_MEMBER_DAYS = 7  # posts within this many days of joining count as intros


async def _discord_get(session: aiohttp.ClientSession, path: str, token: str) -> list | dict:
    url = f"https://discord.com/api/v10{path}"
    async with session.get(url, headers={"Authorization": f"Bot {token}"}) as resp:
        resp.raise_for_status()
        return await resp.json()


def _is_new_member_post(msg_ts_str: str, joined_at_str: str, days: int = NEW_MEMBER_DAYS) -> bool:
    """True if the message was posted within `days` days of the member joining."""
    try:
        msg_ts  = datetime.fromisoformat(msg_ts_str.replace("Z", "+00:00"))
        joined  = datetime.fromisoformat(joined_at_str.replace("Z", "+00:00"))
        return (msg_ts - joined).days <= days
    except Exception:
        return True  # can't parse — fail open


async def seed_queue(
    token: str,
    channel_id: str,
    bot_user_id: str,
    guild_id: str,
    limit: int = 5,
    dry_run: bool = False,
    log=None,
) -> int:
    """
    Fetch recent #introductions, queue any unwelcomed new-member posts.
    Respects the queue watermark (only messages newer than last drain).
    Returns the number of items seeded. Safe to call from on_ready.
    """
    watermark = wq.get_watermark()

    async with aiohttp.ClientSession() as session:
        async def _get(path):
            return await _discord_get(session, path, token)

        messages = await _get(f"/channels/{channel_id}/messages?limit=100")

        # Top-level, non-bot, after watermark
        candidates = [
            m for m in messages
            if not m["author"].get("bot")
            and not m.get("message_reference")
            and (not watermark or m["timestamp"] > watermark)
        ]

        bot_reply_targets = {
            m["message_reference"]["message_id"]
            for m in messages
            if m["author"]["id"] == bot_user_id
            and m.get("message_reference")
        }
        unwelcomed = [m for m in candidates if m["id"] not in bot_reply_targets]

        # Filter by member join date — skip existing members posting in #introductions
        new_member_intros = []
        for m in unwelcomed:
            user_id = m["author"]["id"]
            name    = m["author"].get("global_name") or m["author"]["username"]
            try:
                member     = await _get(f"/guilds/{guild_id}/members/{user_id}")
                joined_at  = member.get("joined_at", "")
                if _is_new_member_post(m["timestamp"], joined_at):
                    new_member_intros.append(m)
                else:
                    if log:
                        log.info(f"Seed: skipping {name} — existing member")
            except Exception as exc:
                if log:
                    log.warning(f"Seed: could not check member {user_id}: {exc}")
                new_member_intros.append(m)  # fail open

        # messages API returns newest-first; reverse to FIFO, take limit
        to_seed = list(reversed(new_member_intros[:limit]))

        seeded = 0
        for m in to_seed:
            name = m["author"].get("global_name") or m["author"]["username"]
            if not dry_run:
                wq.push({
                    "message_id": m["id"],
                    "channel_id": channel_id,
                    "user_id":    m["author"]["id"],
                    "user_name":  name,
                    "intro_text": m["content"][:400],
                    "message_ts": m["timestamp"],
                    "queued_at":  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "attempts":   0,
                })
                seeded += 1
                if log:
                    log.info(f"Startup seed: queued intro from {name} ({m['id']})")

        return seeded


async def main_async(limit: int, dry_run: bool) -> None:
    watermark = wq.get_watermark()
    print(f"Watermark: {watermark or '(none)'}")

    async with aiohttp.ClientSession() as session:
        async def _get(path):
            return await _discord_get(session, path, BOT_TOKEN)

        print(f"Fetching messages from #introductions ({CHANNEL_ID})…")
        messages = await _get(f"/channels/{CHANNEL_ID}/messages?limit=100")
        print(f"  {len(messages)} messages fetched")

        candidates = [
            m for m in messages
            if not m["author"].get("bot")
            and not m.get("message_reference")
            and (not watermark or m["timestamp"] > watermark)
        ]

        bot_reply_targets = {
            m["message_reference"]["message_id"]
            for m in messages
            if m["author"]["id"] == BOT_USER_ID
            and m.get("message_reference")
        }
        unwelcomed = [m for m in candidates if m["id"] not in bot_reply_targets]

        # Check member join dates
        new_member_intros = []
        for m in unwelcomed:
            user_id = m["author"]["id"]
            name    = m["author"].get("global_name") or m["author"]["username"]
            try:
                member    = await _get(f"/guilds/{GUILD_ID}/members/{user_id}")
                joined_at = member.get("joined_at", "")
                if _is_new_member_post(m["timestamp"], joined_at):
                    new_member_intros.append(m)
                else:
                    print(f"  (skip existing member: {name})")
            except Exception as exc:
                print(f"  (cannot check {name}: {exc} — including)")
                new_member_intros.append(m)

        to_seed = list(reversed(new_member_intros[:limit]))
        print(f"  {len(candidates)} candidates after watermark, "
              f"{len(unwelcomed)} unwelcomed, "
              f"{len(new_member_intros)} new-member intros, "
              f"seeding {len(to_seed)}")

        for m in to_seed:
            ts    = (m.get("timestamp") or "")[:10]
            name  = m["author"].get("global_name") or m["author"]["username"]
            blurb = m["content"][:80].replace("\n", " ")
            print(f"  → {name} ({ts}): {blurb}…")

            if not dry_run:
                wq.push({
                    "message_id": m["id"],
                    "channel_id": CHANNEL_ID,
                    "user_id":    m["author"]["id"],
                    "user_name":  name,
                    "intro_text": m["content"][:400],
                    "message_ts": m["timestamp"],
                    "queued_at":  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "attempts":   0,
                })

    if dry_run:
        print("Dry run — nothing written.")
    else:
        print(f"Queue now has {wq.size()} items.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=5, help="Max unwelcomed intros to seed")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    asyncio.run(main_async(args.limit, args.dry_run))


if __name__ == "__main__":
    main()
