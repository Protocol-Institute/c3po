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
