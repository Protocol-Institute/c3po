"""Manually inspect, pause, or resume Pinecone ingestion.

Ingestion also pauses itself automatically the moment any script's write to
Pinecone comes back with a write/read-unit 429 (see the guarded index in
utils.py) — this CLI is for a deliberate manual pause/resume (e.g. "we know
the quota's tight, don't even try until it resets") or for checking status.

Usage:
    python3 ingest/ingestion_control.py status
    python3 ingest/ingestion_control.py pause --until 2026-08-01T00:00:00Z --reason "..."
    python3 ingest/ingestion_control.py resume
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from utils import pause_ingestion, resume_ingestion, pause_status, PAUSE_STATE_PATH


def cmd_status() -> None:
    any_active = False
    for kind in ("write", "read"):
        state = pause_status(kind)
        if state is None:
            print(f"{kind}: not paused")
            continue
        any_active = True
        print(f"{kind}: PAUSED — {state['reason']}")
        print(f"    paused_at: {state['paused_at']}")
        print(f"    resume_at: {state['resume_at']}")
    if not any_active and PAUSE_STATE_PATH.exists():
        print(f"(previous pause state retained at {PAUSE_STATE_PATH} for reference)")


def cmd_pause(until: str, reason: str, kind: str) -> None:
    # Validate the timestamp early rather than writing a bad state file.
    datetime.fromisoformat(until.replace("Z", "+00:00"))
    state = pause_ingestion(reason, until, kind=kind)
    print(f"Paused ({state['kind']}) until {state['resume_at']} — {state['reason']}")


def cmd_resume(kind: str | None) -> None:
    resume_ingestion(kind)
    print(f"Resumed ({kind or 'write + read'}) — pause state cleared.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status")

    p_pause = sub.add_parser("pause")
    p_pause.add_argument("--until", required=True, help="ISO 8601 UTC timestamp, e.g. 2026-08-01T00:00:00Z")
    p_pause.add_argument("--reason", required=True)
    p_pause.add_argument("--kind", choices=["write", "read"], default="write")

    p_resume = sub.add_parser("resume")
    p_resume.add_argument("--kind", choices=["write", "read"], default=None,
                           help="Resume only this kind; omit to resume both")

    args = parser.parse_args()

    if args.command == "status":
        cmd_status()
    elif args.command == "pause":
        cmd_pause(args.until, args.reason, args.kind)
    elif args.command == "resume":
        cmd_resume(args.kind)


if __name__ == "__main__":
    main()
