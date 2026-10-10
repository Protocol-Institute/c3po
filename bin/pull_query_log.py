"""
Copy the worker's query log (KV `log:*`) to a local JSONL file, for the reranker's
Phase 3 calibration (plans/reranker.md): a min-score floor, the pool width, and
whether to add an instruction.

The log lives 90 days in KV; run this before then (it is idempotent: entries already
in the file are skipped, keyed on timestamp + query). Session ids are dropped on the
way in: the analysis needs the questions and the scores, not who asked.

Output: data/query_log.jsonl (gitignored, like everything under data/; the repo is
public and these are people's questions).

Each entry: ts, query, context (web/discord), turnNumber, answer (first 1,200 chars),
sources (the cut, with rerank_score), and, for entries logged since 2026-10-10,
pool: {status, budget, items: [{source, title, url, cos, w, rr, rank, kept}]}.

Usage:
    python3 bin/pull_query_log.py            # pull everything new
    python3 bin/pull_query_log.py --stats    # pull, then summarise the score distribution
"""

import argparse
import json
import os
import statistics
import urllib.parse
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

BASE = os.environ.get("PROBE_BASE", "https://pibot.protocolized.io")
OUT = ROOT / "data" / "query_log.jsonl"
DROP = {"sessionId"}

# Test traffic that reached the log before admin requests were excluded (worker
# f5220187, 2026-10-09), plus non-admin checks run by hand while building this.
# Marked `test: true`, never deleted, so the calibration can leave them out.
TEST_CUTOFF = "2026-10-10T01:30:00"
ADHOC_TESTS = {
    "What is stigmergy?", "What is protocol atrophy?", "What is protocol atrophy? Two sentences.",
    "Define protocol hardness in one sentence.", "Define protocol atrophy in one sentence.",
    "Give one concrete example of it, in two sentences.", "What is PIBot?", "What is C3PO?",
    "What were you called before PIBot?", "Why was C3PO renamed?", "When did you become PIBot?",
    "Tell me about your own history.", "What were you called before PIBot, and why did that change?",
    "Where did your name come from?", "What has SIGPSY been discussing lately?",
    "PIBot logging check (please ignore)",
}


def test_queries():
    """Every question the probe and the A/B harness send, plus the hand-run checks."""
    import importlib.util
    qs = set(ADHOC_TESTS)
    for name in ("probe_event_scope", "ab_model"):
        spec = importlib.util.spec_from_file_location(name, ROOT / "bin" / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        qs |= {c["q"] for c in getattr(mod, "CASES", [])}
        qs |= set(getattr(mod, "QUESTIONS", []))
    return qs


def mark_tests(rows, tests):
    n = 0
    for r in rows:
        if r.get("ts", "") < TEST_CUTOFF and r.get("query") in tests and not r.get("test"):
            r["test"] = True
            n += 1
    return n


def fetch_page(cursor=None):
    q = {"limit": "100"}
    if cursor:
        q["cursor"] = cursor
    req = urllib.request.Request(f"{BASE}/admin/transcripts?{urllib.parse.urlencode(q)}", headers={
        "X-Admin-Key": os.environ["ADMIN_KEY"],
        "User-Agent": "pibot-pull/1.0 (+https://github.com/Protocol-Institute/pibot)",
    })
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def key(e):
    return f"{e.get('ts')}|{e.get('query')}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()

    seen = set()
    if OUT.exists():
        for line in OUT.read_text().splitlines():
            if line.strip():
                seen.add(key(json.loads(line)))

    new, cursor, pages = [], None, 0
    while True:
        page = fetch_page(cursor)
        pages += 1
        for e in page.get("items", []):
            if key(e) in seen:
                continue
            seen.add(key(e))
            new.append({k: v for k, v in e.items() if k not in DROP})
        cursor = page.get("cursor")
        if not cursor:
            break

    new.sort(key=lambda e: e.get("ts", ""))
    rows = [json.loads(l) for l in OUT.read_text().splitlines() if l.strip()] if OUT.exists() else []
    rows += new
    marked = mark_tests(rows, test_queries())
    OUT.write_text("".join(json.dumps(r) + "\n" for r in sorted(rows, key=lambda e: e.get("ts", ""))))
    tests = sum(1 for r in rows if r.get("test"))
    print(f"{len(new)} new entries ({pages} page(s)); {len(rows)} in {OUT.relative_to(ROOT)}, "
          f"{tests} marked test ({marked} newly)")

    if args.stats:
        rows = [r for r in rows if not r.get("test")]
        with_pool = [r for r in rows if r.get("pool")]
        print(f"entries with the full pool: {len(with_pool)} of {len(rows)}")
        kept = [i["rr"] for r in with_pool for i in r["pool"]["items"] if i.get("kept") and i.get("rr") is not None]
        cut = [i["rr"] for r in with_pool for i in r["pool"]["items"] if not i.get("kept") and i.get("rr") is not None]
        for name, xs in (("kept", kept), ("cut", cut)):
            if len(xs) >= 2:
                qs = statistics.quantiles(xs, n=10)
                print(f"  rerank score, {name:4} (n={len(xs)}): p10 {qs[0]:.3f}  median {statistics.median(xs):.3f}  p90 {qs[-1]:.3f}")


if __name__ == "__main__":
    main()
