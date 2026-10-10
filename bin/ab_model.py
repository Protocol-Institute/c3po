"""
A/B the answer model through the live worker (admin-only `variant`, see AB_VARIANTS
in api/worker.js). Same retrieval, same prompt, same question; only the model and
its thinking settings change.

    A  claude-sonnet-4-6   (production)
    B  claude-sonnet-5-5   thinking off ("between_tools")
    C  claude-sonnet-5-5   adaptive thinking, effort low

Records wall time, output length, token usage and cost at each model's own prices,
and saves every answer so they can be read side by side. Admin requests are kept
out of the query log, so this does not touch the reranker calibration data.

Usage:
    python3 bin/ab_model.py                      # all variants, all questions
    python3 bin/ab_model.py --variants A,B
    python3 bin/ab_model.py --out ab_results.json
"""

import argparse
import json
import os
import statistics
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

BASE = os.environ.get("PROBE_BASE", "https://pibot.protocolized.io")

# $ per million tokens: input, cache write (5m), cache read, output
PRICES = {
    "claude-sonnet-4-6": (3.00, 3.75, 0.30, 15.00),
    "claude-sonnet-5-5": (2.00, 2.50, 0.20, 10.00),
}

QUESTIONS = [
    "What is protocol atrophy?",
    "What is the hardness of a protocol, and why does it matter?",
    "How do protocols relate to institutions and bureaucracy?",
    "What has the Memory Research Group been exploring?",
    "What did Venkat and Humboldt say about the trust ratchet at the symposium?",
    "Recommend some Protocolized fiction about artificial intelligence.",
    "How do protocols die or get replaced?",
    "What is stigmergy and how does the Institute study it?",
    "Compare protocols and standards. Where do they differ?",
    "I'm new here. Where should I start reading?",
]


def ask(question: str, variant: str) -> dict:
    body = json.dumps({"query": question, "context": "web", "variant": variant}).encode()
    req = urllib.request.Request(BASE + "/query", data=body, headers={
        "Content-Type": "application/json",
        "User-Agent": "pibot-ab/1.0 (+https://github.com/Protocol-Institute/pibot)",
        "X-Admin-Key": os.environ["ADMIN_KEY"],
    })
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        return {"q": question, "variant": variant, "error": f"HTTP {e.code}: {e.read().decode()[:200]}"}
    secs = time.time() - t0
    u = d.get("_usage") or {}
    model = d.get("_model") or ""
    pin, pw, pr, pout = PRICES.get(model, PRICES["claude-sonnet-4-6"])
    cost = (u.get("input_tokens", 0) * pin + u.get("cache_creation_input_tokens", 0) * pw
            + u.get("cache_read_input_tokens", 0) * pr + u.get("output_tokens", 0) * pout) / 1e6
    answer = d.get("answer") or ""
    return {"q": question, "variant": variant, "model": model, "secs": round(secs, 1),
            "words": len(answer.split()), "out_tok": u.get("output_tokens", 0),
            "thinking_tok": (u.get("output_tokens_details") or {}).get("thinking_tokens", 0),
            "cost": round(cost, 5), "answer": answer}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--variants", default="A,B,C")
    ap.add_argument("--out", default=str(Path(__file__).parent.parent / "data" / "ab_model_results.json"))
    args = ap.parse_args()
    variants = args.variants.split(",")

    # One question at a time, its variants in parallel, so each comparison runs
    # under the same load conditions.
    rows = []
    with ThreadPoolExecutor(len(variants)) as pool:
        for q in QUESTIONS:
            got = list(pool.map(lambda v: ask(q, v), variants))
            for g in got:
                rows.append(g)
                if "error" in g:
                    print(f"  {g['variant']} ERROR {g['error']}  | {q[:50]}")
                else:
                    print(f"  {g['variant']} {g['secs']:>5.1f}s {g['words']:>4}w {g['out_tok']:>5} tok "
                          f"think {g['thinking_tok']:>4} ${g['cost']:.4f} | {q[:50]}")

    print("\nvariant  model               median s  median words  words/s  out tok  $/answer (median)")
    for v in variants:
        rs = [r for r in rows if r["variant"] == v and "error" not in r]
        if not rs:
            continue
        med = lambda k: statistics.median(r[k] for r in rs)
        wps = statistics.median(r["words"] / r["secs"] for r in rs if r["secs"])
        print(f"   {v}     {rs[0]['model']:<18} {med('secs'):>7.1f}  {med('words'):>11.0f}  {wps:>7.1f}  "
              f"{med('out_tok'):>7.0f}  {med('cost'):>8.4f}")
    Path(args.out).write_text(json.dumps(rows, indent=1))
    print(f"\nanswers saved -> {args.out}")


if __name__ == "__main__":
    main()
