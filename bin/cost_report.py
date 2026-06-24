#!/usr/bin/env python3
"""
Report cumulative Anthropic API costs from data/cost_log.jsonl.

Usage:
    python3 bin/cost_report.py              # last 7 days + all-time
    python3 bin/cost_report.py --days 30    # custom window
    python3 bin/cost_report.py --all        # all-time only
"""

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

COST_LOG = Path(__file__).parent.parent / "data" / "cost_log.jsonl"


def load_records(since: datetime | None = None) -> list[dict]:
    if not COST_LOG.exists():
        return []
    records = []
    with COST_LOG.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                if since:
                    ts = datetime.fromisoformat(r["ts"].replace("Z", "+00:00"))
                    if ts < since:
                        continue
                records.append(r)
            except Exception:
                pass
    return records


def summarize(records: list[dict]) -> dict:
    total_cost = 0.0
    total_input = total_output = 0
    by_script: dict = defaultdict(lambda: {"cost_usd": 0.0, "calls": 0, "input_tokens": 0, "output_tokens": 0})

    for r in records:
        cost = r.get("cost_usd", 0.0)
        total_cost += cost
        total_input  += r.get("input_tokens", 0)
        total_output += r.get("output_tokens", 0)
        s = by_script[r.get("script", "unknown")]
        s["cost_usd"]      += cost
        s["calls"]         += 1
        s["input_tokens"]  += r.get("input_tokens", 0)
        s["output_tokens"] += r.get("output_tokens", 0)

    return {
        "total_cost_usd":      round(total_cost, 6),
        "total_calls":         len(records),
        "total_input_tokens":  total_input,
        "total_output_tokens": total_output,
        "by_script": {
            k: {**v, "cost_usd": round(v["cost_usd"], 6)}
            for k, v in sorted(by_script.items(), key=lambda x: -x[1]["cost_usd"])
        },
    }


def print_report(label: str, s: dict) -> None:
    print(f"\n── {label} {'─' * max(0, 46 - len(label))}")
    print(f"  Total cost    : ${s['total_cost_usd']:.4f}")
    print(f"  API calls     : {s['total_calls']:,}")
    print(f"  Input tokens  : {s['total_input_tokens']:,}")
    print(f"  Output tokens : {s['total_output_tokens']:,}")
    if s["by_script"]:
        print(f"  By script:")
        for script, d in s["by_script"].items():
            print(f"    {script:<32}  ${d['cost_usd']:.4f}  ({d['calls']} calls)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7, help="Recent window in days (default: 7)")
    parser.add_argument("--all",  action="store_true", help="Show all-time totals only")
    args = parser.parse_args()

    if not COST_LOG.exists():
        print("No cost log yet — data/cost_log.jsonl not found.")
        print("Tracking begins automatically on the next daemon cycle after this session.")
        return

    now = datetime.now(timezone.utc)

    if not args.all:
        since = now - timedelta(days=args.days)
        print_report(f"Last {args.days} days", summarize(load_records(since=since)))

    all_records = load_records()
    print_report("All-time", summarize(all_records))

    if all_records:
        print(f"\n  Tracking since: {all_records[0].get('ts', '?')}")
    print()


if __name__ == "__main__":
    main()
