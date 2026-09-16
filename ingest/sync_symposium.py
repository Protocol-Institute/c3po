"""
Ingest the Protocol Symposium 2026 program into the Pinecone `symposium` namespace.

Source of truth is the D1-backed public JSON API on protocol-institute.org, not
the rendered page — scraping our own HTML would reproduce the stale-snapshot
failure that once had c3po reporting "DRG has 0 archived sessions".

    GET /api/symposium/proposals   all shortlisted talks/workshops/interactives
    GET /api/symposium/sessions    the special sessions, with block times
    GET /api/symposium/sessions/:slug   description + agenda for one session

Chunk types written (see plans/symposium-ingest.md):
    symposium_overview   1    event-level facts
    symposium_block      4    special sessions ("The Art of Memory", ...)
    symposium_session   61    one per talk / workshop / interactive
    symposium_workshop   5    workshop audience/takeaways/activities

PII: the proposals API returns speaker/organizer/host email addresses and the
internal member vote aggregates. c3po's corpus and bot are both public, so this
script works from an explicit field allowlist and additionally refuses to embed
any chunk containing an email address. Do not replace the allowlist with a
denylist — host3_email..host5_email reached the public payload precisely because
a new column joined it automatically.

State file: data/symposium_state.json  (record key → content hash + vector ids)

Usage:
    python3 ingest/sync_symposium.py --dry-run
    python3 ingest/sync_symposium.py
    python3 ingest/sync_symposium.py --force     # re-embed everything
    python3 ingest/sync_symposium.py --prune     # also drop vanished records
"""

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
from utils import embed_chunks, get_voyage_client, get_pinecone_index, clean_text
from sync_meeting_notes import match_sig, SIG_NAMES

load_dotenv(Path(__file__).parent.parent / ".env")

BASE_URL   = "https://protocol-institute.org"
EVENT_PATH = "/events/protocol-symposium-2026"
NAMESPACE  = "symposium"
STATE_PATH = Path(__file__).parent.parent / "data" / "symposium_state.json"

EVENT_NAME  = "Protocol Symposium 2026"
EVENT_DATES = "September 21-25, 2026"

# Fields that may be read off a proposal record. Everything absent from this
# list stays server-side, including every *_email field, the submitter's private
# `comments` note, and the score/voter_count/total_votes vote aggregates.
ALLOWED_PROPOSAL_FIELDS = {
    "id", "type", "slug", "track", "title", "abstract", "session",
    "speaker_name", "speaker_website", "co_speakers", "artifact_type",
    "organizer_name", "organizer_bio", "co_organizer_name", "co_organizer_bio",
    "host3_name", "host3_bio", "host4_name", "host4_bio", "host5_name", "host5_bio",
    "audience", "takeaways", "activities", "max_participants",
    "scheduled_date", "scheduled_time_utc", "scheduled_end_time_utc",
    "registration_url", "schedule_track", "workshop_sessions",
}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


# ── HTTP ──────────────────────────────────────────────────────────────────────

def fetch_json(path: str, attempts: int = 3):
    """GET a JSON endpoint, retrying transient network failures.

    This runs unattended as a daemon step, and an uncaught exception here fails
    the whole cycle rather than just this step — the same blast radius as the
    SIG_TITLE_MAP crash. A single dropped read should cost a retry, not a cycle.
    """
    last = None
    for attempt in range(1, attempts + 1):
        try:
            req = Request(BASE_URL + path,
                          headers={"User-Agent": "c3po-ingest/1.0 (protocol-institute.org)"})
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except (URLError, OSError, json.JSONDecodeError) as e:
            last = e
            if attempt < attempts:
                time.sleep(2 * attempt)
    raise RuntimeError(f"GET {path} failed after {attempts} attempts: {last}")


# ── Formatting ────────────────────────────────────────────────────────────────

def day_label(date_str: str | None) -> str:
    """'2026-09-24' -> 'Thursday, September 24, 2026'."""
    if not date_str:
        return ""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").strftime("%A, %B %-d, %Y")
    except ValueError:
        return date_str


def time_range(start: str | None, end: str | None) -> str:
    if not start:
        return ""
    return f"{start}-{end} UTC" if end else f"{start} UTC"


def when_line(date_str: str | None, start: str | None, end: str | None) -> str:
    day = day_label(date_str)
    rng = time_range(start, end)
    if day and rng:
        return f"{day} at {rng}"
    return day or rng or "Time to be confirmed"


def hosts_of(p: dict) -> list[str]:
    """Every named human on a record, speaker or workshop host, in order."""
    names = [p.get("speaker_name"), p.get("co_speakers"), p.get("organizer_name"),
             p.get("co_organizer_name"), p.get("host3_name"), p.get("host4_name"),
             p.get("host5_name")]
    return [n.strip() for n in names if n and n.strip() and n.strip().lower() not in {"none", "n/a"}]


# Non-SIG track values the submission form allows. match_sig() correctly returns
# None for these, so they need their own collapse: two spellings of the alumni
# track, and a value that means "no track at all" rather than naming one.
NON_SIG_TRACKS = {
    "continuing alumni research (sop 23-25 cohorts)": "Alumni",
    "alumni":                                        "Alumni",
    "latin america track":                           "Latin America",
    "not part of an existing activity stream":       "",
    "general":                                       "General",
}


def canonical_track(raw: str | None) -> str:
    """Collapse the API's several spellings per track onto one canonical label.

    The program stores e.g. both 'SIGPfB' and 'SIGP4B (Protocols for Business)',
    both 'MRG' and 'MRG (Memory Research Group)'. Reuses match_sig() rather than
    adding an eighth SIG registry to the codebase, then handles the non-SIG
    tracks it is not meant to know about. Returns "" for "no track".
    """
    if not raw:
        return ""
    sig = match_sig(raw)
    if sig:
        return sig
    return NON_SIG_TRACKS.get(raw.strip().lower(), raw.strip())


def track_label(raw: str | None) -> str:
    """Canonical track, expanded to its full name for the embedded text.

    The abbreviation alone is not enough: with only 'DRG' in context the model
    guessed 'Drama Research Group', and 'SIGPSY' became 'Psychology SIG'. Carry
    the expansion so it never has to invent one.
    """
    key = canonical_track(raw)
    if not key:
        return ""
    full = SIG_NAMES.get(key)
    return f"{key} ({full})" if full else key


# ── Chunk builders ────────────────────────────────────────────────────────────

def session_chunk(p: dict) -> dict:
    kind = {"talk": "Talk", "workshop": "Workshop", "interactive": "Interactive session"}.get(p["type"], "Session")
    track = canonical_track(p.get("track"))
    people = hosts_of(p)

    if p["type"] == "workshop":
        days = sorted({s["date"] for s in (p.get("workshop_sessions") or []) if s.get("date")})
        when = (f"{day_label(days[0])} to {day_label(days[-1])}" if len(days) > 1
                else day_label(days[0]) if days else "Time to be confirmed")
        when += f" ({len(p.get('workshop_sessions') or [])} repeated sessions; attend any one)"
    else:
        when = when_line(p.get("scheduled_date"), p.get("scheduled_time_utc"),
                         p.get("scheduled_end_time_utc"))
        if not p.get("scheduled_date") and p.get("session") and p["session"] != "General":
            when = f"within the '{p['session']}' block (exact order set by the session host)"

    lines = [
        f"{EVENT_NAME} - {kind}",
        f"Title: {p['title']}",
    ]
    if people:
        lines.append(f"{'Hosts' if p['type'] == 'workshop' else 'Speakers'}: {', '.join(people)}")
    if track:
        lines.append(f"Track: {track_label(p.get('track'))}")
    if p.get("session") and p["session"] != "General":
        lines.append(f"Part of the special session: {p['session']}")
    lines.append(f"When: {when}")
    if p.get("abstract"):
        lines += ["", clean_text(p["abstract"])]

    return {
        "key":  f"proposal:{p['slug'] or p['id']}",
        "type": "symposium_session",
        "text": "\n".join(lines),
        "meta": {
            "chunk_type":     "symposium_session",
            "event":          EVENT_NAME,
            "title":          p["title"],
            "slug":           p.get("slug") or "",
            "item_type":      p["type"],
            "track":          track,
            "session":        p.get("session") or "General",
            "speakers":       ", ".join(people),
            "scheduled_date": p.get("scheduled_date") or "",
            "scheduled_time_utc": p.get("scheduled_time_utc") or "",
            "url":            f"{BASE_URL}{EVENT_PATH}/talks#p-{p['id']}",
        },
    }


def workshop_chunk(p: dict) -> dict | None:
    """Workshop audience/takeaways/activities — the substance for thematic questions.

    These fields never appear in full on the program page and are far richer than
    the abstract, which is why a workshop gets a second chunk rather than having
    them flattened into its session chunk.
    """
    if p["type"] != "workshop":
        return None
    if not any(p.get(f) for f in ("audience", "takeaways", "activities")):
        return None

    people = hosts_of(p)
    lines = [
        f"{EVENT_NAME} - Workshop details",
        f"Title: {p['title']}",
    ]
    if people:
        lines.append(f"Hosts: {', '.join(people)}")
    if track_label(p.get("track")):
        lines.append(f"Track: {track_label(p.get('track'))}")

    for seq, s in enumerate(p.get("workshop_sessions") or [], 1):
        note = f" ({s['note']})" if s.get("note") else ""
        lines.append(f"  Session {s.get('seq', seq)}: {day_label(s.get('date'))}, "
                     f"{time_range(s.get('start_time'), s.get('end_time'))}{note}")
    if p.get("max_participants"):
        lines.append(f"Participant limit: {p['max_participants']}")
    for label, field in (("Who it is for", "audience"),
                         ("What participants take away", "takeaways"),
                         ("What happens in the workshop", "activities")):
        if p.get(field):
            lines += ["", f"{label}: {clean_text(p[field])}"]

    return {
        "key":  f"workshop:{p['slug'] or p['id']}",
        "type": "symposium_workshop",
        "text": "\n".join(lines),
        "meta": {
            "chunk_type": "symposium_workshop",
            "event":      EVENT_NAME,
            "title":      p["title"],
            "slug":       p.get("slug") or "",
            "track":      canonical_track(p.get("track")),
            "speakers":   ", ".join(people),
            "url":        f"{BASE_URL}{EVENT_PATH}/program/workshops/{p.get('slug', '')}/",
        },
    }


def block_chunk(sess: dict, detail: dict, proposals: list[dict]) -> dict:
    """One special session, with the talks that sit inside it."""
    inner = [p for p in proposals if (p.get("session") or "") == sess["name"]]
    inner.sort(key=lambda p: (p.get("scheduled_time_utc") or "zz", p["title"]))

    lines = [
        f"{EVENT_NAME} - Special session: {sess['name']}",
        f"When: {when_line(sess.get('date'), sess.get('start_time'), sess.get('end_time'))}",
    ]
    if detail.get("description"):
        lines += ["", clean_text(detail["description"])]
    if detail.get("agenda"):
        lines += ["", f"Agenda: {clean_text(detail['agenda'])}"]
    if inner:
        lines += ["", f"Talks in this session ({len(inner)}):"]
        for p in inner:
            who = ", ".join(hosts_of(p))
            at = p.get("scheduled_time_utc")
            lines.append(f"  - {p['title']}" + (f" - {who}" if who else "")
                         + (f" ({at} UTC)" if at else ""))

    return {
        "key":  f"block:{sess['slug']}",
        "type": "symposium_block",
        "text": "\n".join(lines),
        "meta": {
            "chunk_type":     "symposium_block",
            "event":          EVENT_NAME,
            "title":          sess["name"],
            "slug":           sess["slug"],
            "scheduled_date": sess.get("date") or "",
            "talk_count":     len(inner),
            "url":            f"{BASE_URL}{EVENT_PATH}/sessions/{sess['slug']}/",
        },
    }


def overview_chunk(proposals: list[dict], sessions: list[dict]) -> dict:
    talks     = [p for p in proposals if p["type"] in ("talk", "interactive")]
    workshops = [p for p in proposals if p["type"] == "workshop"]
    tracks    = sorted({track_label(p.get("track")) for p in proposals
                        if canonical_track(p.get("track")) and canonical_track(p.get("track")) != "General"})
    days      = sorted({p["scheduled_date"] for p in talks if p.get("scheduled_date")})

    lines = [
        f"{EVENT_NAME} - event overview",
        f"{EVENT_NAME} is the Protocol Institute's annual convening, held {EVENT_DATES}. "
        f"The theme is New Nature. The event is fully virtual.",
        "",
        f"Programme: {len(workshops)} hands-on workshops on September 21-22, each running "
        f"as repeated sessions across the two days, followed by {len(talks)} talks and "
        f"interactive sessions on {', '.join(day_label(d) for d in days)}.",
        f"Talks run roughly 15:00-23:00 UTC each day, with some evenings running two "
        f"parallel tracks.",
        "",
        f"Special sessions: {', '.join(s['name'] for s in sessions)}.",
        f"Tracks represented: {', '.join(tracks)}.",
        "",
        "Registration for live participation closed once the event reached capacity, and "
        "a public livestream of the talks is available. Workshops required their own "
        "separate registration.",
        f"The full programme is published at {BASE_URL}{EVENT_PATH}/.",
    ]
    return {
        "key":  "overview:symposium-2026",
        "type": "symposium_overview",
        "text": "\n".join(lines),
        "meta": {
            "chunk_type":  "symposium_overview",
            "event":       EVENT_NAME,
            "title":       f"{EVENT_NAME} overview",
            "talk_count":  len(talks),
            "workshop_count": len(workshops),
            "url":         f"{BASE_URL}{EVENT_PATH}/",
        },
    }


# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"records": {}}


def save_state(state: dict):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def content_hash(text: str, meta: dict) -> str:
    payload = text + json.dumps(meta, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def vector_id(key: str, seq: int = 0) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_.:-]", "-", key)
    return f"symposium__{slug}__{seq}"


# ── Main ──────────────────────────────────────────────────────────────────────

def build_chunks() -> list[dict]:
    raw_proposals = fetch_json("/api/symposium/proposals").get("proposals", [])
    proposals = [{k: v for k, v in p.items() if k in ALLOWED_PROPOSAL_FIELDS} for p in raw_proposals]

    sessions = fetch_json("/api/symposium/sessions").get("sessions", [])

    chunks = [overview_chunk(proposals, sessions)]
    for sess in sessions:
        try:
            detail = fetch_json(f"/api/symposium/sessions/{sess['slug']}").get("session", {})
        except RuntimeError as e:
            # A block without its description is degraded, not fatal — the talks
            # inside it still render from the proposals payload.
            print(f"  WARN: no detail for session '{sess['slug']}': {e}")
            detail = {}
        chunks.append(block_chunk(sess, detail, proposals))
    for p in proposals:
        chunks.append(session_chunk(p))
        wc = workshop_chunk(p)
        if wc:
            chunks.append(wc)

    # Belt and braces over the allowlist: nothing carrying an address is embedded.
    for c in chunks:
        found = EMAIL_RE.findall(c["text"]) + EMAIL_RE.findall(json.dumps(c["meta"]))
        if found:
            raise SystemExit(
                f"REFUSING TO EMBED: chunk {c['key']} contains an email address "
                f"({found[0]}). The field allowlist has been breached — fix that "
                f"before running again."
            )
    return chunks


def run(dry_run: bool = False, force: bool = False, prune: bool = False):
    state   = load_state()
    records = state.get("records", {})

    chunks = build_chunks()
    print(f"Built {len(chunks)} chunks from the live program")
    by_type: dict[str, int] = {}
    for c in chunks:
        by_type[c["type"]] = by_type.get(c["type"], 0) + 1
    for t, n in sorted(by_type.items()):
        print(f"  {t}: {n}")

    changed = [c for c in chunks if force or records.get(c["key"], {}).get("hash") != content_hash(c["text"], c["meta"])]
    seen    = {c["key"] for c in chunks}
    vanished = [k for k in records if k not in seen]

    print(f"\nNew or changed: {len(changed)}   Unchanged: {len(chunks) - len(changed)}"
          f"   Vanished from program: {len(vanished)}")
    for c in changed[:15]:
        status = "NEW" if c["key"] not in records else "UPDATED"
        print(f"  {status:8} {c['type']:20} {c['meta'].get('title', '')[:60]}")
    if len(changed) > 15:
        print(f"  ... and {len(changed) - 15} more")

    if dry_run:
        print("\n(dry run - nothing written)")
        return

    if not changed and not (prune and vanished):
        print("\nNothing to do.")
        return

    vc  = get_voyage_client()
    idx = get_pinecone_index()

    for c in changed:
        # Destructive re-sync: drop the record's previous vectors before writing
        # new ones, so a retitled talk cannot stay indexed under both titles.
        old_ids = records.get(c["key"], {}).get("vector_ids", [])
        if old_ids:
            idx.delete(ids=old_ids, namespace=NAMESPACE)

        vectors = embed_chunks([c["text"]], vc)
        vid = vector_id(c["key"])
        meta = {**c["meta"], "text": c["text"][:1600]}
        idx.upsert(vectors=[{"id": vid, "values": vectors[0], "metadata": meta}],
                   namespace=NAMESPACE)
        records[c["key"]] = {
            "hash":       content_hash(c["text"], c["meta"]),
            "vector_ids": [vid],
            "type":       c["type"],
            "title":      c["meta"].get("title", ""),
        }
        state["records"] = records
        save_state(state)

    if prune and vanished:
        ids = [i for k in vanished for i in records[k].get("vector_ids", [])]
        if ids:
            idx.delete(ids=ids, namespace=NAMESPACE)
        for k in vanished:
            print(f"  PRUNED  {records[k].get('type', '')} {records[k].get('title', '')[:60]}")
            del records[k]
        state["records"] = records
        save_state(state)
    elif vanished:
        print(f"\n{len(vanished)} record(s) no longer in the program; re-run with --prune to remove them.")

    state["last_sync"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    save_state(state)
    print(f"\nUpserted {len(changed)} chunk(s) into the '{NAMESPACE}' namespace.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Sync the Protocol Symposium 2026 program into Pinecone")
    ap.add_argument("--dry-run", action="store_true", help="Build and report without writing")
    ap.add_argument("--force",   action="store_true", help="Re-embed every chunk even if unchanged")
    ap.add_argument("--prune",   action="store_true", help="Delete vectors for records no longer in the program")
    args = ap.parse_args()
    run(dry_run=args.dry_run, force=args.force, prune=args.prune)
