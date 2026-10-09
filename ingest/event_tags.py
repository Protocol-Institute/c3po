"""
Event and time tags for Pinecone vectors (plans/event-awareness.md, Phase B).

Two metadata fields, added to every vector without re-embedding:

    ts_unix   Unix seconds of the item's own date, an int. Pinecone range filters
              ($gte/$lte) work only on numbers, so this is what makes "since June"
              or "during the 2025 symposium" expressible as a filter. (Not `ts`:
              the `transcripts` namespace already stores an ISO string under that name.)
    event_id  a list of registry ids the item belongs to. Only deterministic cases
              are tagged here; judgment cases (Substack, PDFs, Discord channels) are
              Phase D and go through a review queue, never a guess.

`tag_metadata()` is called from the one choke point every script writes through
(`utils._GuardedIndex.upsert`), so new content arrives tagged. `bin/backfill_event_tags.py`
does the same for what was already in the index.

The rules live here, in one place, so ingest and backfill cannot disagree.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

CONFIG_PATH = Path(__file__).parent.parent / "config" / "event_tags.json"

# First usable field wins. meeting_date beats timestamp because a meeting thread is
# created ahead of the session (so its Discord timestamp is not the meeting day).
DATE_FIELDS = ("meeting_date", "scheduled_date", "timestamp", "date", "fetch_date", "ts")

DISCORD_EPOCH_MS = 1420070400000
# Vector ids whose digits are a Discord snowflake of the item itself. Meeting and
# summary ids carry the (early-created) thread id, so they are deliberately absent.
SNOWFLAKE_ID = re.compile(r"^(?:sig_msg|sig_discussion|discord|discord__thread|discord__forum)__(\d{17,20})$")


def to_unix(value) -> int | None:
    """Unix seconds from 'YYYY-MM-DD', ISO-8601 (with or without Z), or a number."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value) if value > 10_000_000 else None
    s = str(value).strip()
    if not s:
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            return int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return int(d.timestamp())
    except ValueError:
        return None


def snowflake_to_unix(vector_id: str) -> int | None:
    m = SNOWFLAKE_ID.match(vector_id or "")
    if not m:
        return None
    return ((int(m.group(1)) >> 22) + DISCORD_EPOCH_MS) // 1000


def vector_ts(vector_id: str, md: dict) -> int | None:
    """The item's own date as Unix seconds, from metadata first, then its id."""
    for field in DATE_FIELDS:
        t = to_unix(md.get(field))
        if t:
            return t
    return snowflake_to_unix(vector_id)


def load_config() -> dict:
    cfg = {"namespace_events": {}, "series_events": {}, "channel_events": {}, "sig_events": True}
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    return cfg


def event_ids(namespace: str, md: dict, cfg: dict | None = None) -> list[str]:
    """Registry ids a vector belongs to, from deterministic rules only."""
    cfg = cfg or load_config()
    out: list[str] = []
    if namespace in cfg["namespace_events"]:                  # symposium -> its event
        out.append(cfg["namespace_events"][namespace])
    series = md.get("series")
    if namespace == "videos" and series in cfg["series_events"]:
        out.append(cfg["series_events"][series])
    if namespace == "sig" and cfg.get("sig_events", True) and md.get("sig_display"):
        out.append(f"sig:{md['sig_display']}")                # matches the calendar series keys
    ch = md.get("channel_id") or md.get("channel_name")
    if namespace in ("discord", "discord_links") and ch in cfg["channel_events"]:
        out.append(cfg["channel_events"][ch])
    return out


def tag_metadata(namespace: str, vector_id: str, md: dict, cfg: dict | None = None) -> dict:
    """md plus ts_unix / event_id where they can be determined. Never overwrites."""
    md = dict(md)
    if "ts_unix" not in md:
        t = vector_ts(vector_id, md)
        if t:
            md["ts_unix"] = t
    if "event_id" not in md:
        ev = event_ids(namespace, md, cfg)
        if ev:
            md["event_id"] = ev
    return md
