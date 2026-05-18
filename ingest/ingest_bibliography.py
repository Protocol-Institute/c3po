"""
Ingest bibliography references into Pinecone (namespace: bibliography).

Every reference gets a ref_summary vector regardless of S2 status.
Downloaded PDFs also get body chunk vectors.

relevance_score (0-3) is stored as metadata on every vector for retrieval weighting.

Run fetch_refs.py first to produce sources/bibliography/sourced_refs.json.

Usage:
    python3 ingest/ingest_bibliography.py
    python3 ingest/ingest_bibliography.py --type summaries
    python3 ingest/ingest_bibliography.py --type body
    python3 ingest/ingest_bibliography.py --id REF_ID
    python3 ingest/ingest_bibliography.py --dry-run
"""

import argparse
import json
import os
import sys
from pathlib import Path

import pdfplumber
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
from utils import clean_text, chunk_text, embed_chunks, chunk_id
from utils import get_voyage_client, get_pinecone_index, PINECONE_BATCH

load_dotenv()

NAMESPACE = "bibliography"
SOURCED_PATH = Path("sources/bibliography/sourced_refs.json")
PDF_DIR = Path("sources/bibliography/pdfs")


# ---------------------------------------------------------------------------
# Build the text for a ref_summary vector
# ---------------------------------------------------------------------------

def build_summary_text(ref: dict) -> str:
    """Construct the best available text for a ref_summary vector."""
    parts = []

    # Canonical title (prefer S2 title if cleaner)
    title = ref.get("s2_title") or ref.get("title") or ""
    parts.append(f"Title: {title}")

    # Authors
    authors = ref.get("s2_authors") or ref.get("authors") or []
    if authors:
        parts.append(f"Authors: {', '.join(str(a) for a in authors)}")

    # Year / venue
    year = ref.get("year")
    venue = ref.get("venue")
    if year and venue:
        parts.append(f"Published: {year}, {venue}")
    elif year:
        parts.append(f"Year: {year}")
    elif venue:
        parts.append(f"Venue: {venue}")

    # Citation context within PI corpus
    cited_by = ref.get("cited_by") or []
    if cited_by:
        parts.append(f"Cited by PI papers: {', '.join(cited_by)}")

    # Abstract (best available)
    abstract = ref.get("abstract") or ""
    if abstract:
        parts.append(f"\nAbstract: {abstract[:1500]}")

    # Relevance rationale always included
    rationale = ref.get("relevance_rationale") or ""
    if rationale:
        parts.append(f"\nRelevance to protocol studies: {rationale}")

    # Inline citation context if available
    context = ref.get("context") or ""
    if context and not abstract:
        parts.append(f"\nContext: {context}")

    return "\n".join(parts)


def build_summary_metadata(ref: dict) -> dict:
    authors = ref.get("s2_authors") or ref.get("authors") or []
    cited_by = ref.get("cited_by") or []
    return {
        "source": "bibliography",
        "chunk_type": "ref_summary",
        "ref_id": ref["_id"],
        "title": (ref.get("s2_title") or ref.get("title") or "")[:500],
        "authors": json.dumps([str(a) for a in authors]),
        "year": ref.get("year") or 0,
        "venue": (ref.get("venue") or "")[:200],
        "doi": (ref.get("doi") or "")[:200],
        "url": (ref.get("url") or "")[:500],
        "relevance_score": ref.get("relevance_score", 1),
        "relevance_rationale": (ref.get("relevance_rationale") or "")[:500],
        "citation_count": ref.get("citation_count", 0),
        "cited_by_count": len(cited_by),
        "cited_by": json.dumps(cited_by),
        "s2_status": ref.get("s2_status", "not_found"),
        "has_abstract": bool(ref.get("abstract")),
        "has_pdf": bool(ref.get("local_pdf")),
        "s2_citation_count": ref.get("s2_citation_count") or 0,
    }


# ---------------------------------------------------------------------------
# Ingest functions
# ---------------------------------------------------------------------------

def ingest_ref_summary(ref: dict, vc, index, dry_run: bool) -> int:
    text = build_summary_text(ref)
    meta = build_summary_metadata(ref)
    meta["text"] = text[:1000]

    if dry_run:
        return 1

    vecs = embed_chunks([text], vc)
    record = {
        "id": f"ref__{ref['_id']}__summary",
        "values": vecs[0],
        "metadata": meta,
    }
    index.upsert(vectors=[record], namespace=NAMESPACE)
    return 1


def ingest_pdf_chunks(ref: dict, vc, index, dry_run: bool) -> int:
    pdf_path = Path(ref["local_pdf"])
    if not pdf_path.exists():
        return 0

    try:
        with pdfplumber.open(pdf_path) as doc:
            raw = "\n".join(page.extract_text() or "" for page in doc.pages)
    except Exception as e:
        print(f"    PDF read error: {e}")
        return 0

    text = clean_text(raw)
    if len(text.strip()) < 200:
        return 0

    chunks = chunk_text(text)
    if not chunks:
        return 0

    title = ref.get("s2_title") or ref.get("title") or ""
    authors = ref.get("s2_authors") or ref.get("authors") or []
    rationale = ref.get("relevance_rationale") or ""
    prefix = (
        f"Title: {title}\n"
        f"Authors: {', '.join(str(a) for a in authors)}\n"
        f"Relevance: {rationale}\n\n"
    )
    embed_texts = [prefix + c for c in chunks]

    if dry_run:
        return len(chunks)

    vectors = embed_chunks(embed_texts, vc)
    meta_base = {
        **build_summary_metadata(ref),
        "chunk_type": "body",
    }

    records = []
    for i, (chunk, vec) in enumerate(zip(chunks, vectors)):
        m = {**meta_base, "chunk_index": i, "chunk_total": len(chunks), "text": chunk[:1000]}
        records.append({
            "id": f"ref__{ref['_id']}__body__{i:03d}",
            "values": vec,
            "metadata": m,
        })

    for i in range(0, len(records), PINECONE_BATCH):
        index.upsert(vectors=records[i:i + PINECONE_BATCH], namespace=NAMESPACE)

    return len(records)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--type", choices=["summaries", "body", "all"], default="all")
    parser.add_argument("--id", help="Process single ref by _id")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not SOURCED_PATH.exists():
        print(f"ERROR: {SOURCED_PATH} not found. Run fetch_refs.py first.")
        sys.exit(1)

    sourced = json.loads(SOURCED_PATH.read_text())
    targets = [sourced[args.id]] if args.id else list(sourced.values())

    print(f"Ingesting {len(targets)} refs → namespace '{NAMESPACE}'")
    if args.dry_run:
        print("(dry run)")

    if not args.dry_run:
        vc = get_voyage_client()
        index = get_pinecone_index()
    else:
        vc = index = None

    total_summaries = total_body = 0

    for i, ref in enumerate(targets):
        score = ref.get("relevance_score", 1)
        score_label = {3: "core", 2: "relevant", 1: "adjacent", 0: "incidental"}.get(score, "?")
        title = (ref.get("s2_title") or ref.get("title") or "")[:55]
        ab = "abs+" if ref.get("abstract") else ""
        pdf = "pdf+" if ref.get("local_pdf") else ""
        print(f"[{i+1}/{len(targets)}] [{score} {score_label}]{ab}{pdf} {title}")

        if args.type in ("summaries", "all"):
            n = ingest_ref_summary(ref, vc, index, args.dry_run)
            total_summaries += n

        if args.type in ("body", "all") and ref.get("local_pdf"):
            n = ingest_pdf_chunks(ref, vc, index, args.dry_run)
            total_body += n
            if n:
                print(f"  body: {n} chunks")

    print(f"\nTotal: {total_summaries} ref_summary + {total_body} body chunks → '{NAMESPACE}'")


if __name__ == "__main__":
    main()
