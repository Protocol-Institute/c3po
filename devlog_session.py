#!/usr/bin/env python3
"""
devlog_session.py — timestamp helper for C3PO dev-log session entries.

Usage:
    python3 devlog_session.py start              # record session start time now
    python3 devlog_session.py end                # print devlog-ts element (start → now)
    python3 devlog_session.py stamp DATE [START [END]]
    python3 devlog_session.py now                # print current PT time
    python3 devlog_session.py costs              # print cost summary template

Output of 'end':
    2026-05-14 · 09:00–13:30 PT

Workflow:
    1. At session start: python3 devlog_session.py start
    2. During session:   build and test as usual
    3. At session end:   python3 devlog_session.py end
    4. Copy the output date into the new session entry in data/devlog.json
"""
import sys
import os
from datetime import datetime

try:
    import zoneinfo
    TZ = zoneinfo.ZoneInfo("America/Los_Angeles")
except ImportError:
    from datetime import timezone, timedelta
    TZ = timezone(timedelta(hours=-7))

START_FILE = "/tmp/c3po_devlog_session_start.txt"


def now_pt():
    return datetime.now(tz=TZ)


def fmt_time(dt):
    return dt.strftime("%H:%M")


def fmt_date(dt):
    return dt.strftime("%Y-%m-%d")


def read_start():
    if os.path.exists(START_FILE):
        with open(START_FILE) as f:
            raw = f.read().strip()
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None
    return None


def write_start(dt):
    with open(START_FILE, "w") as f:
        f.write(dt.isoformat())


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "start":
        now = now_pt()
        write_start(now)
        print(f"Session start recorded: {fmt_date(now)} {fmt_time(now)} PT")
        print(f"Saved to {START_FILE}")

    elif cmd == "end":
        now = now_pt()
        start = read_start()
        if start:
            date_str = fmt_date(start)
            start_str = fmt_time(start)
        else:
            print("Warning: no start time found. Run 'start' at the beginning of each session.", file=sys.stderr)
            date_str = fmt_date(now)
            start_str = None
        end_str = fmt_time(now)
        if start_str:
            print(f"{date_str} · {start_str}–{end_str} PT")
        else:
            print(f"{date_str} · {end_str} PT")

    elif cmd == "stamp":
        date_str = sys.argv[2] if len(sys.argv) > 2 else fmt_date(now_pt())
        start_str = sys.argv[3] if len(sys.argv) > 3 else None
        end_str = sys.argv[4] if len(sys.argv) > 4 else None
        if start_str and end_str:
            print(f"{date_str} · {start_str}–{end_str} PT")
        elif start_str:
            print(f"{date_str} · {start_str} PT")
        else:
            print(date_str)

    elif cmd == "now":
        now = now_pt()
        print(f"{fmt_date(now)} {fmt_time(now)} PT")

    elif cmd == "costs":
        print("Cost template for data/devlog.json session entry:")
        print("""  "costs_usd": {
    "haiku_enrichment": 0.00,
    "voyage_embedding": 0.00,
    "claude_sonnet": 0.00
  },
  "vector_counts": {
    "substack": 0,
    "pdfs": 0
  }""")

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
