"""
Triage lexicon_draft.json terms into three categories:

  a = PI-coined vocabulary — invented or first named by PI / SoP researchers;
      not in standard field usage before this corpus
  b = PI-specific usage — a general term given a distinct PI-flavored definition
      or used in a way that differs meaningfully from the standard field sense
  c = Standard field terminology — used in PI papers with its normal meaning;
      useful for detecting adjacencies with adjacent fields but not PI-specific

Tags are added as {"triage": "a"|"b"|"c", "triage_reason": "<one phrase>"} on
each entry. Existing entries with a triage tag are skipped (resumable).
Nothing is dropped; all 914 terms are retained.

Usage:
    python3 ingest/triage_lexicon.py
    python3 ingest/triage_lexicon.py --dry-run      # print but don't write
    python3 ingest/triage_lexicon.py --retriage      # redo all entries
    python3 ingest/triage_lexicon.py --limit 50
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

LEXICON_PATH = Path(__file__).parent.parent / "sources" / "lexicon_draft.json"
MODEL        = "claude-haiku-4-5-20251001"
BATCH_SIZE   = 20
INTER_SLEEP  = 0.15   # between batches

SYSTEM = """\
You are classifying lexicon terms from the Protocol Institute (PI) research corpus \
into three categories:

  a = PI-coined — invented or first named by PI / Summer of Protocols researchers; \
not in standard academic or industry usage before this corpus. Examples: \
"whale fall" (organizational dissolution as gift), "protocol-pilled", \
"Kafka protocol", "Whitehead advance", "hyperstructures", "feral scholars".

  b = PI-specific usage — a standard term from philosophy, social science, \
economics, CS, or biology that PI uses with a distinct or narrowed definition \
that differs meaningfully from the standard field sense. Examples: \
"hardness" (protocol resistance to circumvention — not hardware hardness), \
"tension" (tradeoff + conflict requiring management — not stress), \
"cast" (future-state claim made reliable by a hardness source — not actor), \
"selection pressure" (applied to protocol propagation, not evolution).

  c = Standard field terminology — used in PI papers with its normal meaning \
from the source discipline; useful for detecting conceptual adjacencies with \
adjacent fields but not specific to PI. Examples: "bandwidth", "network memory", \
"agency/freedom", "trilemma" (standard in economics/CS), \
"ETTO" (Hollnagel's term, credited and unmodified).

Return ONLY a JSON array, one object per term, preserving input order:
[{"key": "<term_key>", "triage": "a"|"b"|"c", "reason": "<5–12 words>"}]
No commentary, no markdown fences.\
"""


def load_lexicon() -> dict:
    return json.loads(LEXICON_PATH.read_text())


def save_lexicon(lex: dict):
    LEXICON_PATH.write_text(json.dumps(lex, indent=2, ensure_ascii=False))


def build_batch_prompt(batch: list[tuple[str, dict]]) -> str:
    lines = []
    for key, entry in batch:
        lines.append(f'key: {key!r}')
        lines.append(f'term: {entry["term"]}')
        lines.append(f'definition: {entry["definition"][:300]}')
        ctx = entry.get("context", "")
        if ctx:
            lines.append(f'context: {ctx[:150]}')
        lines.append("")
    return "\n".join(lines).strip()


def triage_batch(client: anthropic.Anthropic, batch: list[tuple[str, dict]]) -> list[dict]:
    prompt = build_batch_prompt(batch)
    msg = client.messages.create(
        model=MODEL,
        max_tokens=len(batch) * 60 + 100,
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()
    if raw.startswith("```"):
        raw = "\n".join(raw.split("\n")[1:]).rstrip("`").strip()
    return json.loads(raw)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run",  action="store_true")
    parser.add_argument("--retriage", action="store_true", help="Re-triage already-tagged entries")
    parser.add_argument("--limit",    type=int)
    args = parser.parse_args()

    lex    = load_lexicon()
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    # Select candidates
    if args.retriage:
        candidates = list(lex.items())
    else:
        candidates = [(k, e) for k, e in lex.items() if "triage" not in e]

    if args.limit:
        candidates = candidates[:args.limit]

    total = len(candidates)
    print(f"Triaging {total} terms  (dry_run={args.dry_run}, batch_size={BATCH_SIZE})")
    if not total:
        print("Nothing to triage.")
        return

    done = errors = 0
    counts = {"a": 0, "b": 0, "c": 0}

    for i in range(0, total, BATCH_SIZE):
        batch = candidates[i : i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
        print(f"Batch {batch_num}/{total_batches}  ({len(batch)} terms)  ", end="", flush=True)

        try:
            results = triage_batch(client, batch)
        except Exception as e:
            print(f"✗ error: {e}")
            errors += len(batch)
            time.sleep(2)
            continue

        # Match results back by key — with position fallback for key drift
        result_map = {r["key"]: r for r in results}
        # Also build a normalized map for fuzzy matching
        norm_map = {r["key"].lower().strip(): r for r in results}
        tagged_this_batch = 0
        for idx_in_batch, (key, entry) in enumerate(batch):
            r = (result_map.get(key)
                 or norm_map.get(key.lower().strip())
                 or (results[idx_in_batch] if idx_in_batch < len(results) else None))
            if not r:
                print(f"\n  ⚠ no result for {key!r}")
                errors += 1
                continue
            tag    = r.get("triage", "c")
            reason = r.get("reason", "")
            counts[tag] = counts.get(tag, 0) + 1
            done += 1
            tagged_this_batch += 1
            if not args.dry_run:
                lex[key]["triage"]        = tag
                lex[key]["triage_reason"] = reason
            else:
                label = {"a": "PI-coined ", "b": "PI-specific", "c": "standard  "}[tag]
                print(f"\n  [{label}] {entry['term'][:45]:45s}  {reason}")

        print(f"a={sum(1 for _,r in result_map.items() if r.get('triage')=='a')} "
              f"b={sum(1 for _,r in result_map.items() if r.get('triage')=='b')} "
              f"c={sum(1 for _,r in result_map.items() if r.get('triage')=='c')}")

        if not args.dry_run:
            save_lexicon(lex)

        time.sleep(INTER_SLEEP)

    print(f"\n── Done ─────────────────────────────────────────")
    print(f"  Tagged  : {done}")
    print(f"  Errors  : {errors}")
    print(f"  a (PI-coined)    : {counts.get('a',0)}")
    print(f"  b (PI-specific)  : {counts.get('b',0)}")
    print(f"  c (standard)     : {counts.get('c',0)}")


if __name__ == "__main__":
    main()
