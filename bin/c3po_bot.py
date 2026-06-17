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
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import session_log as slog
import welcome_queue as wq
import seed_welcome_queue as swq
import intro_quality

import asyncio

import aiohttp
import discord
import hashlib
import json
import time
import voyageai
from dotenv import load_dotenv
from pinecone import Pinecone

# ── Config ────────────────────────────────────────────────────────────────────

C3PO_DIR = Path(__file__).resolve().parent.parent
load_dotenv(C3PO_DIR / ".env")

BOT_ID       = "c3po_bot"
SESSION_LOG  = Path.home() / "Library" / "Logs" / "c3po" / "bot_sessions.jsonl"
SPOOL_DIR    = C3PO_DIR / "data" / "spool" / "bot_conversations"

WORKER_URL    = "https://c3po.protocolized.io/query"
BOT_TOKEN     = os.environ["ORACLE_BOT_TOKEN"]
MAX_QUERY_LEN = 500
MAX_MSG_LEN   = 2000
MAX_THREAD_TURNS = 5

ORACLE_ROLE_ID           = int(os.environ.get("ORACLE_ROLE_ID", "1509298797040107543"))
INTRODUCTIONS_CHANNEL_ID = int(os.environ.get("INTRODUCTIONS_CHANNEL_ID", "0"))
DISCORD_GUILD_ID         = os.environ.get("DISCORD_GUILD_ID", "1082444651946049567")

DISCORD_CHANNELS_PATH = C3PO_DIR / "config" / "discord_channels.json"
VOYAGE_MODEL          = "voyage-3"

# Fallback channels when guide query returns no SIG match
FALLBACK_CHANNEL_IDS = [
    "1084135714830164100",   # #🤔-idle-protocol-musings  (PLAZA)
    "1095846506382250075",   # #🔍-protocol-watch         (PLAZA)
]

# SIG meta-channels to skip when recommending to newcomers
_SIG_META_NAMES = {"sig-talk", "sig-hosts-best-practices"}

# Nav-intent queries: "where should I post about X", "which channel for Y", etc.
_NAV_RE = re.compile(
    r"\b("
    r"where\s+(should|can|do|would)\s+(i|we)\s+(post|ask|discuss|share|talk)"
    r"|which\s+channel\s+(for|to|about|covers?)"
    r"|what\s+channel\s+(for|to|is|about|covers?)"
    r"|where\s+to\s+(post|ask|discuss|share|talk)"
    r"|where\s+is\s+(the\s+)?(best|right|good)\s+channel"
    r"|any\s+channel\s+(for|about)"
    r")\b",
    re.IGNORECASE,
)

# ── Discord guide query (direct Pinecone) ─────────────────────────────────────

_voyage_client:   voyageai.Client | None = None
_pinecone_index = None


def _guide_clients():
    global _voyage_client, _pinecone_index
    if _voyage_client is None:
        _voyage_client = voyageai.Client(api_key=os.environ["VOYAGE_API_KEY"])
    if _pinecone_index is None:
        pc = Pinecone(api_key=os.environ["PINECONE_API_KEY"])
        _pinecone_index = pc.Index(host=os.environ["PINECONE_C3PO_HOST"])
    return _voyage_client, _pinecone_index


def _sync_query_guide(text: str, top_k: int = 8) -> list[dict]:
    vc, idx = _guide_clients()
    result = vc.embed([text], model=VOYAGE_MODEL, input_type="query")
    vector = result.embeddings[0]
    resp = idx.query(
        vector=vector,
        top_k=top_k,
        namespace="discord_guide",
        filter={"recommend_to_newcomers": True, "status": "active"},
        include_metadata=True,
    )
    return [m.metadata for m in resp.matches]


async def query_discord_guide(text: str) -> list[dict]:
    """Embed text and query discord_guide; runs sync SDK in thread executor."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_query_guide, text)


def _sync_query_guide_nav(text: str, top_k: int = 5) -> list[dict]:
    """Nav-mode guide query — no newcomer filter, returns active channels only."""
    vc, idx = _guide_clients()
    result = vc.embed([text], model=VOYAGE_MODEL, input_type="query")
    vector = result.embeddings[0]
    resp = idx.query(
        vector=vector,
        top_k=top_k,
        namespace="discord_guide",
        filter={"status": "active"},
        include_metadata=True,
    )
    return [(m.score, m.metadata) for m in resp.matches]


async def query_discord_guide_nav(text: str) -> list[tuple[float, dict]]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_query_guide_nav, text)


def _pick_channel(hits: list[dict]) -> dict | None:
    """Return best channel rec: prefer a content SIG, then any recommended channel."""
    sigs = [h for h in hits
            if h.get("category") == "SPECIAL INTEREST GROUPS"
            and h.get("name") not in _SIG_META_NAMES]
    if sigs:
        return sigs[0]
    # Fall back to first non-SIG hit, or hard-coded fallbacks
    for h in hits:
        if h.get("name") not in _SIG_META_NAMES:
            return h
    return None


def _load_fallback_channel(channel_id: str) -> dict | None:
    """Load a channel entry from the registry by ID."""
    try:
        reg = json.loads(DISCORD_CHANNELS_PATH.read_text())
        return reg["channels"].get(channel_id)
    except Exception:
        return None

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
    slog.append(SESSION_LOG, {**record, "bot_id": BOT_ID})


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
    payload: dict = {"query": query, "max_tokens": max_tokens, "context": "discord"}
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

_TURN_LIMIT_MARKER = "turn limit for this thread"


async def handle_thread_reply(message: discord.Message) -> None:
    thread = message.channel

    # Collect prior thread messages (exclude the just-received message)
    prior: list[discord.Message] = []
    async for msg in thread.history(oldest_first=True, limit=50):
        if msg.id != message.id:
            prior.append(msg)

    # Count completed bot turns (skip sources-only messages and the cap notice itself)
    bot_turns = sum(
        1 for m in prior
        if m.author.id == client.user.id
        and not m.content.startswith("**Sources**")
        and _TURN_LIMIT_MARKER not in m.content
    )

    if bot_turns >= MAX_THREAD_TURNS:
        # Only send the cap notice once — if it's already in history, stay silent
        already_capped = any(
            m.author.id == client.user.id and _TURN_LIMIT_MARKER in m.content
            for m in prior
        )
        if not already_capped:
            await thread.send(
                f"We've reached the {MAX_THREAD_TURNS}-turn limit for this thread. "
                "For a longer conversation, use the web interface: https://c3po.protocolized.io"
            )
        return

    # If the message is a reply to another human (not the bot), it's likely a side
    # conversation — skip unless the bot is explicitly @mentioned.
    is_bot_mentioned = (
        client.user in message.mentions
        or any(r.id == ORACLE_ROLE_ID for r in message.role_mentions)
    )
    if not is_bot_mentioned and message.reference is not None:
        try:
            ref_msg = await thread.fetch_message(message.reference.message_id)
            if ref_msg.author.id != client.user.id:
                return  # reply is to another human — ignore
        except Exception:
            pass  # can't verify reference; proceed

    # Strip bot/role mentions from query text (relevant when explicitly @mentioned in thread)
    query = message.content
    for u in message.mentions:
        query = query.replace(f"<@{u.id}>", "").replace(f"<@!{u.id}>", "")
    for r in message.role_mentions:
        query = query.replace(f"<@&{r.id}>", "")
    query = "".join(c for c in query if c.isprintable()).strip()
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


# ── Nav queries — "where should I post about X?" ─────────────────────────────

# Channels that should never be recommended in nav responses
_NAV_SKIP_NAMES = _SIG_META_NAMES | {"📣-announcements", "accepted-links", "⭐-popular-posts"}


async def handle_nav_query(message: discord.Message, query: str) -> None:
    """Handle navigation queries — reply with channel recommendations from discord_guide."""
    t0 = time.monotonic()
    async with message.channel.typing():
        hits = await query_discord_guide_nav(query)
    latency_ms = int((time.monotonic() - t0) * 1000)

    # Filter archived and meta-noise channels; keep up to 3 ranked by score
    candidates = [
        (score, meta) for score, meta in hits
        if meta.get("status") == "active"
        and meta.get("category") not in ("archived read only", "BACKGROUND")
        and meta.get("name") not in _NAV_SKIP_NAMES
    ][:3]

    if not candidates:
        await message.reply(
            "I couldn't find a specific channel for that — try **#🌞-general** for broad discussions, "
            "or browse the SIG channels to find the closest fit.",
            mention_author=False,
        )
        return

    reply = f"Here are the channels that best fit your question, {message.author.mention}:\n"
    for _score, meta in candidates:
        cid     = meta.get("channel_id", "")
        section = meta.get("category", "")
        blurb   = meta.get("guide_blurb", "")
        sig     = meta.get("sig_display", "")
        cadence = meta.get("cadence", "")
        next_t  = meta.get("next_event_time", "")
        mention = f"<#{cid}>" if cid else meta.get("display", "?")

        line = f"\n**{mention}**"
        if section:
            line += f"  ·  {section}"
        if sig:
            sched = next_t or cadence
            if sched:
                line += f"  ·  {sig} — {'next meeting: ' + sched if next_t else 'meets ' + cadence}"
        if blurb:
            line += f"\n{blurb}"
        reply += line

    try:
        for chunk in split_message(reply.strip()):
            await message.reply(chunk, mention_author=False)
    except discord.HTTPException as exc:
        log.error(f"Failed to send nav reply: {exc}")

    log_session({
        "event":       "nav_query",
        "ts":          _ts(),
        "user_hash":   _hash_user(message.author.id),
        "channel":     getattr(message.channel, "name", str(message.channel.id)),
        "query_len":   len(query),
        "hits":        len(candidates),
        "latency_ms":  latency_ms,
    })


# ── Intro rec helpers ────────────────────────────────────────────────────────

NEW_MEMBER_DAYS = 60       # posts within this many days of joining count as new-member intros
RETURNING_INTRO_MIN_LEN = 80  # min chars for an old-member post in #introductions to get a welcome-back


def _is_new_member(member) -> bool:
    """True if the member joined the server within NEW_MEMBER_DAYS days ago."""
    joined_at = getattr(member, "joined_at", None)
    if joined_at is None:
        return True  # can't verify; fail open
    from datetime import datetime, timezone
    return (datetime.now(timezone.utc) - joined_at).days <= NEW_MEMBER_DAYS

# Fallback resource when corpus returns only VGR-authored results
_INTRO_FALLBACK_SRC = {
    "label":          "READER",
    "title":          "The Summer of Protocols Reader",
    "url":            "https://summerofprotocols.com/research/reader",
    "source":         "pdf",
    "primary_author": "Summer of Protocols",
    "authors":        ["Summer of Protocols"],
    "date":           "2023",
    "is_fallback":    True,
}

_VGR_MARKERS = ("venkatesh rao", "venkatesh", "vgr")

def _is_vgr_authored(src: dict) -> bool:
    pa = (src.get("primary_author") or "").lower()
    if any(m in pa for m in _VGR_MARKERS):
        return True
    for a in (src.get("authors") or []):
        if any(m in (a or "").lower() for m in _VGR_MARKERS):
            return True
    return False


def _is_excluded_from_intro(src: dict) -> bool:
    """True if this source should never appear in intro suggested readings."""
    if _is_vgr_authored(src):
        return True
    if src.get("is_cover_letter"):
        return True
    if src.get("source") == "devlog":
        return True
    if "retrospectus" in (src.get("title") or "").lower():
        return True
    # Lexicon entries without URLs can't be linked
    if src.get("source") == "definition" and not src.get("url"):
        return True
    return False


def _find_mentioned_source(sources: list[dict], answer_lower: str) -> dict | None:
    """Find the source Claude actually named in its answer, using title matching with fuzzy fallback."""
    best_src = None
    best_score = 0.0
    for src in sources:
        if _is_excluded_from_intro(src) or not src.get("url"):
            continue
        title = src.get("title") or ""
        title_core = re.sub(r'\s*\(.*?\)', '', title).strip().lower()
        if not title_core or len(title_core) <= 6:
            continue
        # Exact substring match
        if title_core in answer_lower:
            return src
        # Fuzzy: fraction of significant words (4+ chars) present in answer
        words = re.findall(r'\b\w{4,}\b', title_core)
        if words:
            score = sum(1 for w in words if w in answer_lower) / len(words)
            if score > best_score:
                best_score = score
                best_src = src
    return best_src if best_score >= 0.6 else None


TALLY_PATH = C3PO_DIR / "data" / "intro_recs_tally.json"

def _update_intro_tally(rec_sources: list[dict], channel: dict | None) -> None:
    """Append to the running tally of what the intro handler recommends."""
    try:
        tally = json.loads(TALLY_PATH.read_text()) if TALLY_PATH.exists() else \
                {"resources": {}, "channels": {}}
    except Exception:
        tally = {"resources": {}, "channels": {}}

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for src in rec_sources:
        key = src.get("url") or src.get("title") or "(unknown)"
        if key not in tally["resources"]:
            tally["resources"][key] = {
                "title":   src.get("title") or key,
                "url":     src.get("url"),
                "source":  src.get("source"),
                "author":  src.get("primary_author") or (src.get("authors") or [None])[0],
                "label":   src.get("label"),
                "count":   0,
                "last_seen": today,
                "is_fallback": src.get("is_fallback", False),
            }
        tally["resources"][key]["count"] += 1
        tally["resources"][key]["last_seen"] = today

    if channel:
        cid = channel.get("channel_id", "")
        if cid:
            if cid not in tally["channels"]:
                tally["channels"][cid] = {
                    "name":        channel.get("name"),
                    "display":     channel.get("display"),
                    "sig_display": channel.get("sig_display"),
                    "count":       0,
                    "last_seen":   today,
                }
            tally["channels"][cid]["count"] += 1
            tally["channels"][cid]["last_seen"] = today

    try:
        TALLY_PATH.write_text(json.dumps(tally, indent=2, ensure_ascii=False))
    except Exception as exc:
        log.warning(f"Tally write failed: {exc}")


# ── Introductions monitoring ──────────────────────────────────────────────────

async def handle_introduction(message: discord.Message, returning: bool = False) -> bool:
    """Welcome a new (or returning) member. Returns True if the reply was sent successfully."""
    # Only respond to top-level posts, not replies within #introductions
    if message.reference is not None:
        return False

    intro_text = message.content[:400]
    log.info(f"Introduction from [{message.author}] (returning={returning}): {intro_text[:80]}")

    # Corpus query: frame intro as a resource-recommendation request
    member_type = "Returning" if returning else "New"
    corpus_query = (
        f"{member_type} member introduction: {intro_text}\n\n"
        f"Recommend the single most relevant resource from the corpus for their interests, "
        f"preferring non-VGR-authored resources when equally relevant. "
        f"One sentence on why it fits. Be brief."
    )

    t0 = time.monotonic()
    async with message.channel.typing():
        # Parallel: corpus recs via Worker + channel guide via direct Pinecone
        corpus_task = asyncio.create_task(call_worker(corpus_query, max_tokens=250))
        guide_task  = asyncio.create_task(query_discord_guide(intro_text))
        corpus_data, guide_hits = await asyncio.gather(corpus_task, guide_task,
                                                       return_exceptions=True)
    latency_ms = int((time.monotonic() - t0) * 1000)

    if isinstance(corpus_data, Exception):
        log.error(f"Corpus task failed: {corpus_data}")
        corpus_data = None
    if isinstance(guide_hits, Exception):
        log.error(f"Guide task failed: {guide_hits}")
        guide_hits = []

    answer      = ((corpus_data or {}).get("answer") or "").strip()
    all_sources = [s for s in ((corpus_data or {}).get("sources") or [])
                   if s.get("source") != "transcript"]

    answer_lower = answer.lower()
    # Valid sources: excluded items removed, must have a URL to be linkable
    valid_sources = [s for s in all_sources[:8] if not _is_excluded_from_intro(s) and s.get("url")]

    # Primary: the source Claude actually named (fuzzy title match against answer text)
    primary_match  = _find_mentioned_source(all_sources[:8], answer_lower)
    title_matched  = primary_match is not None
    primary        = primary_match if title_matched else (valid_sources[0] if valid_sources else _INTRO_FALLBACK_SRC)

    # Bonus: up to 2 more linkable sources, different from primary
    primary_key = primary.get("url") or primary.get("title")
    bonus = [s for s in valid_sources if (s.get("url") or s.get("title")) != primary_key][:2]
    rec_sources = [primary] + bonus

    # Quality check — auto-fix simple issues, log anything notable
    rec_sources, q_issues = intro_quality.check_and_fix(
        answer, all_sources, rec_sources, intro_text, title_matched
    )

    # Pick channel recommendation
    channel = _pick_channel(guide_hits or [])
    if channel is None:
        # Hard fallback: load #idle-protocol-musings from registry
        channel = _load_fallback_channel(FALLBACK_CHANNEL_IDS[0])

    # ── Format reply ──────────────────────────────────────────────────────────
    if returning:
        reply = (
            f"Hi {message.author.mention} — looks like you joined a while back and are getting more active. "
            f"Welcome back!\n\n"
        )
    else:
        reply = f"Welcome, {message.author.mention}!\n\n"

    if answer:
        reply += answer + "\n"

    if rec_sources:
        reply += "\n**Suggested reading:**\n"
        for src in rec_sources:
            label    = src.get("label") or src.get("source", "").upper()
            title    = src.get("title") or src.get("url") or "(untitled)"
            url      = src.get("url")
            date     = (src.get("date") or "")[:7]
            date_str = f" ({date})" if date else ""
            link_str = f" — <{url}>" if url else ""
            reply   += f"**[{label}]** {title}{date_str}{link_str}\n"

    # Channel recommendation
    if channel:
        cid        = channel.get("channel_id", "")
        blurb      = channel.get("guide_blurb", "")
        cadence    = channel.get("cadence", "")
        next_time  = channel.get("next_event_time", "")
        sig        = channel.get("sig_display", "")
        mention    = f"<#{cid}>" if cid else channel.get("display", "")

        reply += f"\n**Good first channel:** {mention}"
        if sig:
            sched = next_time or cadence
            if sched:
                reply += f" ({sig} — next meeting: {sched})" if next_time else f" ({sig} — meets {cadence})"
            else:
                reply += f" ({sig})"
        elif cadence:
            reply += f" (meets {cadence})"
        if blurb:
            reply += f"\n{blurb}"

    sent = False
    try:
        for chunk in split_message(reply.strip()):
            await message.reply(chunk, mention_author=False)
        sent = True
    except discord.HTTPException as exc:
        log.error(f"Failed to send intro reply: {exc}")

    if sent:
        _update_intro_tally(rec_sources, channel)
        intro_quality.log_quality(
            user_hash=_hash_user(message.author.id),
            intro_snippet=intro_text[:60],
            primary_title=(rec_sources[0].get("title") or "") if rec_sources else "",
            title_matched=title_matched,
            rec_count=len(rec_sources),
            issues=q_issues,
        )

    log_session({
        "event":        "introduction",
        "ts":           _ts(),
        "type":         "introduction",
        "returning":    returning,
        "user_hash":    _hash_user(message.author.id),
        "intro_len":    len(intro_text),
        "answer_len":   len(answer),
        "sources_seen": len(all_sources),
        "recs_shown":   len(rec_sources),
        "used_fallback": any(s.get("is_fallback") for s in rec_sources),
        "channel_rec":  channel.get("name") if channel else None,
        "latency_ms":   latency_ms,
        "sent":         sent,
    })
    return sent


# ── Welcome queue drain ───────────────────────────────────────────────────────

async def drain_welcome_queue() -> None:
    """Process all queued welcome messages. Called at the start of each bot epoch."""
    items = wq.pop_all()
    if not items:
        return
    # Advance watermark before processing — future seeds only look at newer messages
    wq.advance_watermark(items)
    log.info(f"Welcome queue: draining {len(items)} pending")
    try:
        channel = client.get_channel(INTRODUCTIONS_CHANNEL_ID) \
                  or await client.fetch_channel(INTRODUCTIONS_CHANNEL_ID)
    except Exception as exc:
        log.error(f"Cannot fetch introductions channel for drain: {exc}")
        for item in items:
            wq.push(item)
        return

    for item in items:
        if item.get("attempts", 0) >= wq.MAX_ATTEMPTS:
            log.warning(f"Dropping welcome for {item['user_name']} — exceeded {wq.MAX_ATTEMPTS} attempts")
            continue
        try:
            msg = await channel.fetch_message(int(item["message_id"]))
            success = await handle_introduction(msg)
            if not success:
                wq.push({**item, "attempts": item.get("attempts", 0) + 1})
        except discord.NotFound:
            log.warning(f"Intro message {item['message_id']} ({item['user_name']}) deleted — skipping")
        except Exception as exc:
            log.error(f"Welcome drain failed for {item['user_name']}: {exc}")
            wq.push({**item, "attempts": item.get("attempts", 0) + 1})


# ── Events ────────────────────────────────────────────────────────────────────

GREETINGS = {"", "hey", "hi", "hello", "yo", "sup", "hiya", "howdy", "greetings"}


@client.event
async def on_ready():
    log.info(f"C3PO bot ready — {client.user} (id={client.user.id})")
    log_session({"event": "startup", "ts": _ts(), "user": str(client.user), "user_id": client.user.id})
    if INTRODUCTIONS_CHANNEL_ID:
        try:
            n = await swq.seed_queue(
                token=BOT_TOKEN,
                channel_id=str(INTRODUCTIONS_CHANNEL_ID),
                bot_user_id=str(client.user.id),
                guild_id=DISCORD_GUILD_ID,
                limit=5,
                log=log,
            )
            if n:
                log.info(f"Startup seed: {n} missed intro(s) queued")
        except Exception as exc:
            log.warning(f"Startup seed failed: {exc}")
    asyncio.create_task(drain_welcome_queue())


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
        is_mention = (client.user in message.mentions or
                      any(r.id == ORACLE_ROLE_ID for r in message.role_mentions))
        is_new_intro = message.reference is None and _is_new_member(message.author)
        is_returning_intro = (message.reference is None
                              and not _is_new_member(message.author)
                              and len(message.content) >= RETURNING_INTRO_MIN_LEN)

        if is_new_intro:
            wq.push({
                "message_id": str(message.id),
                "channel_id": str(message.channel.id),
                "user_id":    str(message.author.id),
                "user_name":  message.author.display_name,
                "intro_text": message.content[:400],
                "message_ts": message.created_at.isoformat(),
                "queued_at":  _ts(),
                "attempts":   0,
            })
            success = await handle_introduction(message)
            if success:
                wq.remove(str(message.id))
            return
        elif is_returning_intro:
            await handle_introduction(message, returning=True)
            return
        elif not is_mention:
            return  # existing member posting in #introductions — ignore unless mentioned

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
            "https://c3po.protocolized.io",
            mention_author=False,
        )
        return

    # Nav-intent queries: route to channel guide instead of corpus RAG
    if _NAV_RE.search(query):
        log.info(f"Nav query detected from [{message.author}]: {query[:80]}")
        await handle_nav_query(message, query)
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
