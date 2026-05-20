"""
Channel onboarding tool for C3PO.

Given a Discord channel ID, this script:
  1. Fetches channel metadata and a sample of recent messages + threads
  2. Calls Claude to classify the channel and propose ingestion config
  3. Presents the proposed config for human approval
  4. Adds approved channels to data/channel_manifest.json
  5. Optionally runs initial backfill

Usage:
    # Active channel — added to daily monitoring rotation:
    python3 ingest/onboard_channel.py --channel <id>
    python3 ingest/onboard_channel.py --channel <id> --backfill
    python3 ingest/onboard_channel.py --channel <id> --yes  # skip confirmation

    # Archived channel — one-time full-history sweep, NOT added to daily rotation:
    python3 ingest/onboard_channel.py --channel <id> --archive
    python3 ingest/onboard_channel.py --channel <id> --archive --yes

--archive implies --backfill. It sets status="archived" in the manifest so the
daily sync skips the channel, but the full message history is embedded once.

The channel must be accessible by the bot token in .env.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

MANIFEST_PATH = Path(__file__).parent.parent / "data" / "channel_manifest.json"
DISCORD_API = "https://discord.com/api/v10"
GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "")
SAMPLE_MESSAGES = 60
SAMPLE_THREADS = 20


# ── Discord REST ───────────────────────────────────────────────────────────────

def discord_request(path: str):
    token = os.environ["DISCORD_BOT_TOKEN"]
    url = f"{DISCORD_API}{path}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bot {token}", "User-Agent": "C3PO-Onboard/1.0"
    })
    while True:
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(float(json.loads(e.read()).get("retry_after", 1)) + 0.1)
            else:
                raise


def fetch_channel_info(channel_id: str) -> dict:
    return discord_request(f"/channels/{channel_id}")


def fetch_recent_messages(channel_id: str, limit: int = SAMPLE_MESSAGES) -> list[dict]:
    data = discord_request(f"/channels/{channel_id}/messages?limit={min(limit, 100)}")
    data.sort(key=lambda m: int(m["id"]))
    return data


def fetch_recent_threads(channel_id: str, limit: int = SAMPLE_THREADS) -> list[dict]:
    threads = []
    # Active threads (guild-level)
    if GUILD_ID:
        try:
            data = discord_request(f"/guilds/{GUILD_ID}/threads/active")
            active = [t for t in data.get("threads", []) if t.get("parent_id") == channel_id]
            threads.extend(active)
        except Exception:
            pass
    # Archived threads (first page)
    try:
        data = discord_request(f"/channels/{channel_id}/threads/archived/public?limit=100")
        threads.extend(data.get("threads", []))
    except Exception:
        pass
    threads.sort(key=lambda t: int(t["id"]), reverse=True)
    return threads[:limit]


# ── Claude analysis ────────────────────────────────────────────────────────────

ANALYSIS_PROMPT = """You are helping configure a Discord channel ingestion system for the Protocol Institute's C3PO research assistant.

Analyze this Discord channel and propose an ingestion configuration.

CHANNEL INFO:
Name: #{name}
Topic: {topic}
Guild: Protocol Institute

RECENT MESSAGES SAMPLE ({msg_count} messages, oldest to newest):
{messages}

RECENT THREADS ({thread_count} threads, newest first):
{threads}

Based on this sample, answer each question and output a JSON config block.

Questions to consider:
- What is the primary purpose of this channel? (general discussion, SIG meeting channel, announcements, resource sharing, off-topic, etc.)
- Does it have recurring structured meetings/sessions with transcript threads? If so, what naming patterns do the threads use?
- What's the appropriate ingestion type: "general" (embed messages into discord namespace) or "sig" (meeting detection + summaries, embed into sig namespace)?
- Are messages generally substantive enough to embed, or is there a lot of noise?
- What filtering thresholds make sense (min_chars_orig, min_chars_reply)?

Output ONLY valid JSON with this exact structure (no markdown fences):
{{
  "type": "general" or "sig",
  "namespace": "discord" or "sig",
  "display": "<human-readable channel name, e.g. #general or SIGFPT>",
  "sig_display": "<SIG acronym if type=sig, else null>",
  "min_chars_orig": <integer, 0-100>,
  "min_chars_reply": <integer, 0-200>,
  "meeting_patterns": [<regex strings if type=sig, else empty list>],
  "onboarding_notes": "<2-3 sentence summary of channel purpose and ingestion notes>"
}}"""


def analyze_channel(channel_id: str, channel_info: dict, messages: list[dict], threads: list[dict]) -> dict:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    def fmt_msg(m: dict) -> str:
        ts_ms = (int(m["id"]) >> 22) + 1420070400000
        d = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        author = m.get("author", {}).get("username", "?")
        content = m.get("content", "")[:200].replace("\n", " ")
        return f"  [{d}] @{author}: {content}"

    def fmt_thread(t: dict) -> str:
        ts_ms = (int(t["id"]) >> 22) + 1420070400000
        d = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        msgs = t.get("message_count", "?")
        return f"  [{d}] {t['name']!r} ({msgs} msgs)"

    prompt = ANALYSIS_PROMPT.format(
        name=channel_info.get("name", channel_id),
        topic=channel_info.get("topic", "(none)"),
        msg_count=len(messages),
        messages="\n".join(fmt_msg(m) for m in messages[-40:]) or "  (no messages)",
        thread_count=len(threads),
        threads="\n".join(fmt_thread(t) for t in threads[:SAMPLE_THREADS]) or "  (no threads)",
    )

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text.strip()
    # Strip any accidental markdown fences
    raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.M)
    raw = re.sub(r'\s*```$', '', raw, flags=re.M)
    return json.loads(raw)


# ── Manifest helpers ───────────────────────────────────────────────────────────

def load_manifest() -> dict:
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text())
    return {"version": 1, "guild_id": GUILD_ID, "channels": {}}


def save_manifest(manifest: dict):
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Onboard a Discord channel into C3PO.")
    parser.add_argument("--channel", required=True, help="Discord channel ID to onboard")
    parser.add_argument("--backfill", action="store_true", help="Run full backfill after onboarding")
    parser.add_argument("--archive", action="store_true",
                        help="Mark as archived (one-time sweep, excluded from daily rotation). Implies --backfill.")
    parser.add_argument("--yes", action="store_true", help="Accept proposed config without prompting")
    args = parser.parse_args()

    if args.archive:
        args.backfill = True

    channel_id = args.channel

    manifest = load_manifest()
    if channel_id in manifest["channels"]:
        existing = manifest["channels"][channel_id]
        print(f"Channel {channel_id} already in manifest as {existing['display']!r} (type={existing['type']}, status={existing['status']}).")
        print("To re-onboard, remove it from data/channel_manifest.json first.")
        sys.exit(0)

    print(f"Fetching channel info for {channel_id}...")
    try:
        channel_info = fetch_channel_info(channel_id)
    except Exception as e:
        print(f"ERROR: cannot access channel {channel_id}: {e}")
        print("Make sure the bot has access to this channel.")
        sys.exit(1)

    print(f"  #{channel_info.get('name')}  (type={channel_info.get('type')})")

    print(f"Fetching sample messages ({SAMPLE_MESSAGES})...")
    messages = fetch_recent_messages(channel_id)
    print(f"  {len(messages)} messages fetched")

    print(f"Fetching recent threads ({SAMPLE_THREADS})...")
    threads = fetch_recent_threads(channel_id)
    print(f"  {len(threads)} threads found")

    print("\nAnalyzing with Claude...")
    proposed = analyze_channel(channel_id, channel_info, messages, threads)

    print("\n" + "═" * 60)
    print("PROPOSED CONFIG")
    print("═" * 60)
    print(f"  Channel:          #{channel_info.get('name')} ({channel_id})")
    print(f"  Type:             {proposed['type']}")
    print(f"  Namespace:        {proposed['namespace']}")
    print(f"  Display:          {proposed['display']}")
    if proposed.get("sig_display"):
        print(f"  SIG display:      {proposed['sig_display']}")
    print(f"  min_chars_orig:   {proposed['min_chars_orig']}")
    print(f"  min_chars_reply:  {proposed['min_chars_reply']}")
    if proposed.get("meeting_patterns"):
        print(f"  Meeting patterns:")
        for p in proposed["meeting_patterns"]:
            print(f"    {p!r}")
    print(f"  Notes:  {proposed['onboarding_notes']}")
    print("═" * 60)

    if not args.yes:
        answer = input("\nAccept this config? [y/N/edit] ").strip().lower()
        if answer == "edit":
            print("\nPaste edited JSON (end with a line containing just '---'):")
            lines = []
            while True:
                line = input()
                if line.strip() == "---":
                    break
                lines.append(line)
            proposed = json.loads("\n".join(lines))
            print("Updated config accepted.")
        elif answer != "y":
            print("Aborted — channel not added to manifest.")
            sys.exit(0)

    # Build manifest entry
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    status = "archived" if args.archive else "active"
    entry = {
        "channel_id": channel_id,
        "name": channel_info.get("name", channel_id),
        "display": proposed["display"],
        "type": proposed["type"],
        "namespace": proposed["namespace"],
        "status": status,
        "added": today,
        "backfill_complete": False,
        "min_chars_orig": proposed.get("min_chars_orig", 20),
        "min_chars_reply": proposed.get("min_chars_reply", 150),
        "onboarding_notes": proposed.get("onboarding_notes", ""),
    }
    if proposed["type"] == "sig":
        entry["sig_display"] = proposed.get("sig_display") or proposed["display"]
        entry["meeting_patterns"] = proposed.get("meeting_patterns", [])

    manifest["channels"][channel_id] = entry
    save_manifest(manifest)
    mode_label = "archived (one-time sweep)" if args.archive else "active (daily monitoring)"
    print(f"\n✓ Added #{channel_info.get('name')} to channel manifest [{mode_label}].")

    run_backfill = args.backfill or (not args.yes and input("\nRun initial backfill now? [y/N] ").strip().lower() == "y")
    if run_backfill:
        print("\nRunning backfill...")
        if proposed["type"] == "general":
            # --channel scopes to just this channel; --backfill-days goes all the way back
            # for archived channels vs. the 30-day default for new active channels.
            cmd = [sys.executable, str(Path(__file__).parent / "sync_discord.py"),
                   "--channel", channel_id]
            if args.archive:
                cmd += ["--backfill-days", "3650"]
            result = subprocess.run(cmd, cwd=Path(__file__).parent.parent)
        elif proposed["type"] == "sig":
            # sync_sig already does a 1500-day backfill on first run for new channels.
            result = subprocess.run(
                [sys.executable, str(Path(__file__).parent / "sync_sig.py"), "--channel", channel_id],
                cwd=Path(__file__).parent.parent,
            )
        else:
            result = subprocess.CompletedProcess(args=[], returncode=0)

        if result.returncode != 0:
            print(f"ERROR: backfill failed (exit {result.returncode}) — channel added to manifest but backfill_complete remains False.")
            sys.exit(result.returncode)

        # Mark backfill complete
        manifest = load_manifest()
        manifest["channels"][channel_id]["backfill_complete"] = True
        save_manifest(manifest)
        print("✓ Backfill complete.")
    else:
        print(f"\nNext: run the appropriate sync script to backfill.")
        if proposed["type"] == "general":
            print(f"  python3 ingest/sync_discord.py")
        elif proposed["type"] == "sig":
            print(f"  python3 ingest/sync_sig.py --channel {channel_id}")


if __name__ == "__main__":
    main()
