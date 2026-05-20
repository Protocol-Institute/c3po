"""
Enrich fetched discord_links with Claude Haiku relevance scores.

For each URL with fetch_status=fetched and no relevance_score yet:
  1. Reconstruct vector IDs (deterministic from url + chunk_index)
  2. Fetch chunk text from Pinecone
  3. Call Claude Haiku to score 0–3 for protocol research relevance
  4. Score 0 → delete vectors from Pinecone, mark registry entry filtered_irrelevant
  5. Score 1–3 → update each vector's metadata with relevance_score field

Run after fetch_discord_links.py has completed. Safe to re-run: already-scored
entries are skipped. Interrupted runs resume where they left off.

Usage:
    python3 ingest/enrich_discord_links.py
    python3 ingest/enrich_discord_links.py --dry-run
    python3 ingest/enrich_discord_links.py --limit 50
    python3 ingest/enrich_discord_links.py --rescore   # re-score already-scored entries
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
from utils import chunk_id, get_pinecone_index, append_run_log

load_dotenv(Path(__file__).parent.parent / ".env")

NAMESPACE     = "discord_links"
REGISTRY_PATH = Path(__file__).parent.parent / "data" / "discord_links_registry.json"

HAIKU_MODEL   = "claude-haiku-4-5-20251001"
INTER_SLEEP   = 0.2   # seconds between Haiku calls
MAX_TEXT_CHARS = 2000  # chars of assembled chunk text sent to Haiku

SCORE_PROMPT = """\
Score this web page on its relevance to Protocol Institute research. Use 0–3.

The Protocol Institute studies protocols as a category: coordination mechanisms, \
governance design, institutional theory, cryptography, distributed systems, \
infrastructure, standards bodies, organizational behavior, and related topics. \
It also covers protocol fiction and narrative approaches to these themes.

Scores:
0 = Not relevant (general news, entertainment, sports, cooking, personal content, \
    pure technology unrelated to coordination or governance)
1 = Tangential (general philosophy, social science, technology not specifically \
    about coordination; interesting but unlikely to answer protocol research questions)
2 = Clearly relevant (governance, standards, coordination mechanisms, infrastructure \
    studies, institutional analysis, formal systems, regulatory frameworks)
3 = Directly on-topic (protocols, coordination theory, cryptographic protocols, \
    distributed consensus, governance design, formal protocol modeling, or \
    content explicitly cited in PI research)

Respond with JSON only — no commentary:
{{"score": <0-3>, "reason": "<one sentence>"}}

URL: {url}
Domain: {domain}
Excerpt ({chars} chars):
{text}
"""


def load_registry() -> dict:
    return json.loads(REGISTRY_PATH.read_text())


def save_registry(registry: dict):
    REGISTRY_PATH.write_text(json.dumps(registry, indent=2, ensure_ascii=False))


def reconstruct_ids(url: str, vector_count: int) -> list[str]:
    """Rebuild the deterministic vector IDs used by fetch_discord_links.py."""
    return [f"discord_link__{chunk_id(url + str(i))}" for i in range(vector_count)]


def fetch_chunk_text(idx, url: str, vector_count: int) -> str:
    """Fetch text from Pinecone chunks and assemble in chunk_index order."""
    ids = reconstruct_ids(url, vector_count)
    if not ids:
        return ""

    # Fetch in batches of 99 (Pinecone limit)
    chunks: dict[int, str] = {}
    for i in range(0, len(ids), 99):
        batch = idx.fetch(ids=ids[i:i+99], namespace=NAMESPACE)
        for vid, vec in batch.vectors.items():
            m = vec.metadata
            ci = m.get("chunk_index", 0)
            chunks[ci] = m.get("text", "")

    if not chunks:
        return ""

    ordered = [chunks[k] for k in sorted(chunks)]
    assembled = "\n\n".join(ordered)
    return assembled[:MAX_TEXT_CHARS]


def score_relevance(client: anthropic.Anthropic, url: str, domain: str, text: str) -> tuple[int, str]:
    """Call Claude Haiku and return (score, reason)."""
    prompt = SCORE_PROMPT.format(
        url=url, domain=domain,
        chars=len(text), text=text,
    )
    msg = client.messages.create(
        model=HAIKU_MODEL,
        max_tokens=80,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()
    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = "\n".join(raw.split("\n")[1:]).rstrip("`").strip()
    try:
        parsed = json.loads(raw)
        score = int(parsed.get("score", 1))
        reason = str(parsed.get("reason", ""))
        return max(0, min(3, score)), reason
    except Exception:
        # Non-JSON response: treat as score 1 (uncertain, keep it)
        return 1, f"parse_error: {raw[:80]}"


def delete_vectors(idx, url: str, vector_count: int):
    ids = reconstruct_ids(url, vector_count)
    if ids:
        idx.delete(ids=ids, namespace=NAMESPACE)


def update_vector_scores(idx, url: str, vector_count: int, score: int):
    ids = reconstruct_ids(url, vector_count)
    for vid in ids:
        idx.update(id=vid, set_metadata={"relevance_score": score}, namespace=NAMESPACE)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run",  action="store_true", help="Score but don't write to Pinecone or registry")
    parser.add_argument("--limit",    type=int, help="Max URLs to score this run")
    parser.add_argument("--rescore",  action="store_true", help="Re-score already-scored entries")
    args = parser.parse_args()

    registry = load_registry()
    idx      = get_pinecone_index()
    client   = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    # Select entries to score
    if args.rescore:
        candidates = [
            (k, e) for k, e in registry.items()
            if e.get("fetch_status") == "fetched"
        ]
    else:
        candidates = [
            (k, e) for k, e in registry.items()
            if e.get("fetch_status") == "fetched" and "relevance_score" not in e
        ]

    if args.limit:
        candidates = candidates[:args.limit]

    total = len(candidates)
    print(f"Scoring {total} URLs  (dry_run={args.dry_run})")
    if not total:
        print("Nothing to score.")
        return

    deleted = scored = errors = 0
    score_dist: dict[int, int] = {0: 0, 1: 0, 2: 0, 3: 0}

    for i, (key, entry) in enumerate(candidates):
        url    = entry.get("url", key)
        domain = entry.get("domain", "")
        vcount = entry.get("vector_count", 0)

        print(f"[{i+1}/{total}] {domain} — {url[:70]}")

        text = fetch_chunk_text(idx, url, vcount)
        if not text:
            print("  ✗ no text in Pinecone — skipping")
            errors += 1
            continue

        try:
            score, reason = score_relevance(client, url, domain, text)
        except Exception as e:
            print(f"  ✗ Haiku error: {e}")
            errors += 1
            time.sleep(1)
            continue

        score_dist[score] = score_dist.get(score, 0) + 1
        tag = ["✗ irrelevant", "~ tangential", "✓ relevant", "✓✓ on-topic"][score]
        print(f"  {tag}  [{score}]  {reason[:80]}")

        if args.dry_run:
            scored += 1
            continue

        if score == 0:
            delete_vectors(idx, url, vcount)
            registry[key]["fetch_status"]    = "filtered_irrelevant"
            registry[key]["relevance_score"] = 0
            registry[key]["relevance_reason"] = reason
            deleted += 1
        else:
            update_vector_scores(idx, url, vcount, score)
            registry[key]["relevance_score"]  = score
            registry[key]["relevance_reason"] = reason
            scored += 1

        save_registry(registry)
        time.sleep(INTER_SLEEP)

    print(f"\n── Done ────────────────────────────────────────")
    print(f"  Scored     : {scored}")
    print(f"  Deleted (0): {deleted}")
    print(f"  Errors     : {errors}")
    print(f"  Distribution: {score_dist}")

    if not args.dry_run:
        append_run_log({
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "script": "enrich_links",
            "scored": scored,
            "pruned_irrelevant": deleted,
            "errors": errors,
            "score_dist": {str(k): v for k, v in score_dist.items()},
        })


if __name__ == "__main__":
    main()
