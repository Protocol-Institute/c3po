#!/usr/bin/env python3
"""
backfill_key_points.py — re-parse audio key points for meetings already on disk.

Issue #7: sync_meeting_notes kept only top-level bullets, so points whose
content sat in sub-bullets were stored as bare headings. The parser is fixed
(utils.parse_key_points); this rewrites the stored meetings from their source
summaries so the archive pages regenerate clean. Run where data/sigs lives (the
VM). Idempotent.

    python3 bin/backfill_key_points.py --dry-run
    python3 bin/backfill_key_points.py
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ingest"))
from utils import parse_key_points                                  # noqa: E402
from sync_meeting_notes import fetch_r2_summary, parse_summary_md   # noqa: E402

MEETINGS_DIR = ROOT / "data" / "sigs" / "meetings"


def main(dry_run: bool):
    changed = unchanged = failed = 0
    for path in sorted(MEETINGS_DIR.glob("*.json")):
        d = json.loads(path.read_text())
        url = d.get("audio_r2_summary_url")
        if not url or "audio_key_points" not in d:
            continue
        md = fetch_r2_summary(url)
        if not md:
            print(f"  FAIL  {path.name}: could not fetch summary")
            failed += 1
            continue
        new = parse_key_points(parse_summary_md(md)["sections"].get("key_points", ""))
        old = d["audio_key_points"]
        if new == old:
            unchanged += 1
            continue
        print(f"  FIX   {path.name}  {d.get('sig')} {d.get('date')}: "
              f"{sum(1 for p in old if p.rstrip(':*').count('**') and len(p) < 80)} bare -> "
              f"{len(new)} points")
        d["audio_key_points"] = new
        # Meetings created from audio use the same list as their insights;
        # Discord-derived insights on enriched meetings are a different list.
        if d.get("key_insights") == old:
            d["key_insights"] = new
        if not dry_run:
            path.write_text(json.dumps(d, indent=2, ensure_ascii=False))
        changed += 1
    print(f"\n{'Would fix' if dry_run else 'Fixed'} {changed}   unchanged {unchanged}   failed {failed}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    main(ap.parse_args().dry_run)
