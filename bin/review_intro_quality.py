#!/usr/bin/env python3
"""
Print unreviewed intro quality issues for session-start review.

Usage:
    python3 bin/review_intro_quality.py              # show unreviewed issues
    python3 bin/review_intro_quality.py --all        # show all (including reviewed)
    python3 bin/review_intro_quality.py --mark-reviewed  # show then mark as reviewed
"""
import json
import sys
from pathlib import Path

QUALITY_LOG_PATH = Path(__file__).parent.parent / "data" / "intro_quality_log.jsonl"

SEV_SYMBOL = {"warning": "⚠", "fixed": "✓", "info": "ℹ"}
SEV_ORDER  = {"warning": 0, "fixed": 1, "info": 2}


def main():
    show_all      = "--all" in sys.argv
    mark_reviewed = "--mark-reviewed" in sys.argv

    if not QUALITY_LOG_PATH.exists():
        print("No intro quality log found.")
        return

    entries = []
    with QUALITY_LOG_PATH.open() as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    targets = entries if show_all else [e for e in entries if not e.get("reviewed")]

    if not targets:
        print("No unreviewed intro quality issues." if not show_all else "Quality log is empty.")
        return

    label = "all" if show_all else "unreviewed"
    print(f"=== {len(targets)} {label} intro quality issue(s) ===\n")

    # Group by issue code for summary
    code_counts: dict[str, int] = {}
    for e in targets:
        for i in e.get("issues", []):
            code_counts[i["code"]] = code_counts.get(i["code"], 0) + 1

    print("Summary:")
    for code, count in sorted(code_counts.items(), key=lambda x: x[1], reverse=True):
        print(f"  {code}: {count}×")
    print()

    # Detail per entry
    for e in targets:
        match_sym = "✓" if e.get("title_matched") else "✗"
        rev_sym   = " [reviewed]" if e.get("reviewed") else ""
        print(f"[{e['ts'][:10]}] {e['user_hash'][:8]} | match={match_sym} | links={e['rec_count']}{rev_sym}")
        print(f"  intro:   {e['intro_snippet']!r}")
        print(f"  primary: {e['primary_title']!r}")
        for i in sorted(e.get("issues", []), key=lambda x: SEV_ORDER.get(x["severity"], 9)):
            sym = SEV_SYMBOL.get(i["severity"], "?")
            print(f"  {sym} [{i['severity']}] {i['code']}: {i['detail']}")
        print()

    if mark_reviewed and not show_all:
        for e in entries:
            if not e.get("reviewed"):
                e["reviewed"] = True
        with QUALITY_LOG_PATH.open("w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
        print(f"Marked {len(targets)} entry/entries as reviewed.")


if __name__ == "__main__":
    main()
