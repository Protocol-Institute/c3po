"""
Extract protocol-specific terms and definitions from key PI papers via Haiku.

Reads a curated set of core theoretical PDFs, sends each to Claude Haiku with
a structured prompt to extract defined or coined terms, then merges and deduplicates
into a draft lexicon for human review.

Output: sources/lexicon_draft.json
  {
    "term": {
      "term": "...",
      "definition": "...",
      "source": "Paper Title",
      "source_slug": "pdf-filename",
      "context": "verbatim sentence or phrase where term is defined"
    }, ...
  }

Usage:
    python3 ingest/extract_lexicon.py
    python3 ingest/extract_lexicon.py --dry-run
    python3 ingest/extract_lexicon.py --slug The-Unreasonable-Sufficiency-of-Protocols
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import anthropic
import pdfplumber
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
from utils import clean_text

load_dotenv()

PDF_DIR    = Path("data/pdfs")
OUTPUT     = Path("sources/lexicon_draft.json")
MODEL      = "claude-haiku-4-5-20251001"
MAX_CHARS  = 40000   # ~10k tokens — enough for most papers

# Core theoretical papers most likely to define PI-specific terms
TARGET_SLUGS = [
    "The-Unreasonable-Sufficiency-of-Protocols",
    "Introduction-to-the-Protocol-Reader-Venkatesh-Rao",
    "The-Fundamentals-of-Protocol-Systems-Angela-Walch",
    "Dangerous-Protocols-Nadia-Asparouhova",
    "10-e36-SCHROFF-2-PAGER",           # Protocol Selection Pressures
    "A-Phenomenology-of-Protocols-Janna-Tay",
    "Retrofitting-the-Web-Dorian-Taylor",
    "Addressable-Space-Chenoe-Hart",
    "Atoms-Institutes-Blockchains-Josh-Stark",
    "15-e41-KITTEL-rev-2024-06-12-1448", # Unprotocolized Knowledge
    "Exit-to-Protocol-Shuya-Gong",
    "Safe-New-World-Timber-Stinson-Schroff",
]


def all_pdf_slugs() -> list[str]:
    """Return slugs for every PDF in PDF_DIR."""
    return sorted(p.stem for p in PDF_DIR.glob("*.pdf"))

EXTRACTION_PROMPT = """You are analyzing a Protocol Institute research paper. Extract all terms that are:
- Explicitly defined or coined by the author
- Used in a technical or specialized sense specific to protocol theory
- Key concepts that a reader would need to understand the paper's argument

For each term return:
- term: the exact term or phrase (normalized to lowercase, as it would appear in a glossary)
- definition: a clear 1-2 sentence definition in plain language
- context: a short verbatim excerpt (1-2 sentences) from the paper where the term is defined or first used technically
- source: the paper title

Return a JSON array. Example entry:
{"term": "protocolization", "definition": "The process by which informal coordination becomes encoded in explicit, legible, enforceable protocols.", "context": "We call this shift protocolization: the gradual replacement of tacit coordination by explicit rule structures.", "source": "Paper Title Here"}

Be selective — aim for 10-25 genuinely distinctive terms per paper, not every noun. Skip generic academic vocabulary. Prioritize:
1. Terms coined or given new meaning by this author
2. Technical terms central to the paper's framework
3. Named concepts (e.g. "the formalization ladder", "hardness", "Kafka protocol")

Paper text follows. Return ONLY the JSON array, no other text.

---
"""


def extract_text(pdf_path: Path) -> str:
    try:
        with pdfplumber.open(pdf_path) as doc:
            raw = "\n".join(page.extract_text() or "" for page in doc.pages)
        return clean_text(raw)[:MAX_CHARS]
    except Exception as e:
        print(f"  PDF read error: {e}")
        return ""


def extract_terms(slug: str, client: anthropic.Anthropic, dry_run: bool) -> list[dict]:
    pdf_path = PDF_DIR / f"{slug}.pdf"
    if not pdf_path.exists():
        print(f"  NOT FOUND: {pdf_path}")
        return []

    text = extract_text(pdf_path)
    if len(text.strip()) < 500:
        print(f"  Too little text extracted ({len(text)} chars)")
        return []

    print(f"  Text: {len(text):,} chars")

    if dry_run:
        print(f"  (dry run — skipping Haiku call)")
        return []

    try:
        msg = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            messages=[{"role": "user", "content": EXTRACTION_PROMPT + text}],
        )
        raw = msg.content[0].text.strip()
        # Strip markdown fences if present
        raw = re.sub(r"^```json\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        terms = json.loads(raw)
        print(f"  Extracted {len(terms)} terms  (in:{msg.usage.input_tokens} out:{msg.usage.output_tokens})")
        return terms if isinstance(terms, list) else []
    except json.JSONDecodeError as e:
        print(f"  JSON parse error: {e}")
        return []
    except Exception as e:
        print(f"  Haiku error: {e}")
        return []


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--slug", help="Process a single PDF slug")
    parser.add_argument("--all", action="store_true", help="Process every PDF in data/pdfs/")
    parser.add_argument("--force", action="store_true", help="Re-extract even if slug already in draft")
    args = parser.parse_args()

    client = None if args.dry_run else anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    existing: dict = {}
    if OUTPUT.exists():
        existing = json.loads(OUTPUT.read_text())

    if args.slug:
        slugs = [args.slug]
    elif args.all:
        slugs = all_pdf_slugs()
    else:
        slugs = TARGET_SLUGS

    all_terms: dict = dict(existing)  # term → best entry
    total_new = 0

    for i, slug in enumerate(slugs):
        print(f"\n[{i+1}/{len(slugs)}] {slug}")

        # Check if already extracted (keyed by slug in source field)
        already = [t for t in all_terms.values() if t.get("source_slug") == slug]
        if already and not args.force:
            print(f"  Already have {len(already)} terms from this slug — skip (use --force to re-extract)")
            continue

        terms = extract_terms(slug, client, args.dry_run)

        for t in terms:
            if not isinstance(t, dict) or not t.get("term"):
                continue
            key = t["term"].lower().strip()
            t["source_slug"] = slug
            # Keep longer/better definition if term already seen
            if key not in all_terms or len(t.get("definition", "")) > len(all_terms[key].get("definition", "")):
                all_terms[key] = t
                total_new += 1

        if not args.dry_run and terms:
            OUTPUT.write_text(json.dumps(all_terms, indent=2, ensure_ascii=False))

        time.sleep(1)  # light rate limiting between PDFs

    if not args.dry_run:
        OUTPUT.write_text(json.dumps(all_terms, indent=2, ensure_ascii=False))

    print(f"\n--- Done ---")
    print(f"Total terms in draft: {len(all_terms)}  (new this run: {total_new})")
    print(f"Saved to {OUTPUT}")
    print(f"\nNext: review {OUTPUT}, then run build_lexicon_prompt.py to generate the system prompt block.")


if __name__ == "__main__":
    main()
