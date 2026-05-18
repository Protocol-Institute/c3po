"""
Source bibliographic references via Semantic Scholar API.

For every reference in scored_refs.json, fetches:
  - Abstract (always)
  - Open-access PDF URL (when available)
  - Canonical title/authors/year from S2 (replaces garbled OCR versions)

Downloads available PDFs to sources/bibliography/pdfs/.
Writes sources/bibliography/sourced_refs.json.

API: https://api.semanticscholar.org/graph/v1/
Rate limit: 100 req / 5 min without key (~3s between requests).

Usage:
    python3 ingest/fetch_refs.py
    python3 ingest/fetch_refs.py --min-score 0   # all refs (default)
    python3 ingest/fetch_refs.py --min-score 2   # only relevant+core
    python3 ingest/fetch_refs.py --dry-run
    python3 ingest/fetch_refs.py --id REF_ID      # single ref
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

SCORED_PATH = Path("sources/bibliography/scored_refs.json")
SOURCED_PATH = Path("sources/bibliography/sourced_refs.json")
PDF_DIR = Path("sources/bibliography/pdfs")

S2_BASE = "https://api.semanticscholar.org/graph/v1"
S2_FIELDS = "title,authors,year,abstract,openAccessPdf,externalIds,publicationVenue,citationCount"
REQUEST_DELAY = 4.5   # seconds between S2 requests (100 req/5min = 3s min; 4.5s adds margin)


# ---------------------------------------------------------------------------
# Semantic Scholar helpers
# ---------------------------------------------------------------------------

def s2_get(url: str, delay: float = REQUEST_DELAY) -> dict | None:
    """GET a Semantic Scholar URL, return JSON or None on failure."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "c3po-bibliography/1.0 (vgururao@gmail.com)"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        if e.code == 429:
            print(f"    Rate limited — waiting 30s")
            time.sleep(30)
            return s2_get(url, delay)
        print(f"    HTTP {e.code}: {url}")
        return None
    except Exception as e:
        print(f"    Error fetching {url}: {e}")
        return None
    finally:
        time.sleep(delay)


def lookup_by_doi(doi: str) -> dict | None:
    doi_clean = doi.strip().lstrip("https://doi.org/").lstrip("doi:")
    url = f"{S2_BASE}/paper/DOI:{urllib.parse.quote(doi_clean)}?fields={S2_FIELDS}"
    return s2_get(url)


def lookup_by_arxiv(arxiv_id: str) -> dict | None:
    url = f"{S2_BASE}/paper/ARXIV:{arxiv_id}?fields={S2_FIELDS}"
    return s2_get(url)


def search_by_title(title: str) -> dict | None:
    """Search S2 by title, return best match if title similarity is high."""
    query = urllib.parse.quote(title[:200])
    url = f"{S2_BASE}/paper/search?query={query}&limit=3&fields={S2_FIELDS}"
    data = s2_get(url)
    if not data or not data.get("data"):
        return None
    # Pick the first result if title similarity is reasonable
    best = data["data"][0]
    best_title = (best.get("title") or "").lower()
    query_title = title.lower()
    # Simple overlap check — at least 60% of query words appear in result title
    query_words = set(re.findall(r"\w+", query_title))
    match_words = set(re.findall(r"\w+", best_title))
    if not query_words:
        return None
    overlap = len(query_words & match_words) / len(query_words)
    if overlap >= 0.55:
        return best
    return None


def extract_arxiv_id(url_or_doi: str) -> str | None:
    if not url_or_doi:
        return None
    m = re.search(r"arxiv\.org/(abs|pdf)/([0-9]+\.[0-9]+)", url_or_doi or "")
    return m.group(2) if m else None


def download_pdf(url: str, dest: Path) -> bool:
    """Download a PDF to dest. Returns True on success."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "c3po-bibliography/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            content = resp.read()
        if len(content) < 1000:  # suspiciously small
            return False
        # Check it's actually a PDF
        if not content.startswith(b"%PDF"):
            return False
        dest.write_bytes(content)
        return True
    except Exception as e:
        print(f"    PDF download failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Process a single reference
# ---------------------------------------------------------------------------

def process_ref(ref: dict, pdf_dir: Path) -> dict:
    """Attempt S2 lookup + optional PDF download. Returns enriched ref dict."""
    result = {**ref, "s2_status": "not_found", "abstract": None,
              "s2_paper_id": None, "s2_citation_count": None,
              "s2_title": None, "s2_authors": None,
              "oa_pdf_url": None, "local_pdf": None}

    s2 = None

    # Strategy 1: DOI
    if ref.get("doi"):
        s2 = lookup_by_doi(ref["doi"])

    # Strategy 2: arXiv ID from URL or DOI field
    if not s2 and ref.get("url"):
        arxiv_id = extract_arxiv_id(ref["url"])
        if arxiv_id:
            s2 = lookup_by_arxiv(arxiv_id)

    # Strategy 3: Title search
    if not s2 and ref.get("title"):
        s2 = search_by_title(ref["title"])

    if not s2:
        result["s2_status"] = "not_found"
        return result

    # Found it — extract fields
    result["s2_status"] = "found"
    result["s2_paper_id"] = s2.get("paperId")
    result["s2_citation_count"] = s2.get("citationCount")
    result["abstract"] = s2.get("abstract")
    if s2.get("title"):
        result["s2_title"] = s2["title"]
    if s2.get("authors"):
        result["s2_authors"] = [a.get("name") for a in s2["authors"]]
    if s2.get("year") and not result.get("year"):
        result["year"] = s2["year"]
    venue = s2.get("publicationVenue") or {}
    if venue.get("name") and not result.get("venue"):
        result["venue"] = venue["name"]

    oa = s2.get("openAccessPdf") or {}
    oa_url = oa.get("url")
    if oa_url:
        result["oa_pdf_url"] = oa_url
        result["s2_status"] = "found_oa"

    # Try downloading PDF
    if oa_url and not result.get("local_pdf"):
        pdf_path = pdf_dir / f"{ref['_id']}.pdf"
        if not pdf_path.exists():
            print(f"    Downloading PDF...")
            ok = download_pdf(oa_url, pdf_path)
            if ok:
                result["local_pdf"] = str(pdf_path)
                result["s2_status"] = "pdf_downloaded"
                print(f"    PDF saved ({pdf_path.stat().st_size // 1024}KB)")
        else:
            result["local_pdf"] = str(pdf_path)
            result["s2_status"] = "pdf_downloaded"

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-score", type=int, default=0, help="Minimum relevance score to process (default: 0 = all)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--id", help="Process a single ref by _id")
    parser.add_argument("--force", action="store_true", help="Re-fetch even if already sourced")
    args = parser.parse_args()

    if not SCORED_PATH.exists():
        print(f"ERROR: {SCORED_PATH} not found. Run mine_bibliography.py first.")
        sys.exit(1)

    PDF_DIR.mkdir(parents=True, exist_ok=True)

    scored = json.loads(SCORED_PATH.read_text())
    sourced: dict = {}
    if SOURCED_PATH.exists():
        sourced = json.loads(SOURCED_PATH.read_text())

    # Build target list
    if args.id:
        targets = [scored[args.id]] if args.id in scored else []
    else:
        targets = [r for r in scored.values() if r["relevance_score"] >= args.min_score]

    already_done = [r for r in targets if r["_id"] in sourced and not args.force]
    targets = [r for r in targets if r["_id"] not in sourced or args.force]

    print(f"Total scored refs: {len(scored)}")
    print(f"Target (score >= {args.min_score}): {len(targets) + len(already_done)}")
    print(f"Already sourced: {len(already_done)}")
    print(f"To process: {len(targets)}")

    if args.dry_run:
        for r in targets[:15]:
            print(f"  [{r['relevance_score']}] {r['title'][:70]}")
        return

    found = pdf_downloaded = not_found = 0

    for i, ref in enumerate(targets):
        score_label = {3: "core", 2: "relevant", 1: "adjacent", 0: "incidental"}.get(ref["relevance_score"], "?")
        print(f"[{i+1}/{len(targets)}] [{ref['relevance_score']} {score_label}] {ref['title'][:60]}...")

        result = process_ref(ref, PDF_DIR)
        sourced[ref["_id"]] = result

        status = result["s2_status"]
        if status == "pdf_downloaded":
            pdf_downloaded += 1
            print(f"  -> PDF downloaded, abstract: {len(result.get('abstract') or '')} chars")
        elif status in ("found", "found_oa"):
            found += 1
            ab = result.get("abstract") or ""
            print(f"  -> Found on S2, abstract: {len(ab)} chars")
        else:
            not_found += 1
            print(f"  -> Not found on S2")

        # Checkpoint every 10
        if (i + 1) % 10 == 0:
            SOURCED_PATH.write_text(json.dumps(sourced, indent=2, ensure_ascii=False))

    SOURCED_PATH.write_text(json.dumps(sourced, indent=2, ensure_ascii=False))

    print(f"\n--- Summary ---")
    print(f"PDF downloaded:  {pdf_downloaded}")
    print(f"Abstract only:   {found}")
    print(f"Not found on S2: {not_found}")
    print(f"Saved to {SOURCED_PATH}")


if __name__ == "__main__":
    main()
