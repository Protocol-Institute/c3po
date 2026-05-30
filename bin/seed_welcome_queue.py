#!/usr/bin/env python3
"""
Seed the welcome queue with recent unwelcomed introductions.

Fetches the last 100 messages from #introductions via Discord REST API,
identifies top-level posts that have no bot reply, and queues the oldest
N unwelcomed ones (FIFO order).

Usage:
    source .venv/bin/activate
    python3 bin/seed_welcome_queue.py [--limit N] [--dry-run]
"""
import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

C3PO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(C3PO_DIR / "bin"))

import welcome_queue as wq
from dotenv import load_dotenv
import os

load_dotenv(C3PO_DIR / ".env")

BOT_TOKEN   = os.environ["ORACLE_BOT_TOKEN"]
BOT_USER_ID = "1509294356694044722"
CHANNEL_ID  = os.environ.get("INTRODUCTIONS_CHANNEL_ID", "1082504762433490975")


async def discord_get(session: aiohttp.ClientSession, path: str) -> list | dict:
    url = f"https://discord.com/api/v10{path}"
    async with session.get(url, headers={"Authorization": f"Bot {BOT_TOKEN}"}) as resp:
        resp.raise_for_status()
        return await resp.json()


async def main_async(limit: int, dry_run: bool) -> None:
    async with aiohttp.ClientSession() as session:
        print(f"Fetching messages from #introductions ({CHANNEL_ID})…")
        messages = await discord_get(session, f"/channels/{CHANNEL_ID}/messages?limit=100")
        print(f"  {len(messages)} messages fetched")

        # Top-level intro posts (not from bots, no message_reference)
        intros = [
            m for m in messages
            if not m["author"].get("bot")
            and not m.get("message_reference")
        ]

        # Bot reply targets — message IDs the bot has already replied to
        bot_reply_targets = {
            m["message_reference"]["message_id"]
            for m in messages
            if m["author"]["id"] == BOT_USER_ID
            and m.get("message_reference")
        }

        # Unwelcomed = no bot reply in the fetched window
        unwelcomed = [m for m in intros if m["id"] not in bot_reply_targets]

        # messages API returns newest-first; reverse to FIFO order, take --limit
        to_seed = list(reversed(unwelcomed[:limit]))

        print(f"  {len(intros)} intros, {len(unwelcomed)} unwelcomed, seeding {len(to_seed)}")

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
