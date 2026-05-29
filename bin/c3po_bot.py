#!/usr/bin/env python3
"""
C3PO Discord bot — mention-based RAG interface.

Mention @c3po with a question; the bot opens a thread and replies.
Continues responding in bot-created threads for up to MAX_THREAD_TURNS turns.
Monitors #introductions for new member introductions.

Requires: ORACLE_BOT_TOKEN in .env
Message Content Intent must be enabled in the Discord Developer Portal.

Usage:
    source .venv/bin/activate
    python3 -u bin/c3po_bot.py
"""

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import discord
import hashlib
import json
import time
from dotenv import load_dotenv

# ── Config ────────────────────────────────────────────────────────────────────

C3PO_DIR = Path(__file__).resolve().parent.parent
load_dotenv(C3PO_DIR / ".env")

BOT_ID       = "c3po_bot"
SESSION_LOG  = Path.home() / "Library" / "Logs" / "c3po" / "bot_sessions.jsonl"
SPOOL_DIR    = C3PO_DIR / "data" / "spool" / "bot_conversations"

WORKER_URL    = "https://c3po.vgr-702.workers.dev/query"
BOT_TOKEN     = os.environ["ORACLE_BOT_TOKEN"]
MAX_QUERY_LEN = 500
MAX_MSG_LEN   = 2000
MAX_THREAD_TURNS = 5

ORACLE_ROLE_ID           = int(os.environ.get("ORACLE_ROLE_ID", "1509298797040107543"))
INTRODUCTIONS_CHANNEL_ID = int(os.environ.get("INTRODUCTIONS_CHANNEL_ID", "0"))

# SIG channels — used in introductions recommendations
# Channel mention format in Discord: <#CHANNEL_ID>
SIG_CHANNELS = {
    "SIGFPT":    {"id": 1327337414175490160, "desc": "Formal Protocol Theory — mathematical/logical foundations"},
    "MRG":       {"id": 1379992696114122832, "desc": "Memory Research Group — protocols of memory and archiving"},
    "SIGPfB":    {"id": 1333851496416153702, "desc": "Protocols for Business — organizational and business protocols"},
    "ProtFiSIG": {"id": 1106572787042238504, "desc": "Protocol Fiction — speculative and imaginative narratives"},
}

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("c3po.bot")


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash_user(user_id: int) -> str:
    return hashlib.sha256(str(user_id).encode()).hexdigest()[:12]


def log_session(record: dict) -> None:
    """Append one JSON record to the session audit log."""
    try:
        SESSION_LOG.parent.mkdir(parents=True, exist_ok=True)
        with SESSION_LOG.open("a") as f:
            f.write(json.dumps({**record, "bot_id": BOT_ID}) + "\n")
    except Exception as exc:
        log.warning(f"session log write failed: {exc}")


def spool_conversation(record: dict) -> None:
    """Write a completed Q&A exchange to the spool for ingestion by daemon."""
    try:
        SPOOL_DIR.mkdir(parents=True, exist_ok=True)
        fname = SPOOL_DIR / f"{record['thread_id']}_{record['turn']}.json"
        with fname.open("w") as f:
            json.dump(record, f, ensure_ascii=False)
    except Exception as exc:
        log.warning(f"spool write failed: {exc}")

# ── Discord client ────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True   # privileged — enable in Dev Portal → Bot tab

client = discord.Client(intents=intents)

# ── Helpers ───────────────────────────────────────────────────────────────────

def format_sources(sources: list) -> str:
    lines = []
    for s in sources[:3]:
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


async def call_worker(query: str, history: list | None = None, max_tokens: int = 300) -> dict | None:
    payload: dict = {"query": query, "max_tokens": max_tokens}
    if history:
        payload["history"] = history
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                WORKER_URL,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status != 200:
                    log.error(f"Worker {resp.status}: {(await resp.text())[:200]}")
                    return None
                return await resp.json()
    except Exception as exc:
        log.error(f"Worker request failed: {exc}")
        return None


async def send_answer(target, data: dict) -> None:
    cache_hits = data.get("cache_hits") or []
    web_hits   = [h for h in cache_hits if h.get("url")]

    answer  = (data.get("answer") or "").strip()
    sources = [s for s in (data.get("sources") or []) if s.get("source") != "transcript"]

    if web_hits:
        hit = web_hits[0]
        url = hit.get("url")
        await target.send(
            f"**Similar conversation:** <{url}>\n"
            f"*(A similar question was answered before — my response below)*"
        )

    if not answer:
        await target.send("No answer returned — the corpus may not cover this topic.")
        return
    for chunk in split_message(answer):
        await target.send(chunk)
    sources_text = format_sources(sources)
    if sources_text:
        await target.send(f"**Sources**\n{sources_text}")


# ── Thread continuation ───────────────────────────────────────────────────────

async def handle_thread_reply(message: discord.Message) -> None:
    thread = message.channel

    # Collect prior thread messages (exclude the just-received message)
    prior: list[discord.Message] = []
    async for msg in thread.history(oldest_first=True, limit=50):
        if msg.id != message.id:
            prior.append(msg)

    # Count completed bot turns (skip sources-only messages)
    bot_turns = sum(
        1 for m in prior
        if m.author.id == client.user.id and not m.content.startswith("**Sources**")
    )

    if bot_turns >= MAX_THREAD_TURNS:
        await thread.send(
            f"We've reached the {MAX_THREAD_TURNS}-turn limit for this thread. "
            "For a longer conversation, use the web interface: https://protocolized.io/resources"
        )
        return

    query = message.content.strip()
    if not query:
        return

    log.info(f"Thread reply [{message.author}] turn={bot_turns + 1}: {query[:80]}")

    # Build history starting with the original trigger query.
    # For message-threads, Discord sets thread.id == original message.id.
    history: list[dict] = []
    try:
        parent = client.get_channel(thread.parent_id)
        if parent:
            orig = await parent.fetch_message(thread.id)
            orig_text = orig.content
            for u in orig.mentions:
                orig_text = orig_text.replace(f"<@{u.id}>", "").replace(f"<@!{u.id}>", "")
            for r in orig.role_mentions:
                orig_text = orig_text.replace(f"<@&{r.id}>", "")
            orig_text = "".join(c for c in orig_text if c.isprintable()).strip()
            if orig_text:
                history.append({"role": "user", "content": orig_text})
    except Exception:
        pass

    if not history:
        history.append({"role": "user", "content": thread.name})

    # Append subsequent thread exchanges; merge consecutive bot chunks
    for msg in prior:
        if msg.author.id == client.user.id:
            if msg.content.startswith("**Sources**"):
                continue
            if history and history[-1]["role"] == "assistant":
                history[-1]["content"] += "\n" + msg.content
            else:
                history.append({"role": "assistant", "content": msg.content})
        elif not msg.author.bot:
            history.append({"role": "user", "content": msg.content})

    t0 = time.monotonic()
    async with thread.typing():
        data = await call_worker(query, history=history)
    latency_ms = int((time.monotonic() - t0) * 1000)

    if data is None:
        await thread.send("The oracle is unavailable right now — try again in a moment.")
        return

    try:
        await send_answer(thread, data)
    except discord.HTTPException as exc:
        log.error(f"Failed to send thread reply: {exc}")

    log_session({
        "event":      "thread_reply",
        "ts":         _ts(),
        "type":       "thread",
        "user_hash":  _hash_user(message.author.id),
        "thread_id":  str(thread.id),
        "turn":       bot_turns + 1,
        "query_len":  len(query),
        "answer_len": len((data.get("answer") or "")),
        "sources":    len(data.get("sources") or []),
        "latency_ms": latency_ms,
    })
    spool_conversation({
        "bot_id":        BOT_ID,
        "ts":            _ts(),
        "user_id_hash":  _hash_user(message.author.id),
        "channel_id":    str(thread.parent_id or thread.id),
        "thread_id":     str(thread.id),
        "question":      query,
        "answer":        (data.get("answer") or ""),
        "sources":       data.get("sources") or [],
        "turn":          bot_turns + 1,
        "latency_ms":    latency_ms,
    })


# ── Introductions monitoring ──────────────────────────────────────────────────

_SIG_PROMPT_LINES = "\n".join(
    f"- {name} (<#{info['id']}>) — {info['desc']}"
    for name, info in SIG_CHANNELS.items()
)


async def handle_introduction(message: discord.Message) -> None:
    # Only respond to top-level posts, not replies within #introductions
    if message.reference is not None:
        return

    intro_text = message.content[:400]
    log.info(f"Introduction from [{message.author}]: {intro_text[:80]}")

    prompt = (
        f"A new member just posted their introduction in the Protocol Institute Discord:\n\n"
        f"---\n{intro_text}\n---\n\n"
        f"Based on their stated interests, do two things:\n"
        f"1. Recommend ONE specific article, essay, or resource from our corpus that would be "
        f"most relevant to them. Give a 1–2 sentence explanation of why.\n"
        f"2. Recommend the ONE most relevant SIG (special interest group) from this list, "
        f"mentioning it by its Discord channel mention exactly as shown:\n"
        f"{_SIG_PROMPT_LINES}\n\n"
        f"Explain in one sentence why that SIG fits their interests. "
        f"Keep the total response to 3–5 sentences. Be warm but concise. "
        f"Do not repeat back the introduction text."
    )

    t0 = time.monotonic()
    async with message.channel.typing():
        data = await call_worker(prompt, max_tokens=400)
    latency_ms = int((time.monotonic() - t0) * 1000)

    if data is None:
        return

    answer  = (data.get("answer") or "").strip()
    sources = data.get("sources") or []
    if not answer:
        return

    reply = f"Welcome, {message.author.mention}!\n\n{answer}"

    if sources:
        src      = sources[0]
        label    = src.get("label") or src.get("source", "").upper()
        title    = src.get("title") or src.get("url") or "(untitled)"
        url      = src.get("url")
        date     = (src.get("date") or "")[:7]
        date_str = f" ({date})" if date else ""
        link_str = f" — <{url}>" if url else ""
        reply += f"\n\n**Suggested reading:** [{label}] {title}{date_str}{link_str}"

    try:
        await message.reply(reply, mention_author=False)
    except discord.HTTPException as exc:
        log.error(f"Failed to send intro reply: {exc}")

    log_session({
        "event":      "introduction",
        "ts":         _ts(),
        "type":       "introduction",
        "user_hash":  _hash_user(message.author.id),
        "intro_len":  len(intro_text),
        "answer_len": len(answer),
        "sources":    len(sources),
        "latency_ms": latency_ms,
    })


# ── Events ────────────────────────────────────────────────────────────────────

GREETINGS = {"", "hey", "hi", "hello", "yo", "sup", "hiya", "howdy", "greetings"}


@client.event
async def on_ready():
    log.info(f"C3PO bot ready — {client.user} (id={client.user.id})")
    log_session({"event": "startup", "ts": _ts(), "user": str(client.user), "user_id": client.user.id})


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # Thread continuation — respond to any message in bot-owned threads
    if isinstance(message.channel, discord.Thread):
        if message.channel.owner_id == client.user.id:
            await handle_thread_reply(message)
        return

    # Introductions monitoring (set INTRODUCTIONS_CHANNEL_ID in .env to enable)
    if INTRODUCTIONS_CHANNEL_ID and message.channel.id == INTRODUCTIONS_CHANNEL_ID:
        await handle_introduction(message)
        return

    # Mention-based query (main channels)
    is_user_mention = client.user in message.mentions
    is_role_mention = any(r.id == ORACLE_ROLE_ID for r in message.role_mentions)
    if not is_user_mention and not is_role_mention:
        return

    # Strip user and role mentions, then non-printable Unicode chars
    query = message.content
    for u in message.mentions:
        query = query.replace(f"<@{u.id}>", "").replace(f"<@!{u.id}>", "")
    for r in message.role_mentions:
        query = query.replace(f"<@&{r.id}>", "")
    query = "".join(c for c in query if c.isprintable()).strip()

    log.info(f"Mention from [{message.author}] query={query!r}")

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

    t0 = time.monotonic()
    async with thread.typing():
        data = await call_worker(query)
    latency_ms = int((time.monotonic() - t0) * 1000)

    if data is None:
        await thread.send("The oracle is unavailable right now — try again in a moment.")
        return

    try:
        await send_answer(thread, data)
    except discord.HTTPException as exc:
        log.error(f"Failed to send to thread: {exc}")

    log_session({
        "event":       "conversation",
        "ts":          _ts(),
        "type":        "mention",
        "user_hash":   _hash_user(message.author.id),
        "channel":     getattr(message.channel, "name", str(message.channel.id)),
        "thread_id":   str(thread.id),
        "query_len":   len(query),
        "answer_len":  len((data.get("answer") or "")),
        "sources":     len(data.get("sources") or []),
        "latency_ms":  latency_ms,
    })
    spool_conversation({
        "bot_id":        BOT_ID,
        "ts":            _ts(),
        "user_id_hash":  _hash_user(message.author.id),
        "channel_id":    str(message.channel.id),
        "thread_id":     str(thread.id),
        "question":      query,
        "answer":        (data.get("answer") or ""),
        "sources":       data.get("sources") or [],
        "turn":          1,
        "latency_ms":    latency_ms,
    })


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    log.info("Starting C3PO bot…")
    client.run(BOT_TOKEN)
