"""
Ingest audio meeting summaries from #meeting-notes into Pinecone and website.

OpenRecapper-PI posts structured output to #meeting-notes after each voice
recording. Each meeting produces:
  - A header message with permanent R2 URLs (summary.md, transcript.txt, etc.)
  - Several AI-written summary section messages

This script:
  1. Scans #meeting-notes for header messages (identified by 📝 prefix + attachment)
  2. Fetches summary.md from the permanent R2 URL in the message body
  3. Parses participants, overview, key points, and Q&D sections
  4. For SIG meetings and ad hoc: embeds sections → Pinecone sig namespace
  5. For SIG meetings: enriches data/sigs/meetings/*.json with audio_* fields
     (new JSON created if no Discord thread match exists)

Uses R2 permanent URLs — NOT Discord CDN attachment URLs which expire in 24h.

State: data/meeting_notes_state.json keyed by header message ID.
       {message_id: {processed_at, title, date, sig, r2_summary_url, pinecone_ids}}

Usage:
    python3 ingest/sync_meeting_notes.py
    python3 ingest/sync_meeting_notes.py --dry-run
    python3 ingest/sync_meeting_notes.py --force      # re-process already-done
"""

import argparse
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
from utils import (clean_text, embed_chunks, get_voyage_client, get_pinecone_index,
                   meeting_ready, MEETING_GRACE_DAYS)

load_dotenv(Path(__file__).parent.parent / ".env")

import os
import requests

CHANNEL_ID    = "1519549380791631903"   # #meeting-notes
GUILD_ID      = "1082444651946049567"
NAMESPACE     = "sig"
STATE_PATH    = Path(__file__).parent.parent / "data" / "meeting_notes_state.json"
MEETINGS_DIR  = Path(__file__).parent.parent / "data" / "sigs" / "meetings"

# Map normalized title prefix → sig_display. Keys are matched against the title
# with every non-alphanumeric character removed, and again with a leading "sig"
# stripped — so a key may be written bare ("drg") and will still match "SIG-DRG",
# "sigdrg" and "DRG". Do not add both bare and sig-prefixed spellings of the same
# group; one bare key covers every form.
SIG_TITLE_MAP = {
    "psy":       "SIGPSY",
    "fpt":       "SIGFPT",
    "drg":       "DRG",
    "mrg":       "MRG",
    "prg":       "PRG",
    "pfb":       "SIGPfB",
    "p4b":       "SIGPfB",
    "ssg":       "SIGPfB",
    "protfi":    "ProtFiSIG",
    "pfsig":     "ProtFiSIG",
    "pf":        "ProtFiSIG",
}

SIG_NAMES = {
    "SIGPSY":    "Special Interest Group in Psychohistory",
    "SIGFPT":    "Formal Protocol Theory",
    "DRG":       "Distributed Robotics Group",
    "MRG":       "Memory Research Group",
    "PRG":       "Personhood Research Group",
    "SIGPfB":    "Protocols for Business",
    "ProtFiSIG": "Protocol Fiction",
}


def match_sig(title: str) -> str | None:
    """Resolve a recording title to a sig_display value, or None if unrecognised.

    The recorder has posted the same group under several spellings over time
    ("sigfpt 2026-07-10", "SIG-FPT 2026-07-24", "SIG-DRG 2026-08-20"), so the
    title is reduced to bare alphanumerics before matching and then tried a
    second time with a leading "sig" removed. Longest key first, so a short key
    ("pf") can never shadow a longer one that also matches ("pfb").
    """
    key = re.sub(r"[^a-z0-9]", "", title.lower())
    if key.startswith("adhoc"):
        return "AdHoc"
    candidates = [key]
    if key.startswith("sig"):
        candidates.append(key[3:])
    for cand in candidates:
        for prefix in sorted(SIG_TITLE_MAP, key=len, reverse=True):
            if cand.startswith(prefix):
                return SIG_TITLE_MAP[prefix]
    return None


def discord_get(path: str, token: str) -> dict | list:
    r = requests.get(
        f"https://discord.com/api/v10{path}",
        headers={"Authorization": f"Bot {token}"},
    )
    r.raise_for_status()
    return r.json()


def fetch_all_messages(token: str) -> list[dict]:
    """Fetch all messages in #meeting-notes, oldest first, paginated."""
    msgs = []
    before = None
    while True:
        params = "?limit=100"
        if before:
            params += f"&before={before}"
        batch = discord_get(f"/channels/{CHANNEL_ID}/messages{params}", token)
        if not batch:
            break
        msgs.extend(batch)
        before = batch[-1]["id"]
        if len(batch) < 100:
            break
        time.sleep(0.5)
    msgs.reverse()   # oldest first
    return msgs


def is_header(msg: dict) -> bool:
    """A header message: OpenRecapper-PI, starts with 📝 **, has summary.md attachment."""
    if not msg["content"].startswith("📝 **"):
        return False
    if not any(a["filename"] == "summary.md" for a in msg.get("attachments", [])):
        return False
    return True


def parse_header(msg: dict) -> dict | None:
    """Extract structured info from a header message."""
    content = msg["content"]

    # Title: **sigpsy 02July26 2026-07-02** — transcription complete for <#...>
    title_m = re.search(r"📝 \*\*(.+?)\*\*", content)
    if not title_m:
        return None
    title = title_m.group(1).strip()

    # Date at end of title (YYYY-MM-DD)
    date_m = re.search(r"(\d{4}-\d{2}-\d{2})$", title)
    date = date_m.group(1) if date_m else msg["timestamp"][:10]

    # Duration
    dur_m = re.search(r"\*\*Duration:\*\*\s+(\d+m\s+\d+s)", content)
    duration = dur_m.group(1) if dur_m else None

    # Speaker count
    spk_m = re.search(r"\*\*Speakers:\*\*\s+(\d+)", content)
    speakers = int(spk_m.group(1)) if spk_m else None

    # R2 summary URL (permanent)
    r2_m = re.search(r"📝 Summary:\s+(https://[^\s]+/summary\.md)", content)
    r2_summary_url = r2_m.group(1) if r2_m else None

    r2_transcript_m = re.search(r"📄 Transcript:\s+(https://[^\s]+/transcript\.txt)", content)
    r2_transcript_url = r2_transcript_m.group(1) if r2_transcript_m else None

    sig_display = match_sig(title)

    return {
        "message_id": msg["id"],
        "title": title,
        "date": date,
        "sig_display": sig_display,
        "duration": duration,
        "speakers": speakers,
        "r2_summary_url": r2_summary_url,
        "r2_transcript_url": r2_transcript_url,
        "ts": msg["timestamp"],
    }


def fetch_r2_summary(url: str) -> str | None:
    """Fetch summary.md content from R2 permanent URL."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "c3po-ingest/1.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  ⚠ R2 fetch failed: {e}")
        return None


SECTION_HEADERS = {
    "reading":    re.compile(r"^##\s+📖\s+The Reading", re.MULTILINE),
    "overview":   re.compile(r"^##\s+🧭\s+Overview", re.MULTILINE),
    "key_points": re.compile(r"^##\s+💡\s+Key Points", re.MULTILINE),
    "questions":  re.compile(r"^##\s+🔀\s+Questions", re.MULTILINE),
    "action":     re.compile(r"^##\s+✅\s+Action", re.MULTILINE),
    "context":    re.compile(r"^##\s+🌐\s+Context", re.MULTILINE),
    "tools":      re.compile(r"^##\s+🛠", re.MULTILINE),
}


def parse_summary_md(text: str) -> dict:
    """Parse summary.md into structured fields."""
    # Participants: lines between title and first ---
    parts_m = re.search(r"^Participants:\n([\s\S]+?)^---", text, re.MULTILINE)
    participants = []
    if parts_m:
        for line in parts_m.group(1).splitlines():
            name = line.strip().lstrip("-").strip()
            # Strip timezone suffix (e.g. "| UTC-8")
            name = re.sub(r"\s*\|\s*UTC.*$", "", name).strip()
            if name:
                participants.append(name)

    # Locate each section by finding ## headers
    sections: dict[str, str] = {}
    all_headers = list(re.finditer(r"^(##\s+.+)$", text, re.MULTILINE))

    def extract_between(start_pos: int, end_pos: int) -> str:
        chunk = text[start_pos:end_pos].strip()
        # Remove the header line itself
        return re.sub(r"^##\s+.+\n", "", chunk, count=1).strip()

    for i, h in enumerate(all_headers):
        htext = h.group(1)
        start = h.end()
        end = all_headers[i + 1].start() if i + 1 < len(all_headers) else len(text)
        body = extract_between(h.start(), end)
        for key, pat in SECTION_HEADERS.items():
            if pat.search(htext):
                sections[key] = body
                break

    return {"participants": participants, "sections": sections}


def find_meeting_json(sig_display: str, date: str) -> Path | None:
    """Find existing meeting JSON by SIG + date."""
    for p in MEETINGS_DIR.glob("*.json"):
        try:
            d = json.loads(p.read_text())
        except Exception:
            continue
        if d.get("sig") == sig_display and d.get("date") == date:
            return p
    return None


def create_meeting_json(recording: dict, parsed: dict) -> Path:
    """Create a new meeting JSON from audio recording data."""
    thread_id = f"audio_{recording['message_id']}"
    sig = recording["sig_display"]
    overview = parsed["sections"].get("overview", "")
    key_pts = [
        line.lstrip("- ").strip()
        for line in parsed["sections"].get("key_points", "").splitlines()
        if line.startswith("- **")
    ][:6]

    entry = {
        "thread_id": thread_id,
        "sig": sig,
        "sig_name": SIG_NAMES.get(sig, sig),
        "channel_id": None,
        "title": recording["title"],
        "date": recording["date"],
        "topics": [],
        "key_insights": key_pts,
        "summary": overview[:1000],
        "links": [],
        "participants": parsed["participants"],
        "all_urls": [recording["r2_summary_url"]],
        "discord_url": None,
        # Audio-specific fields
        "audio_summary": overview,
        "audio_key_points": key_pts,
        "audio_participants": parsed["participants"],
        "audio_reading": parsed["sections"].get("reading", ""),
        "audio_questions": parsed["sections"].get("questions", ""),
        "audio_r2_summary_url": recording["r2_summary_url"],
        "audio_duration": recording["duration"],
        "audio_speakers": recording["speakers"],
        "audio_source": "openrecapper-pi",
    }
    path = MEETINGS_DIR / f"{thread_id}.json"
    path.write_text(json.dumps(entry, indent=2, ensure_ascii=False))
    return path


def enrich_meeting_json(path: Path, recording: dict, parsed: dict):
    """Add audio_* fields to an existing meeting JSON."""
    d = json.loads(path.read_text())
    overview = parsed["sections"].get("overview", "")
    key_pts = [
        line.lstrip("- ").strip()
        for line in parsed["sections"].get("key_points", "").splitlines()
        if line.startswith("- **")
    ][:6]

    d["audio_summary"] = overview
    d["audio_key_points"] = key_pts
    d["audio_participants"] = parsed["participants"]
    d["audio_reading"] = parsed["sections"].get("reading", "")
    d["audio_questions"] = parsed["sections"].get("questions", "")
    d["audio_r2_summary_url"] = recording["r2_summary_url"]
    d["audio_duration"] = recording["duration"]
    d["audio_speakers"] = recording["speakers"]
    d["audio_source"] = "openrecapper-pi"
    path.write_text(json.dumps(d, indent=2, ensure_ascii=False))


def build_vectors(recording: dict, parsed: dict) -> list[dict]:
    """Build Pinecone vector records for a recording."""
    sig = recording["sig_display"]
    date = recording["date"]
    title = recording["title"]
    mid = recording["message_id"]
    if sig is None:
        print(f"  ⚠ No SIG matched for title '{title}' — tagging sig_display empty")
    sig_name = "Ad hoc" if sig == "AdHoc" else SIG_NAMES.get(sig, sig or "")

    base_meta = {
        "sig_display":  "" if sig in (None, "AdHoc") else sig,
        "sig_name":     sig_name,
        "meeting_title": title,
        "date":         date,
        "participants": json.dumps(parsed["participants"]),
        "duration":     recording["duration"] or "",
        "speakers":     recording["speakers"] or 0,
        "url":          recording["r2_summary_url"] or "",
        "guild_id":     GUILD_ID,
        "source":       "audio_meeting",
    }

    vectors = []

    # Combined summary vector (overview + key points)
    overview = parsed["sections"].get("overview", "")
    key_pts_text = parsed["sections"].get("key_points", "")
    summary_text = f"{title}\n\n{overview}\n\n{key_pts_text}".strip()
    if summary_text:
        vectors.append({
            "id": f"audio_meeting_summary__{mid}",
            "text": clean_text(summary_text),
            "meta": {**base_meta, "chunk_type": "audio_meeting_summary", "section": "summary"},
        })

    # Individual sections
    for section_key, section_text in parsed["sections"].items():
        if not section_text.strip():
            continue
        section_label = section_key.replace("_", " ").title()
        text = f"{title} — {section_label}\n\n{section_text}".strip()
        vectors.append({
            "id": f"audio_meeting_section__{mid}__{section_key}",
            "text": clean_text(text),
            "meta": {**base_meta, "chunk_type": "audio_meeting_section", "section": section_label},
        })

    return vectors


def attach_meeting_json(recording: dict, parsed: dict, dry_run: bool) -> bool:
    """Enrich or create the data/sigs/meetings/ record for this recording.

    Returns False when the step was deferred and should be retried on a later
    run. A session whose Discord thread has not yet been summarised has no
    meeting JSON to enrich, but rebuild_sig_summaries.py will create one once
    MEETING_GRACE_DAYS have passed; creating an audio-only record before then
    leaves two records for the same sig+date, which renders as a duplicate
    meeting card. So within the grace window we wait rather than create.
    """
    sig = recording["sig_display"]
    if not sig or sig == "AdHoc":
        return True

    existing = find_meeting_json(sig, recording["date"])
    if existing:
        if dry_run:
            print(f"  [dry-run] Would enrich {existing.name}")
        else:
            enrich_meeting_json(existing, recording, parsed)
            print(f"  ✓ Enriched {existing.name} with audio fields")
        return True

    if not meeting_ready(recording["date"]):
        print(f"  ⏸ no meeting record yet and {recording['date']} is under "
              f"{MEETING_GRACE_DAYS}d old — deferring JSON to a later run")
        return False

    if dry_run:
        print(f"  [dry-run] Would create new meeting JSON for {sig} {recording['date']}")
    else:
        new_path = create_meeting_json(recording, parsed)
        print(f"  ✓ Created {new_path.name}")
    return True


def process_recording(recording: dict, dry_run: bool, vc, idx) -> tuple[list[str], bool]:
    """Fetch summary.md, parse, embed, upsert.

    Returns (Pinecone IDs, json_pending) — see attach_meeting_json() for when
    the meeting-JSON step is deferred.
    """
    url = recording["r2_summary_url"]
    if not url:
        print(f"  ⚠ No R2 summary URL — skipping")
        return [], False

    print(f"  Fetching summary.md …")
    md_text = fetch_r2_summary(url)
    if not md_text:
        return [], False

    parsed = parse_summary_md(md_text)
    print(f"  Participants: {len(parsed['participants'])} | Sections: {list(parsed['sections'].keys())}")

    vectors = build_vectors(recording, parsed)
    if not vectors:
        print(f"  ⚠ No vectors built")
        return [], False

    # Embed
    texts = [v["text"] for v in vectors]
    if not dry_run:
        embeddings = embed_chunks(texts, vc)
        pc_vectors = [
            {"id": v["id"], "values": emb, "metadata": v["meta"]}
            for v, emb in zip(vectors, embeddings)
        ]
        idx.upsert(vectors=pc_vectors, namespace=NAMESPACE)
        print(f"  ✓ Upserted {len(pc_vectors)} vectors → Pinecone {NAMESPACE}")
    else:
        print(f"  [dry-run] Would upsert {len(vectors)} vectors")

    json_pending = not attach_meeting_json(recording, parsed, dry_run)

    return [v["id"] for v in vectors], json_pending


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def save_state(state: dict):
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description="Sync #meeting-notes audio summaries")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force",   action="store_true", help="re-process already-done recordings")
    args = parser.parse_args()

    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        print("ERROR: DISCORD_BOT_TOKEN not set"); sys.exit(1)

    state = load_state()

    print("Fetching #meeting-notes messages …")
    msgs = fetch_all_messages(token)
    print(f"  {len(msgs)} messages total")

    # Find header messages
    headers = [m for m in msgs if is_header(m)]
    print(f"  {len(headers)} recording headers found")

    if not headers:
        print("Nothing to do.")
        return

    vc = get_voyage_client()
    idx = get_pinecone_index()

    # Recordings whose meeting-JSON step was deferred inside the grace window
    # (see attach_meeting_json) are retried here — no re-embedding, just the
    # JSON attach, since their vectors are already correct in Pinecone.
    headers_by_id = {m["id"]: m for m in headers}
    attached = 0
    for mid, entry in state.items():
        if not entry.get("json_pending") or mid not in headers_by_id:
            continue
        rec = parse_header(headers_by_id[mid])
        if not rec:
            continue
        md_text = fetch_r2_summary(rec["r2_summary_url"]) if rec["r2_summary_url"] else None
        if not md_text:
            continue
        print(f"\n[{rec['date']}] {rec['title']} — retrying deferred meeting JSON")
        if attach_meeting_json(rec, parse_summary_md(md_text), args.dry_run):
            attached += 1
            if not args.dry_run:
                entry["json_pending"] = False
    if attached and not args.dry_run:
        save_state(state)

    processed = 0
    skipped = 0
    for msg in headers:
        rec = parse_header(msg)
        if not rec:
            continue

        mid = rec["message_id"]
        if mid in state and not args.force:
            skipped += 1
            continue

        print(f"\n[{rec['date']}] {rec['title']} (sig={rec['sig_display']})")

        ids, json_pending = process_recording(rec, dry_run=args.dry_run, vc=vc, idx=idx)

        if not args.dry_run:
            state[mid] = {
                "processed_at":    datetime.now(timezone.utc).isoformat(),
                "title":           rec["title"],
                "date":            rec["date"],
                "sig_display":     rec["sig_display"],
                "r2_summary_url":  rec["r2_summary_url"],
                "pinecone_ids":    ids,
                "json_pending":    json_pending,
            }
            save_state(state)
        processed += 1

    print(f"\n── Done ──────────────────────────────────────")
    print(f"  Processed : {processed}")
    print(f"  Deferred JSONs attached : {attached}")
    print(f"  Skipped   : {skipped} (already done; use --force to reprocess)")


if __name__ == "__main__":
    main()
