#!/usr/bin/env python3
"""
C3PO Discord bot — mention-based RAG interface.

Mention @c3po with a question; the bot opens a thread and replies with
an answer + sources from the C3PO Worker.

Requires: ORACLE_BOT_TOKEN in .env
Message Content Intent must be enabled in the Discord Developer Portal.

Usage:
    source .venv/bin/activate
    python3 bin/c3po_bot.py
"""

import logging
import os
import sys
from pathlib import Path

import aiohttp
import discord
from dotenv import load_dotenv

# ── Config ────────────────────────────────────────────────────────────────────

C3PO_DIR = Path(__file__).resolve().parent.parent
load_dotenv(C3PO_DIR / ".env")

WORKER_URL    = "https://c3po.vgr-702.workers.dev/query"
BOT_TOKEN     = os.environ["ORACLE_BOT_TOKEN"]
MAX_QUERY_LEN = 500
MAX_MSG_LEN   = 2000

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("c3po.bot")

# ── Discord client ────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True   # privileged — enable in Dev Portal → Bot tab

client = discord.Client(intents=intents)

# When the bot joins a server Discord creates a managed role with the same name.
# Users often @mention the role instead of the bot user — treat both as triggers.
ORACLE_ROLE_ID = int(os.environ.get("ORACLE_ROLE_ID", "1509298797040107543"))

# ── Helpers ───────────────────────────────────────────────────────────────────

def format_sources(sources: list) -> str:
    lines = []
    for s in sources[:6]:
        label = s.get("label") or s.get("source", "").upper()
        title = s.get("title") or s.get("url") or "(untitled)"
        url   = s.get("url")
        date  = (s.get("date") or "")[:7]
        date_str = f" — {date}" if date else ""
        link_str = f"\n  <{url}>" if url else ""
        lines.append(f"**[{label}]** {title}{date_str}{link_str}")
    return "\n".join(lines)


def split_message(text: str) -> list[str]:
    """Split at paragraph boundaries to stay under Discord's 2000-char limit."""
    if len(text) <= MAX_MSG_LEN:
        return [text]
    chunks = []
    while text:
        if len(text) <= MAX_MSG_LEN:
            chunks.append(text)
            break
        cut = text.rfind("\n", 0, MAX_MSG_LEN)
        if cut == -1:
            cut = MAX_MSG_LEN
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks

# ── Events ────────────────────────────────────────────────────────────────────

@client.event
async def on_ready():
    log.info(f"C3PO bot ready — {client.user} (id={client.user.id})")


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    is_user_mention = client.user in message.mentions
    is_role_mention = any(r.id == ORACLE_ROLE_ID for r in message.role_mentions)
    if not is_user_mention and not is_role_mention:
        return

    # Strip user and role mentions, then remove non-printable Unicode chars
    query = message.content
    for u in message.mentions:
        query = query.replace(f"<@{u.id}>", "").replace(f"<@!{u.id}>", "")
    for r in message.role_mentions:
        query = query.replace(f"<@&{r.id}>", "")
    query = "".join(c for c in query if c.isprintable()).strip()

    log.info(f"Mention from [{message.author}] query={query!r}")

    GREETINGS = {"", "hey", "hi", "hello", "yo", "sup", "hiya", "howdy", "greetings"}
    if query.lower() in GREETINGS:
        await message.reply(
            "Hi, I'm c3po, the Protocol Institute oracle. Ask me about anything in our archives. "
            "We can have short exchanges here, but for extended chat use the web interface: "
            "https://protocolized.io/resources",
            mention_author=False,
        )
        return

    if len(query) > MAX_QUERY_LEN:
        await message.reply(
            f"Query too long (max {MAX_QUERY_LEN} chars) — please shorten it.",
            mention_author=False,
        )
        return

    thread_name = (query[:77] + "…") if len(query) > 80 else query
    log.info(f"Query [{message.author}] #{getattr(message.channel, 'name', '?')}: {query[:80]}")

    try:
        thread = await message.create_thread(name=thread_name, auto_archive_duration=60)
    except discord.HTTPException as exc:
        log.error(f"Thread creation failed: {exc}")
        await message.reply("Couldn't open a thread here.", mention_author=False)
        return

    async with thread.typing():
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    WORKER_URL,
                    json={"query": query, "max_tokens": 300},
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    if resp.status != 200:
                        err = await resp.text()
                        log.error(f"Worker {resp.status}: {err[:200]}")
                        if resp.status == 400:
                            await thread.send("I didn't catch a question — try mentioning me with something to ask.")
                        else:
                            await thread.send("The oracle is unavailable right now — try again in a moment.")
                        return
                    data = await resp.json()
        except Exception as exc:
            log.error(f"Worker request failed: {exc}")
            await thread.send("Failed to reach the oracle. Check network or Worker status.")
            return

    answer  = (data.get("answer") or "").strip()
    sources = data.get("sources") or []
    log.info(f"Worker OK — answer {len(answer)} chars, {len(sources)} sources")

    if not answer:
        await thread.send("No answer returned — the corpus may not cover this topic.")
        return

    try:
        for chunk in split_message(answer):
            await thread.send(chunk)
        sources_text = format_sources(sources[:3])
        if sources_text:
            await thread.send(f"**Sources**\n{sources_text}")
    except discord.HTTPException as exc:
        log.error(f"Failed to send to thread: {exc}")

# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    log.info("Starting C3PO bot…")
    client.run(BOT_TOKEN)
