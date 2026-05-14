"""
Ingest PDFs from data/pdfs/ into Pinecone.

Also reads resource metadata from protocolized-website/src/content/resources/
to enrich each PDF chunk with title, authors, date, type, and tags.

Usage:
    python3 ingest/ingest_pdfs.py [--pdf path/to/specific.pdf]
"""

import argparse
import json
import os
import sys
from pathlib import Path

import pdfplumber
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
from utils import clean_text, chunk_text, embed_chunks, upsert_chunks
from utils import get_voyage_client, get_pinecone_index

load_dotenv()

PDF_DIR = Path(__file__).parent.parent / "data" / "pdfs"
# Path to protocolized-website resource markdowns for metadata enrichment
RESOURCES_DIR = (
    Path(__file__).parent.parent.parent
    / "protocolized-website"
    / "src"
    / "content"
    / "resources"
)


def load_resource_metadata() -> dict[str, dict]:
    """
    Read all resource markdown files and index by the PDF filename they reference.
    Returns: {pdf_basename: {title, authors, date, type, tags, url}}
    """
    import re

    metadata = {}
    if not RESOURCES_DIR.exists():
        print(f"Warning: resources dir not found at {RESOURCES_DIR}")
        return metadata

    for md_file in RESOURCES_DIR.glob("*.md"):
        text = md_file.read_text(encoding="utf-8")
        # Extract frontmatter between --- delimiters
        match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
        if not match:
            continue
        fm = match.group(1)

        file_match = re.search(r'^file:\s*["\']?(/resources/([^"\']+))["\']?', fm, re.MULTILINE)
        if not file_match:
            continue
        pdf_basename = file_match.group(2).strip()

        title_match = re.search(r'^title:\s*["\']?(.+?)["\']?\s*$', fm, re.MULTILINE)
        date_match = re.search(r'^date:\s*(\S+)', fm, re.MULTILINE)
        type_match = re.search(r'^type:\s*(\S+)', fm, re.MULTILINE)

        authors = re.findall(r'name:\s*["\']?([^"\']+)["\']?', fm)
        tags = re.findall(r'^\s*-\s*(\S+)', fm.split("tags:")[-1].split("audience:")[0], re.MULTILINE) if "tags:" in fm else []

        metadata[pdf_basename] = {
            "title": title_match.group(1).strip().strip('"\'') if title_match else pdf_basename,
            "authors": authors,
            "date": date_match.group(1) if date_match else "",
            "type": type_match.group(1) if type_match else "paper",
            "tags": tags,
            "url": f"/resources/{pdf_basename}",
            "source": "pdf",
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


def ingest_pdf(pdf_path: Path, metadata_map: dict, vc, index) -> int:
    """Ingest a single PDF. Returns number of chunks upserted."""
    basename = pdf_path.name
    meta = metadata_map.get(basename, {
        "title": basename.replace(".pdf", "").replace("-", " "),
        "authors": [],
        "date": "",
        "type": "paper",
        "tags": [],
        "url": f"/resources/{basename}",
        "source": "pdf",
    })

    print(f"  Extracting: {basename}")
    raw = extract_pdf_text(pdf_path)
    if not raw.strip():
        print(f"  Warning: no text extracted from {basename}")
        return 0

    text = clean_text(raw)
    chunks = chunk_text(text)
    print(f"  Chunks: {len(chunks)}")

    vectors = embed_chunks(chunks, vc)
    count = upsert_chunks(chunks, vectors, meta, index)
    print(f"  Upserted: {count}")
    return count


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", help="Ingest a single PDF (path)")
    args = parser.parse_args()

    vc = get_voyage_client()
    index = get_pinecone_index()
    metadata_map = load_resource_metadata()

    if args.pdf:
        pdfs = [Path(args.pdf)]
    else:
        pdfs = sorted(PDF_DIR.glob("*.pdf"))
        if not pdfs:
            print(f"No PDFs found in {PDF_DIR}. Copy PDFs there first.")
            sys.exit(1)

    total = 0
    for pdf_path in pdfs:
        print(f"\n[{pdf_path.name}]")
        total += ingest_pdf(pdf_path, metadata_map, vc, index)

    print(f"\nDone. Total chunks upserted: {total}")


if __name__ == "__main__":
    main()
