"""
Ingest PDFs from data/pdfs/ into Pinecone (namespace: pdfs).

Two vector types per document (see ARCHITECTURE.md — Ingest Pipeline Pattern):
  1. body       — chunked text with title/authors/type/summary prefix for retrieval
  2. doc_summary — one vector per PDF (title + authors + type + tags + summary)

Run enrich_pdfs.py first to generate sources/pdfs/enriched_meta.json.

Usage:
    python3 ingest/ingest_pdfs.py
    python3 ingest/ingest_pdfs.py --pdf some-file.pdf
    python3 ingest/ingest_pdfs.py --type body
    python3 ingest/ingest_pdfs.py --type summaries
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

import pdfplumber
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
from utils import clean_text, chunk_text, embed_chunks, upsert_chunks, chunk_id
from utils import get_voyage_client, get_pinecone_index, PINECONE_BATCH

load_dotenv()

NAMESPACE = "pdfs"
PDF_DIR = Path(__file__).parent.parent / "data" / "pdfs"
RESOURCES_DIR = (
    Path(__file__).parent.parent.parent
    / "protocolized-website"
    / "src"
    / "content"
    / "resources"
)
ENRICHED_META_PATH = Path("sources/pdfs/enriched_meta.json")


def load_resource_metadata() -> dict[str, dict]:
    """Read resource markdowns, indexed by PDF basename."""
    metadata = {}
    if not RESOURCES_DIR.exists():
        print(f"Warning: resources dir not found at {RESOURCES_DIR}")
        return metadata

    for md_file in RESOURCES_DIR.glob("*.md"):
        text = md_file.read_text(encoding="utf-8")
        match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
        if not match:
            continue
        fm = match.group(1)
        file_match = re.search(r'^file:\s*["\']?/resources/([^"\']+)["\']?', fm, re.MULTILINE)
        if not file_match:
            continue
        basename = file_match.group(1).strip()
        title_match = re.search(r'^title:\s*["\']?(.+?)["\']?\s*$', fm, re.MULTILINE)
        date_match = re.search(r'^date:\s*(\S+)', fm, re.MULTILINE)
        type_match = re.search(r'^type:\s*(\S+)', fm, re.MULTILINE)
        authors = re.findall(r'name:\s*["\']?([^"\']+)["\']?', fm)
        tags = re.findall(r'^\s*-\s*(\S+)', fm.split("tags:")[-1].split("audience:")[0], re.MULTILINE) if "tags:" in fm else []
        metadata[basename] = {
            "title": title_match.group(1).strip().strip('"\'') if title_match else basename,
            "authors": authors,
            "date": date_match.group(1) if date_match else "",
            "type": type_match.group(1) if type_match else "paper",
            "tags": tags,
            "url": f"/resources/{basename}",
        }

    print(f"Loaded metadata for {len(metadata)} PDF resources")
    return metadata


def extract_pdf_text(pdf_path: Path) -> str:
    """Extract all text from a PDF using pdfplumber."""
    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                pages.append(text)
    return "\n\n".join(pages)


def ingest_body_chunks(pdf_path: Path, meta: dict, enrich: dict, vc, index) -> int:
    """Ingest body chunks for one PDF. Returns number of vectors upserted."""
    basename = pdf_path.name
    title = meta.get("title") or enrich.get("title") or basename.replace(".pdf", "").replace("-", " ")
    authors = enrich.get("all_authors", meta.get("authors", []))
    primary_author = enrich.get("primary_author", authors[0] if authors else "Protocol Institute")
    doc_type = meta.get("type") or enrich.get("doc_type", "paper")
    tags = meta.get("tags") or enrich.get("tags", [])
    summary = enrich.get("summary", "")
    categories = enrich.get("categories", [])

    print(f"  Extracting text...")
    raw = extract_pdf_text(pdf_path)
    if not raw.strip():
        print(f"  Warning: no text extracted")
        return 0

    text = clean_text(raw)
    chunks = chunk_text(text)
    print(f"  Chunks: {len(chunks)}")

    authors_str = ", ".join(authors) if authors else "Protocol Institute"
    if summary:
        prefix = (
            f"Title: {title}\n"
            f"Authors: {authors_str}\n"
            f"Type: {doc_type}\n"
            f"Summary: {summary}\n\n"
        )
    else:
        prefix = (
            f"Title: {title}\n"
            f"Authors: {authors_str}\n"
            f"Type: {doc_type}\n\n"
        )

    prefixed_chunks = [prefix + c for c in chunks]
    vectors = embed_chunks(prefixed_chunks, vc)

    vector_meta = {
        "source": "pdf",
        "namespace": NAMESPACE,
        "chunk_type": "body",
        "title": title,
        "authors": authors,
        "primary_author": primary_author,
        "date": meta.get("date") or enrich.get("date", ""),
        "type": doc_type,
        "tags": tags,
        "url": enrich.get("url") or meta.get("url", f"/resources/{basename}"),
        "summary": summary[:500] if summary else "",
        "enriched_categories": categories,
        "access_level": "public",
    }

    count = upsert_chunks(chunks, vectors, vector_meta, index, namespace=NAMESPACE)
    print(f"  Upserted: {count} body chunks")
    return count


def ingest_doc_summaries(pdf_paths: list, metadata_map: dict, enriched: dict, vc, index) -> int:
    """Embed one doc_summary vector per PDF. Returns count upserted."""
    texts, records = [], []

    for pdf_path in pdf_paths:
        basename = pdf_path.name
        meta = metadata_map.get(basename, {})
        enrich = enriched.get(basename, {})
        if not enrich.get("summary"):
            print(f"  [skip — no enrichment] {basename}")
            continue

        title = meta.get("title") or enrich.get("title") or basename.replace(".pdf", "").replace("-", " ")
        authors = enrich.get("all_authors", meta.get("authors", []))
        primary_author = enrich.get("primary_author", authors[0] if authors else "Protocol Institute")
        date = meta.get("date") or enrich.get("date", "")
        doc_type = meta.get("type") or enrich.get("doc_type", "paper")
        tags = meta.get("tags") or enrich.get("tags", [])
        categories = enrich.get("categories", [])
        summary = enrich.get("summary", "")

        authors_str = ", ".join(authors) if authors else "Protocol Institute"
        text = (
            f"Title: {title}\n"
            f"Authors: {authors_str}\n"
            f"Date: {date}\n"
            f"Type: {doc_type}\n"
            f"Tags: {', '.join(tags)}\n"
            f"Categories: {', '.join(categories)}\n"
            f"Summary: {summary}"
        )
        texts.append(text)

        stem = basename.replace(".pdf", "")
        records.append({
            "id": f"{stem}__doc_summary",
            "metadata": {
                "source": "pdf",
                "namespace": NAMESPACE,
                "chunk_type": "doc_summary",
                "title": title,
                "authors": authors,
                "primary_author": primary_author,
                "date": date,
                "type": doc_type,
                "tags": tags,
                "url": enrich.get("url") or meta.get("url", f"/resources/{basename}"),
                "enriched_categories": categories,
                "summary": summary[:500],
                "access_level": "public",
                "text": text[:1000],
            },
        })
        print(f"  {basename}")

    print(f"Doc summary vectors: {len(records)}")
    if not records:
        return 0

    vectors = embed_chunks(texts, vc)
    pinecone_records = [
        {"id": r["id"], "values": v, "metadata": r["metadata"]}
        for r, v in zip(records, vectors)
    ]
    for i in range(0, len(pinecone_records), PINECONE_BATCH):
        index.upsert(vectors=pinecone_records[i:i + PINECONE_BATCH], namespace=NAMESPACE)

    print(f"Upserted: {len(pinecone_records)} doc_summary vectors")
    return len(pinecone_records)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", help="Ingest a single PDF (basename or path)")
    parser.add_argument("--type", choices=["body", "summaries", "all"], default="all",
                        help="Which vector types to ingest (default: all)")
    args = parser.parse_args()

    vc = get_voyage_client()
    index = get_pinecone_index()
    metadata_map = load_resource_metadata()

    enriched = {}
    if ENRICHED_META_PATH.exists():
        enriched = json.loads(ENRICHED_META_PATH.read_text())
        print(f"Loaded enrichment for {len(enriched)} PDFs")
    else:
        print("Warning: sources/pdfs/enriched_meta.json not found. Run enrich_pdfs.py first.")

    if args.pdf:
        p = Path(args.pdf)
        pdf_paths = [p if p.is_absolute() else PDF_DIR / p.name]
    else:
        pdf_paths = sorted(PDF_DIR.glob("*.pdf"))
        if not pdf_paths:
            print(f"No PDFs found in {PDF_DIR}.")
            sys.exit(1)

    total = 0

    if args.type in ("body", "all"):
        print("\n=== Body chunks ===")
        for pdf_path in pdf_paths:
            print(f"\n[{pdf_path.name}]")
            meta = metadata_map.get(pdf_path.name, {
                "title": pdf_path.stem.replace("-", " "),
                "authors": [], "date": "", "type": "paper", "tags": [],
                "url": f"/resources/{pdf_path.name}",
            })
            enrich = enriched.get(pdf_path.name, {})
            total += ingest_body_chunks(pdf_path, meta, enrich, vc, index)

    if args.type in ("summaries", "all"):
        print("\n=== Doc summaries ===")
        total += ingest_doc_summaries(pdf_paths, metadata_map, enriched, vc, index)

    print(f"\nDone. Total vectors: {total}")


if __name__ == "__main__":
    main()
