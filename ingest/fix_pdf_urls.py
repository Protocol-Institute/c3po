"""
One-off script: fix PDF URLs in Pinecone and delete deprecated PDF vectors.

1. Lists all vectors in the 'pdfs' namespace.
2. Deletes vectors for PDFs marked deprecated in enriched_meta.json.
3. Updates the `url` metadata field for remaining vectors to use
   https://files.protocolized.io/{basename} (matching the protocolized website).

Safe to re-run — idempotent.

Usage:
    source .venv/bin/activate
    python3 ingest/fix_pdf_urls.py [--dry-run]
"""

import argparse
import json
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

C3PO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(C3PO_DIR / "ingest"))
from utils import get_pinecone_index

NAMESPACE = "pdfs"
RESOURCES_DIR = (
    C3PO_DIR.parent / "protocolized-website" / "src" / "content" / "resources"
)
ENRICHED_META_PATH = C3PO_DIR / "sources" / "pdfs" / "enriched_meta.json"
BATCH = 100


def build_url_map() -> dict[str, str]:
    """Return {pdf_basename: full_url} from the protocolized website resource files."""
    url_map = {}
    if not RESOURCES_DIR.exists():
        print(f"Warning: resources dir not found at {RESOURCES_DIR}")
        return url_map
    for md_file in RESOURCES_DIR.glob("*.md"):
        text = md_file.read_text(encoding="utf-8")
        m = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
        if not m:
            continue
        fm = m.group(1)
        fm_match = re.search(
            r'^file:\s*["\']?(https://files\.protocolized\.io/([^"\']+)|/resources/([^"\']+))["\']?',
            fm, re.MULTILINE
        )
        if not fm_match:
            continue
        basename = (fm_match.group(2) or fm_match.group(3)).strip()
        full_url = fm_match.group(1).strip()
        if full_url.startswith("/resources/"):
            full_url = f"https://files.protocolized.io/{basename}"
        url_map[basename] = full_url
    return url_map


def load_deprecated() -> set[str]:
    """Return set of PDF basenames marked deprecated in enriched_meta.json."""
    if not ENRICHED_META_PATH.exists():
        return set()
    meta = json.loads(ENRICHED_META_PATH.read_text())
    return {k for k, v in meta.items() if v.get("deprecated")}


def list_all_ids(index) -> list[str]:
    """List all vector IDs in the pdfs namespace."""
    ids = []
    for id_batch in index.list(namespace=NAMESPACE):
        # Pinecone returns ListItem objects, not plain strings
        for item in id_batch:
            ids.append(item.id if hasattr(item, "id") else item)
    return ids


def fetch_metadata(index, ids: list[str]) -> dict[str, dict]:
    """Fetch metadata for a list of IDs. Returns {id: metadata}."""
    result = {}
    for i in range(0, len(ids), BATCH):
        batch = ids[i:i + BATCH]
        resp = index.fetch(ids=batch, namespace=NAMESPACE)
        for vid, vec in (resp.vectors or {}).items():
            result[vid] = vec.metadata or {}
    return result


def url_from_basename(basename: str, url_map: dict[str, str]) -> str:
    return url_map.get(basename, f"https://files.protocolized.io/{basename}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Print what would happen without writing")
    args = ap.parse_args()

    url_map = build_url_map()
    print(f"URL map: {len(url_map)} entries from website resources")

    deprecated = load_deprecated()
    print(f"Deprecated PDFs: {len(deprecated)}")
    for d in sorted(deprecated):
        print(f"  {d}")

    index = get_pinecone_index()

    print(f"\nListing all vector IDs in namespace '{NAMESPACE}'…")
    all_ids = list_all_ids(index)
    print(f"  {len(all_ids)} vectors found")

    print("Fetching metadata…")
    meta_map = fetch_metadata(index, all_ids)

    # Classify vectors
    # Deprecated PDFs: mark is_cover_letter=True + fix URL (keep vectors for meta retrieval)
    # Non-deprecated: fix URL only
    to_mark_cover_letter = []  # (id, correct_url) — deprecated, needs flag + url fix
    to_update_url = []         # (id, correct_url) — url fix only
    already_ok = 0

    for vid, meta in meta_map.items():
        current_url = meta.get("url", "")

        # Only fix legacy /resources/ URLs; leave external https:// URLs alone
        if not current_url.startswith("/resources/"):
            already_ok += 1
            continue

        basename = current_url.split("/")[-1]
        if not basename.endswith(".pdf"):
            already_ok += 1
            continue

        correct_url = url_from_basename(basename, url_map)
        is_deprecated = basename in deprecated

        if is_deprecated:
            to_mark_cover_letter.append((vid, correct_url))
        else:
            to_update_url.append((vid, correct_url))

    print(f"\nSummary:")
    print(f"  To flag is_cover_letter + fix URL: {len(to_mark_cover_letter)}")
    print(f"  To fix URL only:                   {len(to_update_url)}")
    print(f"  Already correct:                   {already_ok}")

    if args.dry_run:
        if to_mark_cover_letter:
            print(f"\nWould flag {len(to_mark_cover_letter)} cover letter vectors (sample):")
            for vid, url in to_mark_cover_letter[:3]:
                print(f"  {vid[:40]}  → {url}")
        if to_update_url:
            print(f"\nWould update {len(to_update_url)} URLs (sample):")
            for vid, new_url in to_update_url[:5]:
                old_url = meta_map[vid].get("url", "(none)")
                print(f"  {vid[:30]}…  {old_url} → {new_url}")
        print("\nDry run — nothing written.")
        return

    # Flag cover letters + fix their URLs
    if to_mark_cover_letter:
        print(f"\nFlagging {len(to_mark_cover_letter)} cover letter vectors…")
        for i, (vid, correct_url) in enumerate(to_mark_cover_letter):
            index.update(id=vid, set_metadata={"is_cover_letter": True, "url": correct_url}, namespace=NAMESPACE)
            if (i + 1) % 20 == 0:
                print(f"  {i + 1}/{len(to_mark_cover_letter)}…")
        print("  Done.")

    # Fix URLs for non-deprecated vectors
    if to_update_url:
        print(f"\nUpdating {len(to_update_url)} vector URLs…")
        for i, (vid, new_url) in enumerate(to_update_url):
            index.update(id=vid, set_metadata={"url": new_url}, namespace=NAMESPACE)
            if (i + 1) % 50 == 0:
                print(f"  {i + 1}/{len(to_update_url)}…")
        print("  Done.")

    total = len(to_mark_cover_letter) + len(to_update_url)
    print(f"\nComplete. Flagged {len(to_mark_cover_letter)} cover letter(s), updated {len(to_update_url)} URL(s), {total} total.")


if __name__ == "__main__":
    main()
