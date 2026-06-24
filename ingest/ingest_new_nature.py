"""
One-shot ingest for the New Nature special feature (June 2026).

Ingests two items into the pdfs Pinecone namespace:
  1. new-nature-essay — HTML essay at protocolized.io/features/new-nature
  2. new-nature-slides — 28-page PDF slide deck

Source files are read from the protocolized-website inbox processed directory.
Enrichment records are appended to sources/pdfs/enriched_meta.json.

Usage:
    python3 ingest/ingest_new_nature.py
    python3 ingest/ingest_new_nature.py --dry-run
"""

import argparse
import json
import re
import sys
from pathlib import Path

import pdfplumber
from bs4 import BeautifulSoup
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
from utils import clean_text, chunk_text, embed_chunks, upsert_chunks, chunk_id
from utils import get_voyage_client, get_pinecone_index, PINECONE_BATCH

load_dotenv(Path(__file__).parent.parent / ".env")

NAMESPACE = "pdfs"
INBOX_DIR = (
    Path(__file__).parent.parent.parent
    / "protocolized-website" / "inbox" / ".processed" / "new-nature"
)
ENRICHED_META_PATH = Path(__file__).parent.parent / "sources" / "pdfs" / "enriched_meta.json"

ESSAY_KEY  = "new-nature-essay"
SLIDES_KEY = "new-nature-slides.pdf"

ESSAY_ENRICHMENT = {
    "title":          "New Nature",
    "summary":        (
        "Venkatesh Rao unpacks the Protocol Institute's core research thesis: "
        "New Nature = AI and Protocols entangled at planetary scale. Develops the "
        "Humboldt analogy, the Evil Twins Thesis (AI as irresistible force vs. "
        "protocols as immovable objects), a three-circle Venn of AI/Protocols/Planetary, "
        "and PI's positioning as a Context Tank in the priceless-planetary quadrant."
    ),
    "categories":     ["protocol-theory", "ai", "planetary-scale", "editorial", "evil-twins"],
    "primary_author": "Venkatesh Rao",
    "all_authors":    ["Venkatesh Rao"],
    "doc_type":       "special-feature",
    "date":           "2026-06-17",
    "url":            "https://protocolized.io/features/new-nature",
    "tags":           ["new-nature", "protocol-theory", "ai", "planetary-scale", "evil-twins", "editorial"],
}

SLIDES_ENRICHMENT = {
    "title":          "New Nature — Talk Slides (June 2026)",
    "summary":        (
        "Slide deck from the June 17, 2026 New Nature talk. Covers the Humboldt parallel, "
        "the Evil Twins Thesis (AI vs Protocols), the three-circle Venn diagram of "
        "AI/Protocols/Planetary, PI's Context Tank positioning, and the Special Interest "
        "Group research model."
    ),
    "categories":     ["protocol-theory", "ai", "planetary-scale", "evil-twins"],
    "primary_author": "Venkatesh Rao",
    "all_authors":    ["Venkatesh Rao"],
    "doc_type":       "handout",
    "date":           "2026-06-17",
    "url":            "https://protocolized.io/features/new-nature",
    "file":           "https://files.protocolized.io/features/new-nature-slides.pdf",
    "tags":           ["new-nature", "protocol-theory", "ai", "planetary-scale", "evil-twins"],
}


def extract_essay_text() -> str:
    html_path = INBOX_DIR / "index.html"
    if not html_path.exists():
        raise FileNotFoundError(f"Essay HTML not found: {html_path}")
    html = html_path.read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")
    # Remove style/script tags
    for tag in soup(["style", "script"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    # Collapse whitespace
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def extract_slides_text() -> str:
    pdf_path = INBOX_DIR / "slides.pdf"
    if not pdf_path.exists():
        raise FileNotFoundError(f"Slides PDF not found: {pdf_path}")
    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t and t.strip():
                pages.append(t.strip())
    return "\n\n".join(pages)


def ingest_item(key: str, raw_text: str, enrichment: dict, vc, index, dry_run: bool) -> int:
    title    = enrichment["title"]
    summary  = enrichment["summary"]
    authors  = enrichment["all_authors"]
    doc_type = enrichment["doc_type"]
    url      = enrichment["url"]
    date     = enrichment["date"]
    tags     = enrichment.get("tags", [])
    cats     = enrichment.get("categories", [])
    authors_str = ", ".join(authors)

    text = clean_text(raw_text)
    chunks = chunk_text(text)
    print(f"  [{key}] {len(chunks)} chunks from {len(text)} chars")

    prefix = (
        f"Title: {title}\n"
        f"Authors: {authors_str}\n"
        f"Type: {doc_type}\n"
        f"Summary: {summary}\n\n"
    )
    prefixed = [prefix + c for c in chunks]

    vector_meta = {
        "source":               "special_feature",
        "namespace":            NAMESPACE,
        "chunk_type":           "body",
        "title":                title,
        "authors":              authors,
        "primary_author":       enrichment["primary_author"],
        "date":                 date,
        "type":                 doc_type,
        "tags":                 tags,
        "url":                  url,
        "summary":              summary[:500],
        "enriched_categories":  cats,
        "access_level":         "public",
    }

    # Doc summary vector
    summary_text = (
        f"Title: {title}\n"
        f"Authors: {authors_str}\n"
        f"Date: {date}\n"
        f"Type: {doc_type}\n"
        f"Tags: {', '.join(tags)}\n"
        f"Categories: {', '.join(cats)}\n"
        f"Summary: {summary}"
    )
    summary_id = f"{key}__doc_summary"
    summary_meta = {
        **vector_meta,
        "chunk_type": "doc_summary",
        "text": summary_text[:1000],
    }

    if dry_run:
        print(f"  [dry-run] would upsert {len(chunks)} body + 1 doc_summary vectors")
        return 0

    print(f"  Embedding {len(prefixed)} body chunks...")
    body_vectors = embed_chunks(prefixed, vc)
    body_count = upsert_chunks(chunks, body_vectors, vector_meta, index, namespace=NAMESPACE)
    print(f"  Upserted {body_count} body chunks")

    print(f"  Embedding doc_summary...")
    summary_vector = embed_chunks([summary_text], vc)[0]
    index.upsert(
        vectors=[{"id": summary_id, "values": summary_vector, "metadata": summary_meta}],
        namespace=NAMESPACE,
    )
    print(f"  Upserted doc_summary: {summary_id}")

    return body_count + 1


def update_enriched_meta(dry_run: bool) -> None:
    enriched = {}
    if ENRICHED_META_PATH.exists():
        enriched = json.loads(ENRICHED_META_PATH.read_text())

    for key, enrich in [(ESSAY_KEY, ESSAY_ENRICHMENT), (SLIDES_KEY, SLIDES_ENRICHMENT)]:
        if key in enriched:
            print(f"  enriched_meta: {key} already present — skipping")
        else:
            enriched[key] = enrich
            print(f"  enriched_meta: added {key}")

    if not dry_run:
        ENRICHED_META_PATH.write_text(json.dumps(enriched, indent=2, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print(f"New Nature ingest  (dry_run={args.dry_run})")
    print(f"Inbox dir: {INBOX_DIR}")

    if not INBOX_DIR.exists():
        print(f"ERROR: inbox dir not found: {INBOX_DIR}")
        sys.exit(1)

    print("\n── Essay HTML ─────────────────────────────────")
    essay_text = extract_essay_text()
    print(f"  Extracted {len(essay_text)} chars")

    print("\n── Slides PDF ─────────────────────────────────")
    slides_text = extract_slides_text()
    print(f"  Extracted {len(slides_text)} chars")

    print("\n── Enriched metadata ──────────────────────────")
    update_enriched_meta(args.dry_run)

    if args.dry_run:
        print("\n── Ingest (dry-run) ───────────────────────────")
        ingest_item(ESSAY_KEY, essay_text, ESSAY_ENRICHMENT, None, None, dry_run=True)
        ingest_item(SLIDES_KEY, slides_text, SLIDES_ENRICHMENT, None, None, dry_run=True)
        print("\nDry run complete — no vectors upserted.")
        return

    vc    = get_voyage_client()
    index = get_pinecone_index()

    print("\n── Ingest ─────────────────────────────────────")
    essay_count  = ingest_item(ESSAY_KEY,  essay_text,  ESSAY_ENRICHMENT,  vc, index, dry_run=False)
    slides_count = ingest_item(SLIDES_KEY, slides_text, SLIDES_ENRICHMENT, vc, index, dry_run=False)

    print(f"\n── Done ───────────────────────────────────────")
    print(f"  Essay  : {essay_count} vectors")
    print(f"  Slides : {slides_count} vectors")
    print(f"  Total  : {essay_count + slides_count} vectors → pdfs namespace")


if __name__ == "__main__":
    main()
