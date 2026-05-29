"""
Discord scheduled events sync for C3PO.

Fetches all guild scheduled events via the Discord REST API, decodes
recurrence rules into human-readable cadence strings, and updates
config/discord_channels.json with accurate schedule data.

Matched channels get:
  cadence         — e.g. "Every 2 weeks on Friday at 17:00 UTC"
  next_event_name — official Discord event name
  next_event_iso  — ISO 8601 datetime of next occurrence
  next_event_time — human-readable, e.g. "Fri 29 May 17:00 UTC"

These fields are included in embed text and Pinecone metadata so the
discord_guide retrieval returns up-to-date schedule info.

Matching strategy: event name keywords vs. channel sig_display and name.
Unmatched events are logged but not stored (they don't correspond to a
channel we track). Manual overrides are preserved via recommend_newcomers_override.

Usage:
    python3 ingest/sync_discord_events.py
    python3 ingest/sync_discord_events.py --dry-run
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
from utils import embed_chunks, get_voyage_client, get_pinecone_index, append_run_log

load_dotenv(Path(__file__).parent.parent / ".env")

REGISTRY_PATH = Path(__file__).parent.parent / "config" / "discord_channels.json"
NAMESPACE     = "discord_guide"
DISCORD_API   = "https://discord.com/api/v10"
GUILD_ID      = os.environ.get("DISCORD_GUILD_ID", "1082444651946049567")

VOYAGE_MODEL  = "voyage-3"

# ── Recurrence decoding ────────────────────────────────────────────────────────

_FREQ  = {0: "yearly", 1: "monthly", 2: "weekly", 3: "daily"}
_DAYS  = {0: "Monday", 1: "Tuesday", 2: "Wednesday",
          3: "Thursday", 4: "Friday", 5: "Saturday", 6: "Sunday"}
_ORD   = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th", -1: "last"}


def decode_recurrence(rr: dict, start_iso: str) -> str:
    """Turn a Discord recurrence_rule dict into a human-readable cadence string."""
    if not rr:
        return ""

    freq     = rr.get("frequency", 2)
    interval = rr.get("interval", 1)

    # Parse start time for clock
    try:
        start = datetime.fromisoformat(start_iso).astimezone(timezone.utc)
        clock = start.strftime("%H:%M UTC")
    except Exception:
        clock = ""

    if rr.get("by_weekday") is not None:
        days = " + ".join(_DAYS[d] for d in rr["by_weekday"])
        if freq == 2:  # weekly
            every = "Weekly" if interval == 1 else f"Every {interval} weeks"
            return f"{every} on {days}" + (f" at {clock}" if clock else "")
        if freq == 3:  # daily
            return f"Every {interval} day(s)" + (f" at {clock}" if clock else "")

    if rr.get("by_n_weekday"):
        parts = []
        for nwd in rr["by_n_weekday"]:
            n, day = nwd["n"], nwd["day"]
            ordinal = _ORD.get(n, f"{n}th")
            parts.append(f"{ordinal} {_DAYS[day]}")
        return "Monthly — " + " + ".join(parts) + (f" at {clock}" if clock else "")

    if freq == 1 and rr.get("by_month_day"):
        days_str = ", ".join(str(d) for d in rr["by_month_day"])
        return f"Monthly on day {days_str}" + (f" at {clock}" if clock else "")

    return f"Every {interval} {_FREQ.get(freq, '?')}(s)" + (f" at {clock}" if clock else "")


def fmt_event_time(iso: str) -> str:
    """Human-readable next event time, e.g. 'Fri 29 May 2026 17:00 UTC'."""
    try:
        dt = datetime.fromisoformat(iso).astimezone(timezone.utc)
        return dt.strftime("%a %-d %b %Y %H:%M UTC")
    except Exception:
        return iso


# ── Discord REST ───────────────────────────────────────────────────────────────

def discord_get(path: str) -> list | dict:
    token = os.environ["DISCORD_BOT_TOKEN"]
    req = urllib.request.Request(
        f"{DISCORD_API}{path}",
        headers={"Authorization": f"Bot {token}", "User-Agent": "C3PO-Events/1.0"},
    )
    while True:
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                body = json.loads(e.read())
                time.sleep(float(body.get("retry_after", 1)) + 0.1)
            else:
                raise


def fetch_scheduled_events() -> list[dict]:
    return discord_get(f"/guilds/{GUILD_ID}/scheduled-events?with_user_count=true")


# ── Channel matching ───────────────────────────────────────────────────────────

def _keywords(s: str) -> set[str]:
    """Lower-cased tokens of length > 2, stripped of punctuation."""
    import re
    return {w.lower() for w in re.split(r'\W+', s) if len(w) > 2}


def match_event_to_channel(event_name: str, channels: dict) -> str | None:
    """
    Return channel_id of best match, or None.

    Priority:
    1. sig_display appears in event name (case-insensitive substring)
    2. Any item in channel's event_keywords list appears in event name
    3. Keyword overlap (>= 2 tokens) between event name and channel name
    """
    ev_lower = event_name.lower()
    ev_kw    = _keywords(event_name)

    best_id    = None
    best_score = 0

    for cid, ch in channels.items():
        if ch.get("status") != "active":
            continue

        # 1. sig_display substring
        sig = (ch.get("sig_display") or "").lower()
        if sig and sig in ev_lower:
            return cid

        # 2. manual event_keywords (e.g. alternate event-name spellings)
        for kw in ch.get("event_keywords", []):
            if kw.lower() in ev_lower:
                return cid

        # 3. keyword overlap with channel name
        ch_kw = _keywords(ch.get("name", ""))
        score = len(ev_kw & ch_kw)
        if score > best_score:
            best_score = score
            best_id    = cid

    return best_id if best_score >= 2 else None


# ── Pinecone re-embed ──────────────────────────────────────────────────────────

def embed_channel_entry(entry: dict, vc, index) -> None:
    """Re-embed a single updated channel entry."""
    path         = entry.get("path", entry["display"])
    cadence_line = (f"Meets: {entry['cadence']}\n"          if entry.get("cadence")         else "")
    next_line    = (f"Next session: {entry['next_event_time']}\n" if entry.get("next_event_time") else "")
    lead_line    = (f"Lead: {entry['lead']}\n"               if entry.get("lead")            else "")

    text = (
        f"Discord location: {path}\n"
        f"Channel: {entry['display']}  |  Section: {entry.get('category', '')}\n"
        f"Type: {entry.get('type_label', '')}\n"
        f"{cadence_line}{next_line}{lead_line}"
        f"Topic: {entry.get('topic') or '(none)'}\n\n"
        f"{entry.get('description', '')}"
    )
    vectors = embed_chunks([text], vc)
    meta = {
        "chunk_type":              "discord_channel",
        "channel_id":              entry["id"],
        "name":                    entry["name"],
        "display":                 entry["display"],
        "path":                    path,
        "type_label":              entry.get("type_label", ""),
        "category":                entry.get("category", ""),
        "guide_blurb":             entry.get("guide_blurb", ""),
        "sig_display":             entry.get("sig_display", ""),
        "cadence":                 entry.get("cadence", ""),
        "next_event_time":         entry.get("next_event_time", ""),
        "next_event_iso":          entry.get("next_event_iso", ""),
        "lead":                    entry.get("lead", ""),
        "recommend_to_newcomers":  entry.get("recommend_to_newcomers", True),
        "status":                  entry.get("status", "active"),
        "text":                    text[:1000],
    }
    record = {"id": f"discord_channel__{entry['id']}", "values": vectors[0], "metadata": meta}
    index.upsert(vectors=[record], namespace=NAMESPACE)


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f"Fetching scheduled events for guild {GUILD_ID}…")
    events = fetch_scheduled_events()
    print(f"  {len(events)} events found")

    reg      = json.loads(REGISTRY_PATH.read_text())
    channels = reg["channels"]

    matched   = 0
    unmatched = []
    updated   = []   # channel_ids that need re-embedding

    # Collect all events per channel; resolve to primary (highest user_count) after.
    channel_events: dict[str, list[dict]] = {}

    for ev in events:
        if ev.get("status") in (3, 4):   # completed or cancelled
            continue

        name = ev["name"]
        cid  = match_event_to_channel(name, channels)
        if cid is None:
            unmatched.append(name)
            print(f"  UNMATCHED: {name!r}")
            continue

        channel_events.setdefault(cid, []).append(ev)

    for cid, evs in channel_events.items():
        # Primary = highest subscriber/interested count; tie-break: earliest start time.
        primary = max(evs, key=lambda e: (e.get("user_count", 0), -e["scheduled_start_time"].__hash__()))
        secondary = [e for e in evs if e is not primary]

        name     = primary["name"]
        start    = primary["scheduled_start_time"]
        rr       = primary.get("recurrence_rule")
        cadence  = decode_recurrence(rr, start) if rr else ""
        next_iso = start
        next_fmt = fmt_event_time(start)

        ch = channels[cid]
        old_cadence  = ch.get("cadence", "")
        old_next_iso = ch.get("next_event_iso", "")

        ch["next_event_name"] = name
        ch["next_event_iso"]  = next_iso
        ch["next_event_time"] = next_fmt
        if cadence:
            ch["cadence"] = cadence

        if secondary:
            ch["secondary_events"] = [
                {"name": e["name"], "next_event_iso": e["scheduled_start_time"],
                 "user_count": e.get("user_count", 0)}
                for e in secondary
            ]

        changed = (cadence != old_cadence or next_iso != old_next_iso)
        status  = "UPD" if changed else "OK "
        print(f"  {status} #{ch['name']!r:35s} ← {name!r}" +
              (f" (+ {len(secondary)} secondary)" if secondary else ""))
        if cadence:
            print(f"       cadence: {cadence}")
        print(f"       next:    {next_fmt}")
        if secondary:
            for e in secondary:
                print(f"       skip:    {e['name']!r} (user_count={e.get('user_count',0)})")

        if changed:
            updated.append(cid)
        matched += 1

    print(f"\n── Results ──────────────────────────────────────")
    print(f"  Matched   : {matched}")
    print(f"  Unmatched : {len(unmatched)} — {unmatched}")
    print(f"  To re-embed: {len(updated)}")

    if args.dry_run:
        return

    # Save registry
    reg["updated_at"] = now
    REGISTRY_PATH.write_text(json.dumps(reg, indent=2, ensure_ascii=False))

    # Re-embed only changed channels
    if updated:
        vc    = get_voyage_client()
        index = get_pinecone_index()
        for cid in updated:
            ch = channels[cid]
            try:
                embed_channel_entry(ch, vc, index)
                print(f"  EMB #{ch['name']}")
            except Exception as exc:
                print(f"  ⚠ embed failed for #{ch['name']}: {exc}")

    append_run_log({
        "ts":        now,
        "script":    "sync_discord_events",
        "matched":   matched,
        "unmatched": len(unmatched),
        "reembedded": len(updated),
    })


if __name__ == "__main__":
    main()
