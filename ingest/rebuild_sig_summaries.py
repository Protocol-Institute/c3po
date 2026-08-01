"""
Rebuild full narrative summaries for all SIG meeting vectors.

Existing sig_meeting_summary vectors have structured metadata (title, date,
topics, participants, urls) but the text field is truncated at 1000 chars and
typically fills up with header content before reaching the narrative paragraph.

This script:
  1. Fetches all sig_meeting_summary__* vectors from Pinecone
  2. For each, reconstructs the transcript from sig_meeting_body__* chunks
  3. Calls Claude Haiku to generate a full structured summary
  4. Writes JSON to data/sigs/meetings/{thread_id}.json
  5. Updates the Pinecone vector metadata with summary_text field

Usage:
    python3 ingest/rebuild_sig_summaries.py
    python3 ingest/rebuild_sig_summaries.py --limit 5      # test run
    python3 ingest/rebuild_sig_summaries.py --sig SIGFPT   # one SIG only
    python3 ingest/rebuild_sig_summaries.py --force        # re-run already built
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

sys.path.insert(0, str(Path(__file__).parent))
from utils import get_pinecone_index, meeting_ready, MEETING_GRACE_DAYS
import cost_logger

NAMESPACE = "sig"
MEETINGS_DIR = Path(__file__).parent.parent / "data" / "sigs" / "meetings"

SIG_DISPLAY_NAMES = {
    "SIGFPT":    "Formal Protocol Theory",
    "MRG":       "Memory Research Group",
    "SIGPfB":    "Protocols for Business",
    "ProtFiSIG": "Protocol Fiction SIG",
    "SIGPSY":    "Special Interest Group in Psychohistory",
    "DRG":       "Distributed Robotics Group",
}


def get_all_summary_ids(idx) -> list[str]:
    ids = []
    for page in idx.list(prefix="sig_meeting_summary__", namespace=NAMESPACE):
        ids.extend(item.id for item in page.vectors)
    return ids


def get_body_chunk_ids(idx, thread_id: str) -> list[str]:
    ids = []
    for page in idx.list(prefix=f"sig_meeting_body__{thread_id}__", namespace=NAMESPACE):
        ids.extend(item.id for item in page.vectors)
    return sorted(ids, key=lambda x: int(x.split("__")[-1]) if x.split("__")[-1].isdigit() else 0)


def reconstruct_transcript(idx, thread_id: str) -> str:
    """Fetch body chunks and reassemble meeting transcript."""
    chunk_ids = get_body_chunk_ids(idx, thread_id)
    if not chunk_ids:
        return ""
    batch = idx.fetch(ids=chunk_ids, namespace=NAMESPACE)
    parts = []
    for cid in chunk_ids:
        vec = batch.vectors.get(cid)
        if not vec:
            continue
        text = vec.metadata.get("text", "")
        # Strip "Meeting: {display} — {title}\n\n" prefix
        body = text.split("\n\n", 1)
        parts.append(body[1] if len(body) > 1 else text)
    return "\n\n".join(parts)


def generate_summary(transcript: str, meta: dict, client: anthropic.Anthropic) -> dict:
    sig_display = meta.get("sig_display", "SIG")
    sig_name = SIG_DISPLAY_NAMES.get(sig_display, sig_display)
    title = meta.get("thread_name", meta.get("meeting_title", ""))
    date = meta.get("meeting_date", "unknown")
    participants = json.loads(meta.get("participants", "[]"))

    prompt = f"""You are summarizing a meeting transcript from the Protocol Institute's {sig_display} ({sig_name}) group.

Thread title: {title}
Date: {date}
Participants: {', '.join(participants[:15])}

Transcript:
{transcript[:12000]}

Return a JSON object with these exact keys:
- "title": clean, readable title for this meeting
- "date": ISO date YYYY-MM-DD if identifiable, else "unknown"
- "topics": array of 3-6 main topics discussed (specific, not generic)
- "key_insights": array of 3-5 significant ideas, arguments, or conclusions (each 1-2 sentences)
- "summary": 2-3 paragraph narrative of what was discussed and concluded
- "links_discussed": array of URLs mentioned as substantive references (not logistics/calendar/gif links)

Respond with only the JSON object."""

    try:
        cost_logger.check_budget()
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        cost_logger.log_api_call("rebuild_sig_summaries", "claude-haiku-4-5-20251001", resp.usage)
        raw = resp.content[0].text.strip()
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        return json.loads(m.group()) if m else {}
    except Exception as e:
        print(f"    Haiku failed: {e}")
        return {}


def build_meeting_record(summary: dict, meta: dict) -> dict:
    """Merge Pinecone metadata with Haiku summary into a clean record."""
    guild_id = meta.get("guild_id", "1082444651946049567")
    thread_id = meta.get("thread_id", "")
    discord_url = f"https://discord.com/channels/{guild_id}/{thread_id}" if thread_id else ""

    return {
        "thread_id": thread_id,
        "sig": meta.get("sig_display", ""),
        "sig_name": SIG_DISPLAY_NAMES.get(meta.get("sig_display", ""), ""),
        "channel_id": meta.get("channel_id", ""),
        "title": summary.get("title") or meta.get("meeting_title", ""),
        "date": summary.get("date") or meta.get("meeting_date", "unknown"),
        "topics": summary.get("topics") or json.loads(meta.get("topics", "[]")),
        "key_insights": summary.get("key_insights", []),
        "summary": summary.get("summary", ""),
        "links": summary.get("links_discussed") or [],
        "participants": json.loads(meta.get("participants", "[]")),
        "all_urls": json.loads(meta.get("urls", "[]")),
        "discord_url": discord_url,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, help="Max meetings to process")
    parser.add_argument("--sig", help="Process only this SIG (e.g. SIGFPT)")
    parser.add_argument("--force", action="store_true", help="Re-process already-built meetings")
    args = parser.parse_args()

    MEETINGS_DIR.mkdir(parents=True, exist_ok=True)
    idx = get_pinecone_index()
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    # Fetch all meeting summary vectors
    print("Fetching meeting summary IDs...")
    all_ids = get_all_summary_ids(idx)
    print(f"  {len(all_ids)} meeting summaries found")

    # Fetch all metadata at once
    all_meta = {}
    for i in range(0, len(all_ids), 100):
        batch = idx.fetch(ids=all_ids[i:i+100], namespace=NAMESPACE)
        for vid, vec in batch.vectors.items():
            all_meta[vid] = vec.metadata

    # Filter by SIG if requested
    if args.sig:
        all_meta = {k: v for k, v in all_meta.items()
                    if v.get("sig_display", "").upper() == args.sig.upper()}
        print(f"  Filtered to {len(all_meta)} {args.sig} meetings")

    # Sort by date for readable output
    sorted_metas = sorted(all_meta.items(),
                          key=lambda x: (x[1].get("sig_display", ""), x[1].get("meeting_date", "")),
                          reverse=True)

    # Skip already-built unless --force
    if not args.force:
        to_process = [(vid, m) for vid, m in sorted_metas
                      if not (MEETINGS_DIR / f"{m.get('thread_id','')}.json").exists()]
        if len(to_process) < len(sorted_metas):
            print(f"  {len(sorted_metas)-len(to_process)} already built (--force to redo)")
    else:
        to_process = sorted_metas

    if args.limit:
        to_process = to_process[:args.limit]

    print(f"  Processing {len(to_process)} meetings\n")

    ok = 0
    fail = 0

    for i, (vid, meta) in enumerate(to_process):
        thread_id = meta.get("thread_id", "")
        sig = meta.get("sig_display", "?")
        title = meta.get("meeting_title", "?")
        date = meta.get("meeting_date", "?")
        out_path = MEETINGS_DIR / f"{thread_id}.json"

        print(f"[{i+1}/{len(to_process)}] {sig} | {date} | {title[:55]}")

        if not meeting_ready(date):
            print(f"  ⏸ not yet {MEETING_GRACE_DAYS}d past meeting date — skipping (will retry next run)")
            continue

        transcript = reconstruct_transcript(idx, thread_id)
        if not transcript:
            print(f"  ⚠ No transcript — skipping")
            fail += 1
            continue

        summary = generate_summary(transcript, meta, client)
        if not summary:
            fail += 1
            continue

        record = build_meeting_record(summary, meta)
        out_path.write_text(json.dumps(record, indent=2, ensure_ascii=False))
        print(f"  ✓ {len(record['topics'])} topics | {len(record['key_insights'])} insights | {len(transcript)} chars")
        ok += 1
        time.sleep(0.2)

    print(f"\n── Done ─────────────────────────────────")
    print(f"  Built  : {ok}")
    print(f"  Failed : {fail}")
    print(f"  Output : {MEETINGS_DIR}")


if __name__ == "__main__":
    main()
