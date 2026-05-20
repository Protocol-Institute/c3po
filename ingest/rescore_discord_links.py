"""
Re-score discord_links with an expanded relevance rubric (v2).

Scores are stored as relevance_score_v2 / relevance_reason_v2, leaving the
original relevance_score / relevance_reason fields untouched for comparison.

Scope of C3PO (v2 rubric):
  3 = Core — directly about protocols, coordination, cryptography, distributed
      systems, institutional theory, governance design, protocol fiction,
      memory/cognition research, or explicitly cited by PI researchers.
  2 = In scope — technology as protocol substrate (AI, robotics, digital
      infrastructure); cultural history and pre-modern coordination; governance,
      policy, regulation; formal/mathematical structures; notation and
      representation systems; science of coordination; speculative fiction;
      spatial/temporal epistemology; research practice.
  1 = Adjacent — general science, technology, arts, philosophy, current events,
      news incidents that illuminate coordination dynamics; the broad intellectual
      interests of the PI community.
  0 = Out of scope — pure entertainment, commercial products with no research
      angle, personal lifestyle content, spam or placeholder pages.

By default rescores only filtered_irrelevant entries (the 485 dropped by v1).
Use --all to rescore all fetched entries too (for bidirectional diff).

Usage:
    python3 ingest/rescore_discord_links.py
    python3 ingest/rescore_discord_links.py --dry-run
    python3 ingest/rescore_discord_links.py --all
    python3 ingest/rescore_discord_links.py --limit 20
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import anthropic
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
from utils import chunk_id, get_pinecone_index

load_dotenv(Path(__file__).parent.parent / ".env")

NAMESPACE      = "discord_links"
REGISTRY_PATH  = Path(__file__).parent.parent / "data" / "discord_links_registry.json"
HAIKU_MODEL    = "claude-haiku-4-5-20251001"
INTER_SLEEP    = 0.2
MAX_TEXT_CHARS = 2000

SCORE_PROMPT = """\
Score this web page for C3PO — the Protocol Institute's broad-based oracle.

C3PO covers the Protocol Institute's full intellectual world. Use this rubric:

SCORE 3 — Core (directly on-topic):
  - Protocol theory: coordination mechanisms, governance design, cryptography,
    distributed consensus, formal modeling, standards bodies, institutional theory
  - Protocol fiction: narrative or speculative fiction exploring governance,
    coordination, institutional life; genre theory about such fiction
  - Memory and cognition: mnemonic systems, procedural memory, the Whitehead
    advance (habitual performance freeing cognition), spaced repetition, habits
  - Work explicitly produced or cited by Protocol Institute researchers

SCORE 2 — In scope (clearly relevant to PI's intellectual world):
  - Technology as protocol substrate: AI agents, robotics, distributed systems,
    digital infrastructure — especially where new coordination forms emerge
  - Cultural history and pre-modern protocols: rituals, kinship, trade routes,
    craft traditions, religious institutions as protocol archaeology
  - Governance, policy, regulation: institutional design, regulatory frameworks,
    political economy of standards, real-world protocol failures and reforms
  - Formal and mathematical structures: graph theory, category theory, game
    theory, formal verification, complexity — tools for thinking about coordination
  - Notation and representation: symbolic encoding of human practices (dance
    notation, musical scores, recipe formats, code, drawing conventions)
  - Science of coordination: organizational behavior, complex systems, evolutionary
    dynamics, collective intelligence
  - Speculative and genre fiction: weird literature, science fiction, experimental
    forms that make institutional or coordination dynamics legible
  - Spatial and temporal epistemology: navigation, cartography, deep-time thinking,
    longevity research — context for protocols operating at large scales
  - Research practice and epistemic culture: how knowledge is produced, validated,
    transmitted; peer review, taste formation, intellectual self-cultivation

SCORE 1 — Adjacent (interesting to the PI community, not specifically protocol):
  - General science, technology, arts, philosophy a thoughtful PI member would read
  - News or current events illuminating coordination breakdowns or policy failures
  - The broader humanistic and intellectual interests of the community

SCORE 0 — Out of scope:
  - Pure entertainment with no intellectual content (sports scores, celebrity gossip)
  - Commercial products or services with no research or coordination angle
  - Personal lifestyle content (fitness apps, wellness tracking, travel booking)
  - Spam, placeholder pages, error pages, or pages with no extractable content

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
    return [f"discord_link__{chunk_id(url + str(i))}" for i in range(vector_count)]


def fetch_chunk_text(idx, url: str, vector_count: int) -> str:
    ids = reconstruct_ids(url, vector_count)
    if not ids:
        return ""
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
    return "\n\n".join(ordered)[:MAX_TEXT_CHARS]


def fetch_text_from_registry(entry: dict) -> str:
    """For filtered_irrelevant entries, text isn't in Pinecone — use stored excerpt if any."""
    # We don't store raw text in the registry, so return empty;
    # the caller will try Pinecone first (for fetched entries),
    # then fall back to domain + URL as minimal context.
    return ""


def score_relevance(client: anthropic.Anthropic, url: str, domain: str, text: str) -> tuple[int, str]:
    prompt = SCORE_PROMPT.format(url=url, domain=domain, chars=len(text), text=text)
    msg = client.messages.create(
        model=HAIKU_MODEL,
        max_tokens=80,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()
    if raw.startswith("```"):
        raw = "\n".join(raw.split("\n")[1:]).rstrip("`").strip()
    try:
        parsed = json.loads(raw)
        score = int(parsed.get("score", 1))
        reason = str(parsed.get("reason", ""))
        return max(0, min(3, score)), reason
    except Exception:
        return 1, f"parse_error: {raw[:80]}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--all",     action="store_true",
                        help="Rescore all fetched entries, not just filtered_irrelevant")
    parser.add_argument("--limit",   type=int)
    args = parser.parse_args()

    registry = load_registry()
    idx      = get_pinecone_index()
    client   = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    if args.all:
        statuses = {"fetched", "filtered_irrelevant"}
    else:
        statuses = {"filtered_irrelevant"}

    candidates = [
        (k, e) for k, e in registry.items()
        if e.get("fetch_status") in statuses
    ]

    if args.limit:
        candidates = candidates[:args.limit]

    total = len(candidates)
    print(f"Rescoring {total} URLs  (statuses={statuses}, dry_run={args.dry_run})")
    if not total:
        print("Nothing to rescore.")
        return

    scored = errors = 0
    score_dist: dict[int, int] = {0: 0, 1: 0, 2: 0, 3: 0}
    rescued = 0  # filtered_irrelevant that score >= 1 under v2

    for i, (key, entry) in enumerate(candidates):
        url    = entry.get("url", key)
        domain = entry.get("domain", "")
        vcount = entry.get("vector_count", 0)
        status = entry.get("fetch_status", "")
        old_score = entry.get("relevance_score", "—")

        print(f"[{i+1}/{total}] {domain} — {url[:70]}")

        # For fetched entries, text is still in Pinecone.
        # For filtered_irrelevant, vectors were deleted — score on domain+URL alone.
        if status == "fetched":
            text = fetch_chunk_text(idx, url, vcount)
        else:
            text = ""

        if not text:
            # Minimal context: just domain and URL path
            path = url.split("//")[-1] if "//" in url else url
            text = f"[no body text available — scoring on URL and domain only]\nURL path: {path}"

        try:
            score, reason = score_relevance(client, url, domain, text)
        except Exception as e:
            print(f"  ✗ Haiku error: {e}")
            errors += 1
            time.sleep(1)
            continue

        score_dist[score] = score_dist.get(score, 0) + 1
        tag = ["✗ out-of-scope", "~ adjacent", "✓ in-scope", "✓✓ core"][score]
        delta = ""
        if status == "filtered_irrelevant":
            if score >= 1:
                rescued += 1
                delta = f"  ← RESCUED from score {old_score}"
            else:
                delta = f"  (still 0)"
        elif str(old_score) != str(score):
            delta = f"  ← was {old_score}"

        print(f"  {tag}  [v2={score}]{delta}  {reason[:80]}")

        scored += 1
        if args.dry_run:
            continue

        registry[key]["relevance_score_v2"]  = score
        registry[key]["relevance_reason_v2"] = reason
        save_registry(registry)
        time.sleep(INTER_SLEEP)

    print(f"\n── Done ─────────────────────────────────────────")
    print(f"  Scored      : {scored}")
    print(f"  Errors      : {errors}")
    print(f"  Distribution: {score_dist}")
    if "filtered_irrelevant" in statuses:
        print(f"  Rescued (≥1): {rescued} of {sum(1 for _,e in candidates if e.get('fetch_status')=='filtered_irrelevant')}")


if __name__ == "__main__":
    main()
