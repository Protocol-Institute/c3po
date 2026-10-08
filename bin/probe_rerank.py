"""
Reranker probe — plans/reranker.md, issue #10.

For each probe query, fetch the live search_corpus pool (cosine × tier weight
order), rerank it locally with one or more Voyage rerank models, and print the
orders side by side. With --worker it also asks the worker to rerank
(`rerank: true`) so the deployed code path is compared against the local one.

The query set is the point: re-run it after any retrieval change rather than
re-deriving a new ad-hoc set each time.

Usage:
    python3 bin/probe_rerank.py                       # all queries, default models
    python3 bin/probe_rerank.py --models rerank-2.5   # one model
    python3 bin/probe_rerank.py --worker              # also show the worker's rerank
    python3 bin/probe_rerank.py --query "how do protocols die"
    python3 bin/probe_rerank.py --summary             # one line per query
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

MCP_URL    = "https://pibot.protocolized.io/mcp"
RERANK_URL = "https://api.voyageai.com/v1/rerank"
DOC_CHARS  = 2000   # match the worker's rerank document length
POOL       = 20     # search_corpus maximum

QUERIES = [
    # From issue #10
    "how do protocols die or get replaced",
    "what makes a protocol hard to change once adopted",
    "memory as protocol continuity infrastructure",
    "native plants, prairie restoration and ecological gardening",   # off-corpus control
    # Named people / events — rerankers score these high; cosine is usually fine
    "what did Venkat and Humboldt say about the trust ratchet",
    "is there a recording of Helena Rong's talk",
    "what workshops are happening at the Protocol Symposium",
    # Definitional
    "what is protocol atrophy",
    "what is the hardness of a protocol",
    # SIG / community
    "when does the Memory Research Group meet",
    "what is SIGPSY working on",
    "how does blygger handle stubs and forks",
    # Thematic, broad
    "protocol fiction stories about artificial intelligence",
    "how do protocols relate to institutions and bureaucracy",
    "what does the Protocol Institute fund",
]


def search_corpus(query: str, rerank: bool = False) -> dict:
    args = {"query": query, "limit": POOL}
    if rerank:
        args["rerank"] = True
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "search_corpus", "arguments": args}}).encode()
    req = urllib.request.Request(MCP_URL, data=body, headers={
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": "c3po-probe-rerank/1.0",
    })
    with urllib.request.urlopen(req, timeout=60) as r:
        payload = json.load(r)
    return json.loads(payload["result"]["content"][0]["text"])


def rerank(query: str, docs: list[str], model: str) -> tuple[list[float], float]:
    body = json.dumps({"query": query, "documents": docs, "model": model}).encode()
    req = urllib.request.Request(RERANK_URL, data=body, headers={
        "Authorization": f"Bearer {os.environ['VOYAGE_API_KEY']}",
        "Content-Type": "application/json",
    })
    t = time.time()
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    scores = [0.0] * len(docs)
    for d in data["data"]:
        scores[d["index"]] = d["relevance_score"]
    return scores, time.time() - t


def doc_text(item: dict) -> str:
    return f"{item.get('title') or ''}\n\n{(item.get('excerpt') or '')[:DOC_CHARS]}"


def label(item: dict) -> str:
    return f"{item['source']:<10} {(item.get('title') or '')[:52]}"


def topk_overlap(a: list[int], b: list[int], k: int = 5) -> int:
    return len(set(a[:k]) & set(b[:k]))


def probe(query: str, models: list[str], worker: bool, summary: bool):
    res = search_corpus(query)
    items = res["results"]
    if not items:
        print(f"\n### {query}\n  (no results)")
        return
    docs = [doc_text(i) for i in items]
    cosine_order = list(range(len(items)))

    per_model = {}
    for m in models:
        scores, secs = rerank(query, docs, m)
        order = sorted(cosine_order, key=lambda i: -scores[i])
        per_model[m] = (scores, order, secs)

    worker_items = None
    if worker:
        wres = search_corpus(query, rerank=True)
        worker_items = wres["results"]

    if summary:
        parts = [f"{m}: top5∩cos={topk_overlap(cosine_order, o)}/5 max={max(s):.2f} ({t:.2f}s)"
                 for m, (s, o, t) in per_model.items()]
        print(f"{query[:58]:<58} | " + " | ".join(parts))
        return

    print(f"\n### {query}")
    hdr = f"  {'cos':>3}  {'item':<63}" + "".join(f" {m[-12:]:>14}" for m in models)
    print(hdr)
    for i, it in enumerate(items):
        cells = ""
        for m in models:
            scores, order, _ = per_model[m]
            cells += f" {scores[i]:>6.3f} (#{order.index(i)+1:>2})"
        print(f"  {i+1:>3}  {label(it):<63}{cells}")
    for m, (scores, order, secs) in per_model.items():
        print(f"  {m}: reranked top 6 = cosine ranks {[i+1 for i in order[:6]]}  "
              f"· top-5 overlap with cosine {topk_overlap(cosine_order, order)}/5 · {secs:.2f}s")
    if worker_items is not None:
        flag = worker_items and worker_items[0].get("rerank_score") is not None
        print(f"  worker rerank ({'on' if flag else 'NOT APPLIED'}):")
        for j, it in enumerate(worker_items[:8]):
            rs = it.get("rerank_score")
            print(f"    {j+1:>2}  {label(it)}  {'' if rs is None else f'{rs:.3f}'}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", default="rerank-2.5,rerank-3",
                    help="comma-separated Voyage rerank models")
    ap.add_argument("--query", help="run a single query instead of the probe set")
    ap.add_argument("--worker", action="store_true", help="also call search_corpus with rerank: true")
    ap.add_argument("--summary", action="store_true", help="one line per query")
    args = ap.parse_args()

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    queries = [args.query] if args.query else QUERIES
    for q in queries:
        try:
            probe(q, models, args.worker, args.summary)
        except urllib.error.HTTPError as e:
            print(f"\n### {q}\n  HTTP {e.code}: {e.read()[:200]!r}", file=sys.stderr)


if __name__ == "__main__":
    main()
