#!/usr/bin/env python3
"""
Roll older devlog entries out of the live page into a new archive page.

`data/devlog.json` is the page a session appends to; it stays small. Entries
rolled out of it land in `data/devlog_archive_NNN.json`. Nothing downstream
changes: `ingest/devlog_store.load_devlog()` merges every page, so the Pinecone
`meta` ingest, `DEVLOG.md` and the published page on protocolized.io all still
see one continuous log with the same `#session-{id}` anchors.

Usage:
    python3 bin/devlog_roll.py --keep 0            # archive everything live now
    python3 bin/devlog_roll.py --keep 10           # keep the 10 newest live
    python3 bin/devlog_roll.py --through-id 54     # archive ids up to 54
    python3 bin/devlog_roll.py --keep 10 --dry-run

The merged log is compared before and after the roll and the script aborts if
they differ, so a roll can never drop or reorder a session.
"""

import argparse
import copy
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "ingest"))
from devlog_store import (  # noqa: E402
    METADATA_KEYS,
    load_devlog,
    load_live,
    next_archive_path,
    save_archive,
    save_live,
)


def split_sessions(sessions: list[dict], keep: int | None,
                   through_id: int | None) -> tuple[list[dict], list[dict]]:
    """Return (to_archive, to_keep), both in sort_key order."""
    ordered = sorted(sessions, key=lambda s: s["sort_key"])

    if through_id is not None:
        to_archive = [s for s in ordered if int(s.get("id", 0)) <= through_id]
        to_keep    = [s for s in ordered if int(s.get("id", 0)) > through_id]
        return to_archive, to_keep

    if keep == 0:
        return ordered, []
    return ordered[:-keep], ordered[-keep:]


def describe(sessions: list[dict]) -> str:
    if not sessions:
        return "(none)"
    first, last = sessions[0], sessions[-1]
    return (f"{first.get('label') or first['id']} ({first.get('date')}) → "
            f"{last.get('label') or last['id']} ({last.get('date')})")


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--keep", type=int,
                       help="Number of newest entries to leave in the live page")
    group.add_argument("--through-id", type=int,
                       help="Archive every entry with id <= this")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.keep is not None and args.keep < 0:
        print("ERROR: --keep must be >= 0")
        return 1

    before   = load_devlog()
    live     = load_live()
    to_archive, to_keep = split_sessions(live.get("sessions", []),
                                         args.keep, args.through_id)

    if not to_archive:
        print("Nothing to roll — the live page holds no entries older than the cut.")
        return 0

    archive_path = next_archive_path()
    archive_doc  = {k: live[k] for k in METADATA_KEYS if k in live}
    archive_doc["archive_page"] = archive_path.stem
    archive_doc["archived_on"]  = date.today().isoformat()
    archive_doc["covers"] = {
        "first_id":   int(to_archive[0].get("id", 0)),
        "last_id":    int(to_archive[-1].get("id", 0)),
        "first_date": to_archive[0].get("date"),
        "last_date":  to_archive[-1].get("date"),
        "entries":    len(to_archive),
    }
    archive_doc["sessions"] = to_archive

    new_live = copy.deepcopy(live)
    new_live["sessions"] = to_keep

    print(f"Archive page : {archive_path.name}")
    print(f"  archiving  : {len(to_archive)} entries — {describe(to_archive)}")
    print(f"  live keeps : {len(to_keep)} entries — {describe(to_keep)}")

    # A roll must be invisible downstream: the merged log has to come out
    # byte-identical. Check before writing anything.
    after_sessions = sorted(to_archive + to_keep, key=lambda s: s["sort_key"])
    if json.dumps(after_sessions, sort_keys=True) != \
       json.dumps(before["sessions"], sort_keys=True):
        print("ERROR: merged log would change — refusing to roll.")
        return 1

    if args.dry_run:
        print(f"[dry-run] would write {archive_path.name} "
              f"and rewrite data/devlog.json")
        return 0

    save_archive(archive_path, archive_doc)
    save_live(new_live)

    # Re-read from disk and confirm the merge still matches.
    reloaded = load_devlog()
    if json.dumps(reloaded["sessions"], sort_keys=True) != \
       json.dumps(before["sessions"], sort_keys=True):
        print("ERROR: post-roll merge does not match the pre-roll log. "
              "Restore data/devlog.json from git before continuing.")
        return 1

    print(f"✓ Rolled. Merged log still reads {len(reloaded['sessions'])} entries.")
    print(f"  Commit data/{archive_path.name} alongside data/devlog.json — "
          f"the archive page is the only copy of those entries.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
