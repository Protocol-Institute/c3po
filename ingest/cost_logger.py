"""
Shared cost logging utility for C3PO ingest scripts.

Appends one JSON line per Claude API call to data/cost_log.jsonl.
Call log_api_call() after every client.messages.create() to track spend.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

COST_LOG = Path(__file__).parent.parent / "data" / "cost_log.jsonl"

# USD per 1M tokens
_PRICING: dict[str, dict[str, float]] = {
    "claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00},
    "claude-haiku-4-5":          {"input": 1.00, "output": 5.00},
    "claude-sonnet-4-6":         {"input": 3.00, "output": 15.00},
    "claude-opus-4-8":           {"input": 5.00, "output": 25.00},
}
_DEFAULT_RATES = {"input": 3.00, "output": 15.00}


class BudgetExceeded(Exception):
    """Raised when today's Anthropic spend has reached the configured daily cap."""
    pass


_DEFAULT_DAILY_LIMIT_USD = 5.00  # runaway-loop backstop, not a routine constraint —
                                  # current all-time average is ~$0.10/day (session 46)


def today_usd() -> float:
    """Sum cost_log.jsonl entries for today (local calendar date)."""
    if not COST_LOG.exists():
        return 0.0
    today_local = datetime.now().strftime("%Y-%m-%d")
    total = 0.0
    with COST_LOG.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                # ts is stored UTC; convert to local date for midnight-reset semantics
                ts_local = datetime.strptime(r["ts"], "%Y-%m-%dT%H:%M:%SZ") \
                    .replace(tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%d")
                if ts_local == today_local:
                    total += r.get("cost_usd", 0.0)
            except Exception:
                pass
    return round(total, 6)


def check_budget(limit_usd: float = _DEFAULT_DAILY_LIMIT_USD) -> None:
    """Raise BudgetExceeded if today's spend has reached limit_usd. Call before
    every Claude API invocation."""
    spent = today_usd()
    if spent >= limit_usd:
        raise BudgetExceeded(f"${spent:.4f} spent today (limit ${limit_usd:.2f})")


def log_api_call(script: str, model: str, usage) -> float:
    """Append one cost record to data/cost_log.jsonl. Returns cost in USD."""
    rates = _PRICING.get(model, _DEFAULT_RATES)
    input_tok  = getattr(usage, "input_tokens",  0) or 0
    output_tok = getattr(usage, "output_tokens", 0) or 0
    cost = (input_tok / 1_000_000 * rates["input"]) + (output_tok / 1_000_000 * rates["output"])

    record = {
        "ts":            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "script":        script,
        "model":         model,
        "input_tokens":  input_tok,
        "output_tokens": output_tok,
        "cost_usd":      round(cost, 8),
    }

    with COST_LOG.open("a") as f:
        f.write(json.dumps(record) + "\n")

    return cost
