"""
Enrich PDFs with Claude Haiku: summary, categories, author resolution.

Reads PDFs from data/pdfs/ and resource metadata from protocolized-website.
Outputs sources/pdfs/enriched_meta.json (keyed by PDF basename).

Run this before ingest_pdfs.py.

Usage:
    python3 ingest/enrich_pdfs.py
    python3 ingest/enrich_pdfs.py --dry-run
    python3 ingest/enrich_pdfs.py --pdf some-file.pdf
    python3 ingest/enrich_pdfs.py --force
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import pdfplumber
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
from utils import clean_text

load_dotenv()

PDF_DIR = Path(__file__).parent.parent / "data" / "pdfs"
RESOURCES_DIR = (
    Path(__file__).parent.parent.parent
    / "protocolized-website"
    / "src"
    / "content"
    / "resources"
)
OUT_PATH = Path("sources/pdfs/enriched_meta.json")

# Shared category vocabulary — same as Substack for cross-corpus query consistency
CATEGORY_VOCAB = [
    "protocol-fiction",
    "protocol-theory",
    "protocol-watching",
    "editorial",
    "research-report",
    "technology-ai",
    "governance",
    "announcement",
    "interview",
    "memory-archival",
    "organizations",
]

SYSTEM_PROMPT = f"""You are a metadata enrichment assistant for the Protocol Institute research corpus.
Given a document title, authors, type, tags, and opening text, return a JSON object with:
- "summary": exactly two sentences. Be concrete about the specific argument, framework, or contribution. No vague generalities.
- "categories": array of 2-4 tags from this fixed vocabulary: {json.dumps(CATEGORY_VOCAB)}
- "primary_author": the main author's name as a clean string. Return "Protocol Institute" if no clear individual author.

Respond with only the JSON object, no other text."""


def load_resource_metadata() -> dict:
    """Load all resource markdowns. Returns {{pdf_basename: {{title, authors, date, type, tags}}}}."""
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
        }
    return metadata


def extract_opening(pdf_path: Path, max_chars: int = 1500) -> str:
    """Extract opening text from a PDF (first pages until max_chars)."""
    pages = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    pages.append(text)
                if sum(len(p) for p in pages) >= max_chars:
                    break
    except Exception as e:
        print(f"  Warning: pdfplumber error: {e}")
    return clean_text("\n\n".join(pages))[:max_chars]


def enrich_pdf(basename: str, meta: dict, pdf_path: Path, client) -> dict:
    """Call Haiku to enrich one PDF. Returns enrichment dict."""
    opening = extract_opening(pdf_path)
    title = meta.get("title", basename)
    authors = meta.get("authors", [])
    doc_type = meta.get("type", "paper")
    tags = meta.get("tags", [])

    user_content = f"""Title: {title}
Authors: {', '.join(authors) or '(unknown)'}
Type: {doc_type}
Tags: {', '.join(tags) or '(none)'}

Opening text:
{opening}"""

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": user_content}],
        system=SYSTEM_PROMPT,
    )
    raw = resp.content[0].text.strip()
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        result = json.loads(m.group()) if m else {}

    if authors and "primary_author" not in result:
        result["primary_author"] = authors[0]
    elif "primary_author" not in result:
        result["primary_author"] = "Protocol Institute"
    result["all_authors"] = authors if authors else ["Protocol Institute"]
    return result


def load_existing() -> dict:
    if OUT_PATH.exists():
        return json.loads(OUT_PATH.read_text())
    return {}


def save(enriched: dict):
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(enriched, indent=2, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--pdf", help="Enrich only this PDF basename (e.g. some-paper.pdf)")
    parser.add_argument("--force", action="store_true", help="Re-enrich all, ignoring cache")
    args = parser.parse_args()

    resource_meta = load_resource_metadata()
    print(f"Loaded metadata for {len(resource_meta)} PDF resources")

    existing = load_existing()

    pdfs = sorted(PDF_DIR.glob("*.pdf"))
    if not pdfs:
        print(f"No PDFs found in {PDF_DIR}. Copy PDFs there first.")
        sys.exit(1)

    candidates = []
    for pdf_path in pdfs:
        basename = pdf_path.name
        if args.pdf and basename != args.pdf:
            continue
        if not args.force and not args.pdf and basename in existing:
            continue
        candidates.append((basename, pdf_path, resource_meta.get(basename, {})))

    print(f"PDFs to enrich: {len(candidates)}  (already done: {len(existing)})")
    if args.dry_run:
        for basename, _, meta in candidates[:10]:
            print(f"  {basename}  title={meta.get('title', '?')}  authors={meta.get('authors', [])}")
        if len(candidates) > 10:
            print(f"  ... and {len(candidates) - 10} more")
        return

    try:
        import anthropic
    except ImportError:
        print("Install anthropic: pip install anthropic")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    errors = []

    for i, (basename, pdf_path, meta) in enumerate(candidates):
        print(f"[{i+1}/{len(candidates)}] {basename}")
        try:
            result = enrich_pdf(basename, meta, pdf_path, client)
            existing[basename] = result
            print(f"  author={result.get('primary_author')}  cats={result.get('categories')}")
            print(f"  {result.get('summary', '')[:100]}...")
            if i % 10 == 9:
                save(existing)
                print("  (saved checkpoint)")
            if i < len(candidates) - 1:
                time.sleep(0.2)
        except Exception as e:
            print(f"  ERROR: {e}")
            errors.append(basename)

    save(existing)
    print(f"\nDone. Enriched: {len(candidates) - len(errors)}  Errors: {len(errors)}")
    if errors:
        print(f"Failed: {errors}")


if __name__ == "__main__":
    main()
