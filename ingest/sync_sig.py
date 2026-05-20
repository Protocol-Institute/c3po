"""
SIG channel harvester for C3PO.

Ingests 4 Protocol Institute Special Interest Group channels with two-level
meeting recognition:

  1. Main channel messages — originals ≥20 chars, replies ≥150 chars
  2. Discussion threads   — bundled as conversation chunk (sig_discussion)
  3. Meeting threads      — transcript chunks (sig_meeting_body) +
                            Claude Haiku enriched summary (sig_meeting_summary)

Meeting detection uses per-channel patterns based on observed thread naming.
Meetings are re-summarized whenever their message_count changes.

Pinecone namespace: `sig`
State file: data/sig_state.json

Usage:
    python3 ingest/sync_sig.py
    python3 ingest/sync_sig.py --dry-run
    python3 ingest/sync_sig.py --backfill     # ignore saved main-channel cursors
    python3 ingest/sync_sig.py --channel 1327337414175490160  # single channel
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
import anthropic

sys.path.insert(0, str(Path(__file__).parent))
from utils import clean_text, chunk_text, embed_chunks, chunk_id, get_voyage_client, get_pinecone_index, PINECONE_BATCH

load_dotenv(Path(__file__).parent.parent / ".env")

NAMESPACE = "sig"
STATE_PATH = Path(__file__).parent.parent / "data" / "sig_state.json"
LINKS_REGISTRY_PATH = Path(__file__).parent.parent / "data" / "discord_links_registry.json"
DISCORD_API = "https://discord.com/api/v10"

ORIG_MIN_CHARS  = 20
REPLY_MIN_CHARS = 150
INITIAL_BACKFILL_DAYS = 1500   # go all the way back on first run
INTER_REQUEST_SLEEP = 1.1

URL_RE = re.compile(r'https?://\S+')

# ── SIG channel configuration ─────────────────────────────────────────────────

SIG_CHANNELS = {
    "1327337414175490160": {
        "name": "formal-protocol-theory",
        "display": "SIGFPT",
        "meeting_patterns": [
            re.compile(r'^SIGFPT[\s:]', re.I),
            re.compile(r'^PFW Session', re.I),
            re.compile(r'^Week \d+ Discussion Thread', re.I),
        ],
    },
    "1379992696114122832": {
        "name": "memory-research-group",
        "display": "MRG",
        "meeting_patterns": [
            re.compile(r'^\d{6,8}\s+Memory Research Group session', re.I),
            re.compile(r'^MEMSIG', re.I),
        ],
    },
    "1333851496416153702": {
        "name": "protocols-for-business",
        "display": "SIGPfB",
        "meeting_patterns": [
            re.compile(r'^(?:=+|\*+)?\s*Study Group:', re.I),
            re.compile(r'^Optional Meeting', re.I),
            re.compile(r'^Optional Discussion', re.I),
            re.compile(r'^SIGPfB', re.I),
            re.compile(r'^SIGP4B', re.I),
            re.compile(r'^SIGSSG', re.I),
            re.compile(r'^PfB SIG', re.I),
            re.compile(r'^PFSIG\s+\w+ \d+', re.I),      # PFSIG + month day
            re.compile(r'SIG Meeting', re.I),
            re.compile(r'Discussion Thread', re.I),
            re.compile(r'^Protocols for Business\s+\d', re.I),
            re.compile(r'^\d{8}\s+Protocols for Business', re.I),
            re.compile(r'^(?:\*+|=+)\s*(?:SIG|Thread for)', re.I),
        ],
    },
    "1106572787042238504": {
        "name": "protocol-fiction",
        "display": "ProtFiSIG",
        "meeting_patterns": [
            re.compile(r'^PFSIG[\s\-]', re.I),
            re.compile(r'^PF SIG[\s,]', re.I),
        ],
    },
}


def is_meeting(thread_name: str, channel_id: str) -> bool:
    config = SIG_CHANNELS.get(channel_id, {})
    return any(p.search(thread_name) for p in config.get("meeting_patterns", []))


# ── Discord REST client ────────────────────────────────────────────────────────

def discord_request(method: str, path: str, payload: dict | None = None):
    token = os.environ["DISCORD_BOT_TOKEN"]
    url = f"{DISCORD_API}{path}"
    data = json.dumps(payload).encode() if payload else None
    headers = {
        "Authorization": f"Bot {token}",
        "User-Agent": "C3PO-SIG/1.0",
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
                time.sleep(retry_after + 0.1)
                continue
            raise


def fetch_all_threads(channel_id: str) -> list[dict]:
    """Fetch all active + archived threads for a channel."""
    threads = []
    try:
        data = discord_request("GET", f"/channels/{channel_id}/threads/active")
        threads.extend(data.get("threads", []))
    except Exception:
        pass

    before = None
    for _ in range(30):
        params = {"limit": 100}
        if before:
            params["before"] = before
        try:
            data = discord_request("GET", f"/channels/{channel_id}/threads/archived/public?{urllib.parse.urlencode(params)}")
            batch = data.get("threads", [])
            threads.extend(batch)
            if not data.get("has_more") or not batch:
                break
            before = batch[-1]["thread_metadata"]["archive_timestamp"]
            time.sleep(0.4)
        except Exception:
            break
    return threads


def fetch_messages_forward(channel_id: str, after_id: str) -> list[dict]:
    """Incremental: fetch messages after `after_id`, oldest-first."""
    messages = []
    last_id = after_id
    while True:
        params = {"limit": 100, "after": last_id}
        batch = discord_request("GET", f"/channels/{channel_id}/messages?{urllib.parse.urlencode(params)}")
        if not batch:
            break
        batch.sort(key=lambda m: int(m["id"]))
        messages.extend(batch)
        if len(batch) < 100:
            break
        last_id = batch[-1]["id"]
        time.sleep(INTER_REQUEST_SLEEP)
    return messages


def fetch_messages_backward(channel_id: str, cutoff_dt: datetime | None = None) -> list[dict]:
    """Backfill: page backward from newest to oldest, return oldest-first."""
    messages = []
    before_id = None
    while True:
        params = {"limit": 100}
        if before_id:
            params["before"] = before_id
        batch = discord_request("GET", f"/channels/{channel_id}/messages?{urllib.parse.urlencode(params)}")
        if not batch:
            break
        oldest = batch[-1]
        before_id = oldest["id"]
        if cutoff_dt:
            batch = [m for m in batch if snowflake_to_dt(m["id"]) >= cutoff_dt]
            messages.extend(batch)
            if snowflake_to_dt(oldest["id"]) < cutoff_dt:
                break
        else:
            messages.extend(batch)
        if len(batch) < 100:
            break
        time.sleep(INTER_REQUEST_SLEEP)
    messages.sort(key=lambda m: int(m["id"]))
    return messages


def fetch_all_thread_messages(thread_id: str) -> list[dict]:
    """Fetch complete thread history, oldest-first."""
    return fetch_messages_backward(thread_id, cutoff_dt=None)


# ── Message helpers ────────────────────────────────────────────────────────────

def snowflake_to_dt(snowflake: str) -> datetime:
    ts_ms = (int(snowflake) >> 22) + 1420070400000
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)


def extract_urls(content: str) -> list[str]:
    return URL_RE.findall(content)


def get_star_count(msg: dict) -> int:
    for r in msg.get("reactions", []):
        if r.get("emoji", {}).get("name") in ("⭐", "🌟"):
            return r.get("count", 0)
    return 0


def is_reply(msg: dict) -> bool:
    return msg.get("type") == 19 or "message_reference" in msg


def qualifies_main(msg: dict, content: str, urls: list[str], stars: int) -> bool:
    if msg.get("author", {}).get("bot"):
        return False
    if is_reply(msg):
        return len(content) >= REPLY_MIN_CHARS or stars > 0 or bool(urls)
    return len(content) >= ORIG_MIN_CHARS or stars > 0 or bool(urls)


# ── Chunk formatters ───────────────────────────────────────────────────────────

def base_meta(channel_id: str, config: dict) -> dict:
    return {
        "source": "sig",
        "namespace": NAMESPACE,
        "guild_id": os.environ.get("DISCORD_GUILD_ID", ""),
        "channel_id": channel_id,
        "channel_name": config["name"],
        "sig_display": config["display"],
    }


def format_main_message(msg: dict, channel_id: str, config: dict) -> tuple[str, dict]:
    content = msg.get("content", "")
    ts = snowflake_to_dt(msg["id"]).strftime("%Y-%m-%dT%H:%M:%SZ")
    author = msg.get("author", {}).get("username", "unknown")
    urls = extract_urls(content)
    stars = get_star_count(msg)
    chunk_type = "sig_reply" if is_reply(msg) else "sig_message"
    text = f"{config['display']} #{config['name']} | @{author} | {ts}\n\n{clean_text(content)}"
    meta = {
        **base_meta(channel_id, config),
        "chunk_type": chunk_type,
        "author": author,
        "timestamp": ts,
        "message_id": msg["id"],
        "star_count": stars,
        "urls": json.dumps(urls[:10]),
        "has_url": bool(urls),
        "text": text[:1000],
    }
    return text, meta


def format_discussion_thread(thread: dict, msgs: list[dict],
                              channel_id: str, config: dict) -> tuple[str, dict]:
    thread_name = thread.get("name", "")
    thread_id = thread["id"]
    ts = snowflake_to_dt(thread_id).strftime("%Y-%m-%dT%H:%M:%SZ")

    lines = [f"{config['display']} #{config['name']} | Thread: {thread_name} | {ts}", ""]
    all_urls, authors = [], []
    for msg in msgs:
        if msg.get("author", {}).get("bot"):
            continue
        author = msg.get("author", {}).get("username", "unknown")
        content = clean_text(msg.get("content", ""))
        if not content:
            continue
        msg_ts = snowflake_to_dt(msg["id"]).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines.append(f"@{author} ({msg_ts}): {content}")
        all_urls.extend(extract_urls(msg.get("content", "")))
        if author not in authors:
            authors.append(author)

    text = "\n".join(lines)
    meta = {
        **base_meta(channel_id, config),
        "chunk_type": "sig_discussion",
        "thread_id": thread_id,
        "thread_name": thread_name,
        "timestamp": ts,
        "reply_count": len([m for m in msgs if not m.get("author", {}).get("bot")]),
        "participants": json.dumps(authors),
        "urls": json.dumps(all_urls[:10]),
        "has_url": bool(all_urls),
        "text": text[:1000],
    }
    return text, meta


def build_transcript(thread: dict, msgs: list[dict], config: dict) -> tuple[str, list[str], list[str]]:
    """Build readable transcript. Returns (transcript, authors, all_urls)."""
    thread_name = thread.get("name", "")
    lines = [f"Meeting: {thread_name}", f"SIG: {config['display']} — {config['name']}", ""]
    authors, all_urls = [], []
    for msg in msgs:
        if msg.get("author", {}).get("bot"):
            continue
        author = msg.get("author", {}).get("username", "unknown")
        content = clean_text(msg.get("content", ""))
        if not content:
            continue
        msg_ts = snowflake_to_dt(msg["id"]).strftime("%Y-%m-%d %H:%M")
        lines.append(f"@{author} ({msg_ts}): {content}")
        if author not in authors:
            authors.append(author)
        all_urls.extend(extract_urls(msg.get("content", "")))
    return "\n".join(lines), authors, all_urls


def generate_meeting_summary(transcript: str, thread_name: str,
                              config: dict, client: anthropic.Anthropic) -> dict:
    """Call Claude Haiku to produce a structured meeting summary."""
    prompt = f"""You are summarizing a meeting transcript from the Protocol Institute's {config['display']} ({config['name']}) group.

Thread title: {thread_name}

Transcript:
{transcript[:12000]}

Return a JSON object with these exact keys:
- "title": clean, readable title for this meeting
- "date": ISO date YYYY-MM-DD if identifiable from the thread title, else "unknown"
- "topics": array of 3-6 main topics discussed
- "key_insights": array of 3-5 significant ideas, arguments, or conclusions
- "summary": 2-3 paragraph narrative summary of what was discussed and concluded
- "links_discussed": array of URLs mentioned as substantive references (not logistics)

Respond with only the JSON object."""

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        return json.loads(m.group()) if m else {}
    except Exception as e:
        print(f"  Haiku summary failed: {e}")
        return {}


def format_meeting_summary_vector(thread: dict, summary: dict, authors: list[str],
                                   all_urls: list[str], msg_count: int,
                                   channel_id: str, config: dict) -> tuple[str, dict]:
    thread_id = thread["id"]
    ts = snowflake_to_dt(thread_id).strftime("%Y-%m-%dT%H:%M:%SZ")
    title = summary.get("title", thread.get("name", ""))
    date  = summary.get("date", "unknown")
    topics = "; ".join(summary.get("topics", []))
    insights = " | ".join(summary.get("key_insights", []))
    narrative = summary.get("summary", "")

    text = (
        f"Meeting Summary — {config['display']}: {title}\n"
        f"Date: {date} | Participants: {', '.join(authors[:10])}\n"
        f"Topics: {topics}\n"
        f"Key insights: {insights}\n\n"
        f"{narrative}"
    )
    meta = {
        **base_meta(channel_id, config),
        "chunk_type": "sig_meeting_summary",
        "thread_id": thread_id,
        "thread_name": thread.get("name", ""),
        "meeting_title": title,
        "meeting_date": date,
        "timestamp": ts,
        "topics": json.dumps(summary.get("topics", [])),
        "participants": json.dumps(authors),
        "message_count": msg_count,
        "urls": json.dumps(all_urls[:10]),
        "has_url": bool(all_urls),
        "text": text[:1000],
    }
    return text, meta


def format_meeting_body_chunks(transcript: str, thread: dict, summary: dict,
                                channel_id: str, config: dict) -> list[tuple[str, dict]]:
    """Chunk the full meeting transcript into body vectors."""
    thread_id = thread["id"]
    ts = snowflake_to_dt(thread_id).strftime("%Y-%m-%dT%H:%M:%SZ")
    title = summary.get("title", thread.get("name", ""))
    prefix = f"Meeting: {config['display']} — {title}\n\n"
    chunks = chunk_text(clean_text(transcript))
    results = []
    for i, chunk in enumerate(chunks):
        text = prefix + chunk
        meta = {
            **base_meta(channel_id, config),
            "chunk_type": "sig_meeting_body",
            "thread_id": thread_id,
            "thread_name": thread.get("name", ""),
            "meeting_title": title,
            "meeting_date": summary.get("date", "unknown"),
            "timestamp": ts,
            "chunk_index": i,
            "chunk_total": len(chunks),
            "text": text[:1000],
        }
        results.append((text, meta))
    return results


# ── Links registry ─────────────────────────────────────────────────────────────

def load_links_registry() -> dict:
    if LINKS_REGISTRY_PATH.exists():
        return json.loads(LINKS_REGISTRY_PATH.read_text())
    return {}


def save_links_registry(registry: dict):
    LINKS_REGISTRY_PATH.write_text(json.dumps(registry, indent=2, ensure_ascii=False))


def register_urls(urls: list[str], channel_name: str, channel_id: str,
                  message_id: str, author: str, registry: dict):
    from urllib.parse import urlparse

    def normalize(url):
        try:
            p = urlparse(url.strip())
            keep = {"youtube.com", "youtu.be", "m.youtube.com", "www.youtube.com"}
            netloc = p.netloc.lower().removeprefix("www.")
            query = p.query if netloc in keep else ""
            return urllib.parse.urlunparse((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), "", query, ""))
        except Exception:
            return url.strip()

    for url in urls:
        key = normalize(url)
        if key not in registry:
            registry[key] = {
                "url": url,
                "domain": urlparse(url).netloc.lower(),
                "first_seen": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "sources": [],
                "fetch_status": "pending",
            }
        src = {"channel_name": channel_name, "channel_id": channel_id,
               "message_id": message_id, "author": author}
        if src not in registry[key]["sources"]:
            registry[key]["sources"].append(src)


# ── State ──────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"channels": {}}


def save_state(state: dict):
    STATE_PATH.write_text(json.dumps(state, indent=2))


# ── Main ───────────────────────────────────────────────────────────────────────

def process_channel(channel_id: str, config: dict, state: dict,
                    vc, index, client: anthropic.Anthropic,
                    backfill: bool, dry_run: bool):

    ch_state = state["channels"].setdefault(channel_id, {
        "last_main_message_id": None, "threads": {}
    })
    links_registry = load_links_registry()

    total_vectors = 0
    meeting_count = 0
    discussion_count = 0
    message_count = 0

    # ── 1. Threads ─────────────────────────────────────────────────────────────
    print(f"  Fetching thread list...")
    threads = fetch_all_threads(channel_id)
    print(f"  {len(threads)} threads found")

    for thread in threads:
        thread_id = thread["id"]
        thread_name = thread.get("name", "")
        reported_count = thread.get("message_count", 0)
        thread_state = ch_state["threads"].get(thread_id, {})
        last_count = thread_state.get("last_message_count", -1)
        meeting = is_meeting(thread_name, channel_id)

        # Skip if unchanged
        if last_count == reported_count and thread_state.get("processed"):
            continue

        print(f"  {'[MEETING]' if meeting else '[thread] ':10} [{reported_count:>3} msgs] {thread_name[:60]}")

        if dry_run:
            ch_state["threads"][thread_id] = {
                "name": thread_name, "is_meeting": meeting,
                "last_message_count": reported_count, "processed": True,
            }
            continue

        # Fetch full thread messages (always backward — for complete transcript)
        msgs = fetch_all_thread_messages(thread_id)
        non_bot_msgs = [m for m in msgs if not m.get("author", {}).get("bot")]

        # Register all URLs from this thread
        for msg in non_bot_msgs:
            urls = extract_urls(msg.get("content", ""))
            if urls:
                register_urls(urls, config["name"], channel_id,
                              msg["id"], msg.get("author", {}).get("username", ""),
                              links_registry)

        records = []

        if meeting:
            transcript, authors, all_urls = build_transcript(thread, msgs, config)
            summary = generate_meeting_summary(transcript, thread_name, config, client)

            # Summary vector
            sum_text, sum_meta = format_meeting_summary_vector(
                thread, summary, authors, all_urls, len(non_bot_msgs), channel_id, config)
            records.append({
                "id": f"sig_meeting_summary__{thread_id}",
                "text": sum_text, "meta": sum_meta,
            })

            # Body chunk vectors
            for i, (body_text, body_meta) in enumerate(
                    format_meeting_body_chunks(transcript, thread, summary, channel_id, config)):
                records.append({
                    "id": f"sig_meeting_body__{thread_id}__{i}",
                    "text": body_text, "meta": body_meta,
                })

            meeting_count += 1
            print(f"    → summary + {len(records)-1} body chunks | {summary.get('date','?')} | {summary.get('title','')[:50]}")

        else:
            disc_text, disc_meta = format_discussion_thread(thread, msgs, channel_id, config)
            records.append({
                "id": f"sig_discussion__{thread_id}",
                "text": disc_text, "meta": disc_meta,
            })
            discussion_count += 1

        # Embed and upsert
        texts = [r["text"] for r in records]
        vectors = embed_chunks(texts, vc)
        pinecone_records = [
            {"id": r["id"], "values": v, "metadata": r["meta"]}
            for r, v in zip(records, vectors)
        ]
        for i in range(0, len(pinecone_records), PINECONE_BATCH):
            index.upsert(vectors=pinecone_records[i:i + PINECONE_BATCH], namespace=NAMESPACE)
        total_vectors += len(records)

        ch_state["threads"][thread_id] = {
            "name": thread_name, "is_meeting": meeting,
            "last_message_count": reported_count, "processed": True,
        }
        save_state(state)
        save_links_registry(links_registry)
        time.sleep(0.3)

    # ── 2. Main channel messages ───────────────────────────────────────────────
    after_id = None if backfill else ch_state.get("last_main_message_id")
    cutoff_dt = None
    if after_id is None:
        cutoff_dt = datetime.now(timezone.utc) - timedelta(days=INITIAL_BACKFILL_DAYS)

    print(f"\n  Fetching main channel messages...")
    if after_id:
        msgs = fetch_messages_forward(channel_id, after_id)
    else:
        msgs = fetch_messages_backward(channel_id, cutoff_dt)

    print(f"  {len(msgs)} main channel messages")
    new_last_id = after_id
    records = []

    for msg in msgs:
        msg_id = msg["id"]
        if new_last_id is None or int(msg_id) > int(new_last_id):
            new_last_id = msg_id

        content = msg.get("content", "")
        urls = extract_urls(content)
        stars = get_star_count(msg)

        if not qualifies_main(msg, content, urls, stars):
            continue

        if urls:
            register_urls(urls, config["name"], channel_id,
                          msg_id, msg.get("author", {}).get("username", ""), links_registry)

        text, meta = format_main_message(msg, channel_id, config)
        records.append({"id": f"sig_msg__{msg_id}", "text": text, "meta": meta})
        message_count += 1

    if not dry_run and records:
        texts = [r["text"] for r in records]
        vectors = embed_chunks(texts, vc)
        pinecone_records = [
            {"id": r["id"], "values": v, "metadata": r["meta"]}
            for r, v in zip(records, vectors)
        ]
        for i in range(0, len(pinecone_records), PINECONE_BATCH):
            index.upsert(vectors=pinecone_records[i:i + PINECONE_BATCH], namespace=NAMESPACE)
        total_vectors += len(records)

    if not dry_run:
        ch_state["last_main_message_id"] = new_last_id
        save_state(state)
        save_links_registry(links_registry)

    print(f"\n  ── {config['display']} summary ──")
    print(f"  Meetings processed  : {meeting_count}")
    print(f"  Discussion threads  : {discussion_count}")
    print(f"  Main channel msgs   : {message_count}")
    print(f"  Vectors upserted    : {total_vectors}")

    return total_vectors


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--backfill", action="store_true",
                        help="Ignore saved main-channel cursors (re-process from beginning)")
    parser.add_argument("--channel", help="Process only this channel ID")
    args = parser.parse_args()

    target_channels = SIG_CHANNELS
    if args.channel:
        if args.channel not in SIG_CHANNELS:
            print(f"Unknown channel: {args.channel}")
            sys.exit(1)
        target_channels = {args.channel: SIG_CHANNELS[args.channel]}

    state = load_state()
    vc = None if args.dry_run else get_voyage_client()
    index = None if args.dry_run else get_pinecone_index()
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    grand_total = 0
    for channel_id, config in target_channels.items():
        print(f"\n{'═'*60}")
        print(f"#{config['name']} ({config['display']})")
        print("═" * 60)
        n = process_channel(channel_id, config, state, vc, index, client,
                            backfill=args.backfill, dry_run=args.dry_run)
        grand_total += n

    print(f"\n{'═'*60}")
    print(f"Total vectors upserted: {grand_total}")
    print(f"Namespace: {NAMESPACE}")


if __name__ == "__main__":
    main()
