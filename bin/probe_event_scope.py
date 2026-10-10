"""
Event-awareness probe — plans/event-awareness.md (open question 4, Phase C, Phase F).

A saved list of questions, each with what the live worker must do with it:
which sources it may retrieve, and what the answer must or must not say. It exists
because the session-55 check of the symposium scoping was fifteen phrasings run
by hand and never written down, so "at least as well as before" could not be
tested. Re-run it after any change to event scoping, the known-events digest, or
the live-event window.

Groups
    A  symposium 2026   scoping by name, by date, and the "reach for prior work" escape
    B  other events     named by title, year, or short name; answered from the registry
    C  calendar         "next SIGPSY meeting" and friends: a fact in UTC
    D  honesty          must not invent an event, a date, or an invitation
    E  ordinary         content questions that must not be scoped or get event context
    F  clock            needs the worker to accept ?now= (Phase F); skipped unless --with-now
    G  time ranges      "since June", "last month", "in 2025" -> ts_unix filter (Phase E); needs ?now=, so --with-now

A case that checks only sources runs in sources-only mode (about a second, no model
cost); one that checks the answer text costs one answer (~$0.03, ~20 s). Cases run six
at a time (the admin key exempts the probe from the per-IP rate limit), so a full run
takes a minute or two. The checks are on `sources` and `answer` only, so the probe works
against any deployed worker without a debug hook.

Usage:
    python3 bin/probe_event_scope.py                 # groups A-E
    python3 bin/probe_event_scope.py --group C
    python3 bin/probe_event_scope.py --case sigpsy-next
    python3 bin/probe_event_scope.py --with-now      # also group F (Phase F, needs ADMIN_KEY)
    python3 bin/probe_event_scope.py --save out.json # keep answers + sources for reading
    python3 bin/probe_event_scope.py --list
"""

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

BASE = os.environ.get("PROBE_BASE", "https://pibot.protocolized.io")

DATE_RE = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|"
           r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s+\d{1,2}")

# Phrases the bot must never use: it issues no calls to action (session 54 VOICE rule).
CTA = ["we'd love to see", "come join", "join us", "don't miss", "sign up today", "register now"]

# Check keys, all optional:
#   scoped_to          every source's `source` namespace is in this set
#   includes_type      list; some source's `type` contains one of these substrings
#   not_source         no source comes from this namespace
#   min_namespaces     sources span at least this many namespaces
#   answer_has_any     answer contains at least one of these (case-insensitive)
#   answer_has_all     answer contains all of these
#   answer_regex       answer matches this regex (case-insensitive)
#   answer_lacks       answer contains none of these
#   answer_lacks_regex answer does not match this regex
#   dates_within       [from, to]; every source with a date falls inside (YYYY-MM-DD, inclusive)
#   no_sources_from    list of `source` labels that must not appear (undated: youtube, web)
#   until              ISO date; the case is skipped after it (time-bound facts)
#   now                ISO instant to pretend it is (group F)
CASES = [
    # ── A. The symposium, 2026 ────────────────────────────────────────────────
    # A1-A3 are phrasings quoted in the session-55 docs; A4-A7 are reconstructions.
    dict(id="sym-workshops", group="A", q="What workshops are happening at the Protocol Symposium?",
         scoped_to={"symposium"}, includes_type=["symposium_workshop"]),
    dict(id="sym-by-date", group="A", q="Which sessions run on September 23?",
         scoped_to={"symposium"}),
    dict(id="sym-2025", group="A", q="summarize the 2025 symposium",
         min_namespaces=2),                            # an explicit other year cancels 2026 scoping
    dict(id="sym-robots", group="A", q="Who is speaking at the symposium about robots?",
         scoped_to={"symposium"}),
    dict(id="sym-recording", group="A", q="Is there a recording of Helena Rong's talk at the symposium?",
         includes_type=["symposium_recording", "symposium_transcript"]),
    dict(id="sym-transcript", group="A", q="What did Venkat and Humboldt say about the trust ratchet at the symposium?",
         includes_type=["symposium_transcript"]),
    dict(id="sym-relational", group="A",
         q="How does the symposium's work on stigmergy relate to earlier discussions in the SIGs?",
         min_namespaces=2),                            # reaching for prior work keeps the whole archive

    # ── B. Other events ───────────────────────────────────────────────────────
    dict(id="lanna", group="B", q="What was Edge Lanna?", answer_has_any=["Chiang Mai", "Lanna"]),
    dict(id="symposium-2024", group="B", q="When was the 2024 symposium?",
         min_namespaces=2, answer_has_all=["2024", "September"]),
    dict(id="events-2025", group="B", q="What events did the Institute hold in 2025?",
         answer_has_any=["Khlongs", "Edge Esmeralda", "Devconnect", "Bridge Atlas", "Symposium"]),
    dict(id="coming-up", group="B", q="What is coming up at the Institute?",
         answer_has_any=["Book Writing Month"], until="2026-11-30"),

    # ── C. Calendar facts ─────────────────────────────────────────────────────
    dict(id="sigpsy-next", group="C", q="When is the next SIGPSY meeting?",
         answer_regex=DATE_RE, answer_has_any=["UTC"]),
    dict(id="mrg-meets", group="C", q="When does the Memory Research Group meet?",
         answer_has_any=["UTC"]),
    dict(id="newnature-next", group="C", q="When is the next New Nature live episode?",
         answer_has_any=["UTC"]),

    # ── D. Honesty ────────────────────────────────────────────────────────────
    dict(id="no-2027", group="D", q="When is the 2027 Protocol Symposium?",
         answer_lacks_regex=r"2027-\d\d-\d\d|" + DATE_RE + r",?\s+2027"),
    dict(id="no-edge-city", group="D", q="When is the next Edge City workshop?",
         answer_lacks_regex=r"(?:upcoming|next)[^.]{0,60}" + DATE_RE + r",?\s+202[7-9]"),
    dict(id="no-cta", group="D", q="When is the next SIGFPT meeting and how do I join?",
         answer_lacks=CTA),

    # ── E. Ordinary questions must be left alone ──────────────────────────────
    dict(id="stack", group="E", q="What is a protocol stack?",
         min_namespaces=2, answer_lacks=["KNOWN EVENTS"] + CTA),
    dict(id="hardness", group="E", q="What is the hardness of a protocol?",
         min_namespaces=2, answer_lacks=["KNOWN EVENTS"] + CTA),

    # ── F. Clock-dependent (Phase F: worker honours ?now= with the admin key) ──
    dict(id="live-today", group="F", q="What's on today?", now="2026-09-23T16:00:00Z",
         scoped_to={"symposium"}),                     # the session-55 behaviour, replayed
    dict(id="live-workshops", group="F", q="Which workshops are on right now?", now="2026-09-21T14:30:00Z",
         scoped_to={"symposium"}),
    dict(id="quiet-day", group="F", q="What's on today?", now="2026-10-10T10:00:00Z",
         answer_lacks=["running now"]),                # no significant event in a window (Oct 18 is the lead window of Book Writing Month)
    dict(id="lead-window", group="F", q="Is anything coming up soon?", now="2026-10-25T10:00:00Z",
         answer_has_any=["Book Writing Month"]),
    dict(id="live-bwm", group="F", q="What's on today?", now="2026-11-15T10:00:00Z",
         answer_has_any=["Book Writing Month"]),
    dict(id="sig-only-day", group="F", q="What's on today?", now="2026-10-22T10:00:00Z",
         answer_has_any=["SIGPSY", "Psychohistory"],              # today's SIG call is a fact worth stating
         answer_lacks=["Symposium is running", "running now"]),   # but a SIG call is not a significant event

    # ── G. Explicit time ranges (Phase E: ts_unix filters; replayed on a fixed day) ──
    dict(id="since-june", group="G", q="What has SIGPSY discussed since June?", now="2026-10-09T12:00:00Z",
         dates_within=["2026-06-01", "2026-10-09"], no_sources_from=["youtube", "web"]),
    dict(id="last-month", group="G", q="What happened in the community last month?", now="2026-10-09T12:00:00Z",
         dates_within=["2026-09-01", "2026-09-30"], no_sources_from=["youtube", "web"]),
    dict(id="in-2025", group="G", q="What did the Institute publish in 2025?", now="2026-10-09T12:00:00Z",
         dates_within=["2025-01-01", "2025-12-31"], no_sources_from=["youtube", "web"]),
    dict(id="in-march", group="G", q="What was discussed in March?", now="2026-10-09T12:00:00Z",
         dates_within=["2026-03-01", "2026-03-31"], no_sources_from=["youtube", "web"]),
]


# ── Worker call ───────────────────────────────────────────────────────────────

def ask(case: dict) -> dict:
    url = BASE + "/query"
    headers = {"Content-Type": "application/json", "User-Agent": "pibot-probe/1.0 (+https://github.com/Protocol-Institute/pibot)"}
    if os.environ.get("ADMIN_KEY"):                  # exempts the probe from the per-IP rate limit
        headers["X-Admin-Key"] = os.environ["ADMIN_KEY"]
    if case.get("now"):
        url += "?now=" + urllib.parse.quote(case["now"])
    body = {"query": case["q"], "context": "web"}
    if VARIANT:
        body["variant"] = VARIANT                     # admin-only answer-model A/B (api/worker.js AB_VARIANTS)
    if not needs_answer(case):
        body["mode"] = "sources"                      # retrieval only: same scoping, no model call
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {e.read().decode()[:160]}"
            if e.code not in (429, 500, 502, 503, 504):
                break
        except Exception as e:                       # network blip
            last = str(e)
        time.sleep(2 + attempt * 3)
    return {"error": last}


def needs_answer(case: dict) -> bool:
    return any(k.startswith("answer") for k in case)


PARALLEL = 6
VARIANT = None


# ── Checks ────────────────────────────────────────────────────────────────────

def evaluate(case: dict, resp: dict) -> list[str]:
    """Return a list of failure strings (empty = pass)."""
    if "error" in resp and "answer" not in resp:
        return [f"request failed: {resp['error']}"]
    answer  = (resp.get("answer") or "")
    low     = answer.lower()
    sources = resp.get("sources") or []
    spaces  = {s.get("source") for s in sources}
    types   = [s.get("type") or "" for s in sources]
    fails = []

    if "scoped_to" in case and not spaces <= case["scoped_to"]:
        fails.append(f"sources outside {sorted(case['scoped_to'])}: {sorted(spaces - case['scoped_to'])}")
    if "includes_type" in case and not any(w in t for w in case["includes_type"] for t in types):
        fails.append(f"no source of type {case['includes_type']} (got {sorted(set(types))})")
    if "not_source" in case and case["not_source"] in spaces:
        fails.append(f"retrieved from `{case['not_source']}`, which this question must not be scoped to")
    if "min_namespaces" in case and len(spaces) < case["min_namespaces"]:
        fails.append(f"only {len(spaces)} namespace(s) {sorted(spaces)}; expected the archive to stay in play")
    if "answer_has_any" in case and not any(x.lower() in low for x in case["answer_has_any"]):
        fails.append(f"answer has none of {case['answer_has_any']}")
    if "answer_has_all" in case:
        missing = [x for x in case["answer_has_all"] if x.lower() not in low]
        if missing:
            fails.append(f"answer is missing {missing}")
    if "dates_within" in case:
        lo, hi = case["dates_within"]
        def outside(d):
            if len(d) == 4:                            # Substack shows a year only; the filter used the full date
                return not lo[:4] <= d <= hi[:4]
            return not lo <= d <= hi
        out = sorted({(s.get("date") or "")[:10] for s in sources
                      if s.get("source") not in ("bibliography", "definition")    # timeless reference, kept on purpose
                      and (s.get("date") or "")[:10] and outside((s.get("date") or "")[:10])})
        if out:
            fails.append(f"sources dated outside {lo}..{hi}: {out[:6]}")
    for label in case.get("no_sources_from", []):
        if label in spaces:
            fails.append(f"undated source `{label}` should have been dropped while a time range is active")
    if "answer_regex" in case and not re.search(case["answer_regex"], answer, re.I):
        fails.append("answer has no date in the expected shape")
    for bad in case.get("answer_lacks", []):
        if bad.lower() in low:
            fails.append(f"answer contains forbidden phrase {bad!r}")
    if "answer_lacks_regex" in case:
        m = re.search(case["answer_lacks_regex"], answer, re.I)
        if m:
            fails.append(f"answer states something it should not know: {m.group(0)!r}")
    return fails


def expired(case: dict) -> bool:
    u = case.get("until")
    return bool(u) and datetime.now(timezone.utc).date().isoformat() > u


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", help="run one group letter (A-F)")
    ap.add_argument("--case", help="run one case id")
    ap.add_argument("--with-now", action="store_true", help="include groups F and G (need ?now= support and ADMIN_KEY)")
    ap.add_argument("--save", help="write answers + sources to this JSON file")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--variant", help="answer-model variant (A/B/C, admin only) for the answer cases")
    args = ap.parse_args()
    global VARIANT
    VARIANT = args.variant

    cases = CASES
    if args.case:
        cases = [c for c in CASES if c["id"] == args.case]
    elif args.group:
        cases = [c for c in CASES if c["group"] == args.group.upper()]
    elif not args.with_now:
        cases = [c for c in CASES if c["group"] not in ("F", "G")]
    if not cases:
        sys.exit("no matching cases")

    if args.list:
        for c in cases:
            print(f"{c['group']}  {c['id']:<16} {c['q']}")
        return
    if not os.environ.get("ADMIN_KEY"):
        print("note: no ADMIN_KEY in .env, so the worker's 20/hour per-IP limit applies (~19 cases)")

    saved, passed, failed, skipped = [], 0, 0, 0
    live = [c for c in cases if not expired(c)]
    with ThreadPoolExecutor(PARALLEL) as pool:
        responses = dict(zip((c["id"] for c in live), pool.map(ask, live)))
    for c in cases:
        if expired(c):
            print(f"SKIP  {c['id']:<16} (time-bound, expired {c['until']})")
            skipped += 1
            continue
        resp = responses[c["id"]]
        fails = evaluate(c, resp)
        spaces = sorted({s.get("source") for s in resp.get("sources") or []})
        if fails:
            failed += 1
            print(f"FAIL  {c['id']:<16} {c['q']}")
            for f in fails:
                print(f"        - {f}")
        else:
            passed += 1
            print(f"ok    {c['id']:<16} {spaces}")
        saved.append({"id": c["id"], "q": c["q"], "ok": not fails, "fails": fails,
                      "answer": resp.get("answer", ""),
                      "sources": [{k: s.get(k) for k in ("source", "type", "title", "url", "date")}
                                  for s in resp.get("sources") or []]})

    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
    if args.save:
        Path(args.save).write_text(json.dumps(saved, indent=1))
        print(f"saved -> {args.save}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
