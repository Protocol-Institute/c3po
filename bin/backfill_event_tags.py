"""
Backfill ts_unix and event_id on vectors already in the index
(plans/event-awareness.md, Phase B). New vectors are tagged at ingest by
ingest/utils._GuardedIndex.upsert; this covers what was there before.

No re-embedding, and almost no reads:

  event_id   by metadata *filter*: one update call per event, no fetch at all
             (symposium, the 2024 symposium recordings, each SIG's chunks).
  ts_unix    three classes of vector, cheapest first:
             - ids that carry a Discord snowflake (sig_msg, sig_discussion,
               discord*): time computed from the id alone. `list()` returns ids
               only, so this costs almost nothing.
             - everything else with a date in its metadata (substack, pdfs, meta,
               transcripts, symposium, SIG meeting chunks): fetched in batches of
               100 (a fetch returns the embedding too, ~9KB a vector, so about
               35MB for ~4,000 vectors) and tagged from metadata.
             - no usable date, left alone: videos (flat-playlist upload dates are
               "NA"), definitions, bibliography, discord_guide, and discord_links
               (hashed ids, only a fetch_date).

Idempotent: setting the same value again changes nothing, so a re-run after an
interruption simply repeats some work. Honours the ingestion pause (a quota 429
auto-pauses and the script stops).

Usage:
    python3 bin/backfill_event_tags.py --dry-run          # counts, writes nothing
    python3 bin/backfill_event_tags.py                    # everything
    python3 bin/backfill_event_tags.py --only events      # event_id by filter only
    python3 bin/backfill_event_tags.py --only ts --namespace sig
"""

import argparse
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "ingest"))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

import event_tags
from utils import get_pinecone_index, IngestionPaused

SIG_DISPLAYS = ["SIGFPT", "MRG", "SIGPfB", "ProtFiSIG", "Affiliates", "SIGPSY", "DRG", "PRG",
                "Intelligence Media"]

SNOWFLAKE_NAMESPACES = ("sig", "discord")                 # ids that encode their own time
FETCH_NAMESPACES     = ("substack", "pdfs", "meta", "transcripts", "symposium", "sig")
THREADS = 8
FETCH_BATCH = 100


def ids_of(idx, namespace):
    for page in idx.list(namespace=namespace):
        for item in page:
            yield getattr(item, "id", item)


def update_with_retry(idx, **kw):
    for attempt in range(4):
        try:
            return idx.update(**kw)
        except IngestionPaused:
            raise
        except Exception as e:
            if attempt == 3 or "429" not in str(e) and "timeout" not in str(e).lower():
                raise
            time.sleep(1.5 * (attempt + 1))


# ── event_id by filter ────────────────────────────────────────────────────────

def event_filter_jobs(cfg):
    """(namespace, filter, event_id) triples, one per deterministic event rule."""
    jobs = []
    for ns, ev in cfg["namespace_events"].items():
        jobs.append((ns, {"chunk_type": {"$exists": True}}, ev))
    for series, ev in cfg["series_events"].items():
        jobs.append(("videos", {"series": {"$eq": series}}, ev))
    if cfg.get("sig_events", True):
        for sig in SIG_DISPLAYS:
            jobs.append(("sig", {"sig_display": {"$eq": sig}}, f"sig:{sig}"))
    return jobs


def backfill_events(idx, dry_run):
    cfg = event_tags.load_config()
    total = 0
    for ns, flt, ev in event_filter_jobs(cfg):
        r = update_with_retry(idx, filter=flt, set_metadata={"event_id": [ev]}, namespace=ns, dry_run=True)
        matched = getattr(r, "matched_records", None)
        print(f"  event_id {ev:<34} {ns:<9} matches {matched}")
        if matched and not dry_run:
            update_with_retry(idx, filter=flt, set_metadata={"event_id": [ev]}, namespace=ns)
        total += matched or 0
    print(f"event_id: {total} vectors {'would be ' if dry_run else ''}tagged")


# ── ts_unix ───────────────────────────────────────────────────────────────────

def backfill_snowflake(idx, namespace, dry_run):
    """ts_unix from the vector id alone (no fetch)."""
    todo = []
    for vid in ids_of(idx, namespace):
        t = event_tags.snowflake_to_unix(vid)
        if t:
            todo.append((vid, t))
    print(f"  {namespace}: {len(todo)} snowflake ids")
    if dry_run or not todo:
        return len(todo)
    done = 0

    def one(item):
        vid, t = item
        update_with_retry(idx, id=vid, set_metadata={"ts_unix": t}, namespace=namespace)

    with ThreadPoolExecutor(THREADS) as pool:
        for i, _ in enumerate(pool.map(one, todo), 1):
            done = i
            if i % 2000 == 0:
                print(f"    {namespace}: {i}/{len(todo)}")
    return done


def backfill_fetched(idx, namespace, dry_run):
    """ts_unix for ids without a snowflake, from the vector's own metadata."""
    cfg = event_tags.load_config()
    ids = [v for v in ids_of(idx, namespace) if not event_tags.snowflake_to_unix(v)]
    print(f"  {namespace}: {len(ids)} non-snowflake ids to read")
    planned, missing = [], Counter()
    for i in range(0, len(ids), FETCH_BATCH):
        batch = ids[i:i + FETCH_BATCH]
        got = idx.fetch(ids=batch, namespace=namespace).vectors
        for vid, vec in got.items():
            md = dict(vec.metadata or {})
            t = event_tags.vector_ts(vid, md)
            patch = {}
            if t:
                patch["ts_unix"] = t
            else:
                missing[md.get("chunk_type", "?")] += 1
            ev = event_tags.event_ids(namespace, md, cfg)
            if ev and "event_id" not in md:
                patch["event_id"] = ev
            if patch:
                planned.append((vid, patch))
    print(f"    {len(planned)} to tag; no usable date: {dict(missing) or 0}")
    if dry_run or not planned:
        return len(planned)

    def one(item):
        vid, patch = item
        update_with_retry(idx, id=vid, set_metadata=patch, namespace=namespace)

    with ThreadPoolExecutor(THREADS) as pool:
        list(pool.map(one, planned))
    return len(planned)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", choices=["events", "ts"], help="run just one half")
    ap.add_argument("--namespace", help="limit ts backfill to one namespace")
    args = ap.parse_args()

    idx = get_pinecone_index()
    try:
        if args.only != "ts":
            print("== event_id (by filter, no reads) ==")
            backfill_events(idx, args.dry_run)
        if args.only != "events":
            print("== ts_unix ==")
            total = 0
            for ns in SNOWFLAKE_NAMESPACES:
                if args.namespace and args.namespace != ns:
                    continue
                total += backfill_snowflake(idx, ns, args.dry_run)
            for ns in FETCH_NAMESPACES:
                if args.namespace and args.namespace != ns:
                    continue
                total += backfill_fetched(idx, ns, args.dry_run)
            print(f"ts_unix: {total} vectors {'would be ' if args.dry_run else ''}tagged")
    except IngestionPaused as e:
        sys.exit(f"stopped: {e}")


if __name__ == "__main__":
    main()
