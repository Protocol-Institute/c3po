"""
Sync lexicon definitions to Pinecone `definitions` namespace.

Reads sources/lexicon_draft.json (schema: {term: [entry, ...]}).
Ingests triage a+b entries only (~560 entries).

Vector IDs:  lexicon__{term_slug}__{source_slug}
Namespace:   definitions
"""

import argparse
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

LEXICON_PATH = Path(__file__).parent.parent / "sources" / "lexicon_draft.json"
NAMESPACE    = "definitions"
BATCH_SIZE   = 96
INGEST_TRIAGE = {"a", "b"}


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    return re.sub(r"-+", "-", text).strip("-")[:80]


def make_text(entry: dict) -> str:
    parts = [f"**{entry['term']}**", "", f"Definition: {entry['definition']}"]
    if entry.get("context"):
        parts += ["", f"Context: {entry['context']}"]
    if entry.get("source"):
        parts += ["", f"Source: {entry['source']}"]
    return "\n".join(parts)


def build_records(lexicon: dict) -> list[dict]:
    records = []
    for term, entries in lexicon.items():
        eligible = [e for e in entries if e.get("triage") in INGEST_TRIAGE]
        if not eligible:
            continue
        variant_count = len(eligible)
        for i, entry in enumerate(eligible):
            source_slug = entry.get("source_slug") or str(i)
            vec_id = f"lexicon__{slugify(term)}__{slugify(source_slug)}"
            records.append({
                "id":   vec_id,
                "text": make_text(entry),
                "meta": {
                    "term":             entry["term"],
                    "definition_index": i,
                    "variant_count":    variant_count,
                    "source":           entry.get("source", ""),
                    "source_slug":      entry.get("source_slug", ""),
                    "triage":           entry.get("triage", ""),
                    "triage_reason":    entry.get("triage_reason", ""),
                    "chunk_type":       "lexicon_definition",
                },
            })
    return records


def embed_batch(texts: list[str], voyage_client) -> list[list[float]]:
    result = voyage_client.embed(texts, model="voyage-3", input_type="document")
    return result.embeddings


def upsert_batch(records: list[dict], index, voyage_client, dry_run: bool) -> int:
    texts = [r["text"] for r in records]
    if dry_run:
        for r in records:
            print(f"  [dry] {r['id'][:70]}  triage={r['meta']['triage']}")
        return len(records)

    embeddings = embed_batch(texts, voyage_client)
    vectors = [
        {"id": r["id"], "values": emb, "metadata": r["meta"]}
        for r, emb in zip(records, embeddings)
    ]
    index.upsert(vectors=vectors, namespace=NAMESPACE)
    return len(vectors)


def main():
    parser = argparse.ArgumentParser(description="Sync lexicon to Pinecone definitions namespace")
    parser.add_argument("--dry-run", action="store_true", help="Preview records without embedding or upserting")
    parser.add_argument("--limit",   type=int, default=0,  help="Max records to process (0 = all)")
    args = parser.parse_args()

    import json
    lexicon = json.loads(LEXICON_PATH.read_text())

    records = build_records(lexicon)
    print(f"Built {len(records)} records from triage a+b entries")

    if args.limit:
        records = records[:args.limit]
        print(f"Limited to {len(records)} records")

    if args.dry_run:
        print(f"\nDry run — showing first {min(20, len(records))} records:\n")
        for r in records[:20]:
            print(f"  {r['id']}")
            print(f"    term={r['meta']['term']!r}  triage={r['meta']['triage']}  variant_count={r['meta']['variant_count']}")
            print()
        print(f"... {len(records)} total records would be upserted to namespace '{NAMESPACE}'")
        return

    import voyageai
    from pinecone import Pinecone

    voyage = voyageai.Client(api_key=os.environ["VOYAGE_API_KEY"])
    pc     = Pinecone(api_key=os.environ["PINECONE_API_KEY"])
    index  = pc.Index(host=os.environ["PINECONE_C3PO_HOST"])

    total = 0
    for i in range(0, len(records), BATCH_SIZE):
        batch = records[i : i + BATCH_SIZE]
        n = upsert_batch(batch, index, voyage, dry_run=False)
        total += n
        print(f"  Upserted {total}/{len(records)}")

    print(f"\nDone — {total} vectors upserted to namespace '{NAMESPACE}'")

    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from utils import append_run_log
        from datetime import datetime, timezone
        append_run_log({
            "ts":      datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "script":  "sync_lexicon",
            "vectors": total,
        })
    except Exception as e:
        print(f"  Warning: failed to write run log: {e}")


if __name__ == "__main__":
    main()
