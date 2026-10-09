"""
Build the event registry from the website's three public sources and hand it to
the Worker.

    Community calendar   public Google iCal (sigs@): SIG calls, community calls
    Institute calendar   public Google iCal ("Town Halls"): New Nature episodes
    Events history       protocol-institute.org/data/events.json: major events

c3po reads these and never writes them: the website owns the calendar
(plans/website-interface.md, plans/event-awareness.md section 1). A missing
event is a website fix.

Output: data/events_registry.json, and the same blob PUT to the Worker
(/api/admin/dashboard's sibling, /api/admin/events), which stores it in KV as
`events:registry`. The Worker builds its known-events digest from that copy.

Calendar entries are dated occurrences (kind "calendar"); history entries are
major events that content can belong to (kind "history", significance "major").
Recurring events are expanded over a window (90 days back, 180 ahead).

Calendar descriptions and locations are dropped on purpose: they carry meeting
links and sometimes email addresses, and the digest needs neither.

State: data/events_state.json  (content hash of the last blob handed over)

Usage:
    python3 ingest/sync_events.py
    python3 ingest/sync_events.py --dry-run
    python3 ingest/sync_events.py --force
"""

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

WORKER_URL    = "https://pibot.protocolized.io"
REGISTRY_PATH = ROOT / "data" / "events_registry.json"
STATE_PATH    = ROOT / "data" / "events_state.json"
ALIAS_PATH    = ROOT / "config" / "event_aliases.json"

EVENTS_JSON_URL = "https://protocol-institute.org/data/events.json"
CALENDARS = {
    "community": "sigs@protocol-institute.org",
    "institute": "c_39064bd346954b73835545c21dd13f812700b44c0fbb0723f390211473dded5c@group.calendar.google.com",
}

DAYS_BACK  = 90
DAYS_AHEAD = 180


def ical_url(calendar_id: str) -> str:
    return ("https://calendar.google.com/calendar/ical/"
            f"{urllib.parse.quote(calendar_id, safe='')}/public/basic.ics")


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def load_config() -> dict:
    cfg = {"sig_title_patterns": {}, "series_title_patterns": {}, "ignore_title_patterns": [],
           "extra_aliases": {}, "significance": {}, "archive_namespace": {}, "windows": {}, "exclusive": []}
    if ALIAS_PATH.exists():
        cfg.update(json.loads(ALIAS_PATH.read_text()))
    return cfg


# ── History events (events.json) ──────────────────────────────────────────────

def history_aliases(ev: dict, cfg: dict) -> list[str]:
    title = ev["title"]
    year = (ev.get("date_start") or "")[:4]
    out = {title.lower(), ev["id"].replace("-", " ")}
    if year and year in title:
        out.add(title.lower().replace(year, "").strip())
        kind = (ev.get("type") or "").lower().replace("-", " ")
        if kind:
            out.add(f"{year} {kind}")
    # The name people actually say: "Edge Lanna", not "Edge Lanna Workshop 2024".
    core = title.lower().replace(year, "") if year else title.lower()
    core = re.sub(r"\b(workshop|symposium|retreat|conference)\b", " ", core)
    core = " ".join(core.split())
    if len(core) >= 8 and " " in core:
        out.add(core)
    out.update(a.lower() for a in cfg["extra_aliases"].get(ev["id"], []))
    return sorted(a for a in out if a)


def history_events(cfg: dict) -> list[dict]:
    resp = requests.get(EVENTS_JSON_URL, timeout=30)
    resp.raise_for_status()
    raw = resp.json()
    raw = raw if isinstance(raw, list) else raw.get("events", [])
    out = []
    for ev in raw:
        if not ev.get("id") or not ev.get("title") or not ev.get("date_start"):
            continue
        out.append({
            "id":           ev["id"],
            "kind":         "history",
            "type":         ev.get("type", ""),
            "title":        ev["title"],
            "start":        ev["date_start"],
            "end":          ev.get("date_end") or ev["date_start"],
            "all_day":      True,
            "location":     ev.get("location", ""),
            "description":  (ev.get("short_description") or "").strip(),
            "url":          ev.get("resources_url") or "",
            "significance": cfg["significance"].get(ev["id"], "major"),
            "archive_namespace": cfg["archive_namespace"].get(ev["id"], ""),
            "exclusive":    ev["id"] in cfg["exclusive"],
            **{k: v for k, v in cfg["windows"].get(ev["id"], {}).items() if k in ("lead_days", "tail_days")},
            "aliases":      history_aliases(ev, cfg),
        })
    return out


# ── Calendar occurrences (iCal) ───────────────────────────────────────────────

def as_utc(value):
    """(iso string, all_day) for an iCal DTSTART/DTEND value."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"), False
    return value.isoformat(), True


def match_first(patterns: dict, title: str):
    t = title.lower()
    for key, needle in patterns.items():
        if needle.lower() in t:
            return key
    return None


def calendar_events(name: str, cal_id: str, cfg: dict, now: datetime) -> list[dict]:
    import icalendar
    import recurring_ical_events

    resp = requests.get(ical_url(cal_id), timeout=30)
    resp.raise_for_status()
    cal = icalendar.Calendar.from_ical(resp.content)
    start = now - timedelta(days=DAYS_BACK)
    end   = now + timedelta(days=DAYS_AHEAD)
    ignore = [p.lower() for p in cfg["ignore_title_patterns"]]

    out = []
    for e in recurring_ical_events.of(cal).between(start, end):
        title = " ".join(str(e.get("SUMMARY", "")).split())
        if not title or any(p in title.lower() for p in ignore):
            continue
        s_iso, all_day = as_utc(e["DTSTART"].dt)
        dtend = e.get("DTEND")
        e_iso = as_utc(dtend.dt)[0] if dtend else s_iso
        sig    = match_first(cfg["sig_title_patterns"], title)
        series = f"sig:{sig}" if sig else match_first(cfg["series_title_patterns"], title)
        out.append({
            "id":           f"{name}:{slugify(title)}:{s_iso}",
            "kind":         "calendar",
            "calendar":     name,
            "series":       series or "",
            "sig":          sig or "",
            "title":        title,
            "start":        s_iso,
            "end":          e_iso,
            "all_day":      all_day,
            "significance": "ordinary",
        })
    return out


# ── Hand-off ──────────────────────────────────────────────────────────────────

def content_hash(blob: dict) -> str:
    body = {k: v for k, v in blob.items() if k != "generated"}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]


def publish(blob: dict) -> None:
    admin_key = os.environ.get("ADMIN_KEY", "")
    if not admin_key:
        raise RuntimeError("ADMIN_KEY not set in .env")
    resp = requests.put(f"{WORKER_URL}/api/admin/events", json=blob,
                        headers={"X-Admin-Key": admin_key}, timeout=20)
    resp.raise_for_status()
    if not resp.json().get("ok"):
        raise RuntimeError(f"Worker rejected registry: {resp.text[:200]}")


def build(now: datetime) -> dict:
    cfg = load_config()
    events = history_events(cfg)
    for name, cal_id in CALENDARS.items():
        events += calendar_events(name, cal_id, cfg, now)
    events.sort(key=lambda e: (e["start"], e["id"]))
    return {
        "generated": now.strftime("%Y-%m-%dT%H:%MZ"),
        "window":    {"days_back": DAYS_BACK, "days_ahead": DAYS_AHEAD},
        "events":    events,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    blob = build(datetime.now(timezone.utc))
    h = content_hash(blob)
    hist = [e for e in blob["events"] if e["kind"] == "history"]
    cal  = [e for e in blob["events"] if e["kind"] == "calendar"]
    unmapped = sorted({e["title"] for e in cal if not e["series"]})
    print(f"Events: {len(hist)} history, {len(cal)} calendar occurrences "
          f"({len(cal) - sum(1 for e in cal if not e['series'])} in a series)")
    if unmapped:
        print("  calendar titles with no series:", "; ".join(unmapped[:8]))

    state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}
    if not args.force and state.get("hash") == h:
        print("Unchanged since last hand-off.")
        return
    if args.dry_run:
        print(f"[dry-run] would write {REGISTRY_PATH.name} and PUT {len(json.dumps(blob))} bytes")
        return

    REGISTRY_PATH.write_text(json.dumps(blob, indent=1))
    publish(blob)
    STATE_PATH.write_text(json.dumps({"hash": h, "at": blob["generated"]}))
    print(f"Registry handed to the Worker ({h}).")


if __name__ == "__main__":
    main()
