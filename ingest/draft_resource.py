"""
Draft resource stub in protocolized-website for PDFs enriched by c3po.

For each entry in sources/pdfs/enriched_meta.json that has no matching resource
Markdown file in protocolized-website/src/content/resources/, writes a stub with
description and tags pre-filled from c3po enrichment.

Fields left for human to fill in: date, authors, audience, file (R2 URL).

Usage:
    python3 ingest/draft_resource.py
    python3 ingest/draft_resource.py --dry-run
    python3 ingest/draft_resource.py --basename durable-ai-adoption.pdf
"""

import argparse
import json
import re
import sys
from pathlib import Path

C3PO_ROOT    = Path(__file__).resolve().parent.parent
ENRICHED_META = C3PO_ROOT / "sources" / "pdfs" / "enriched_meta.json"
WEBSITE_ROOT = C3PO_ROOT.parent / "protocolized-website"
RESOURCES_DIR = WEBSITE_ROOT / "src" / "content" / "resources"

CATEGORY_TAGS = {
    "protocol-theory":   "protocols",
    "protocol-watching": "protocol-watching",
    "protocol-fiction":  "fiction",
    "governance":        "governance",
    "technology-ai":     "technology",
    "research-report":   "research",
    "memory-archival":   "memory",
    "organizations":     "organizations",
    "interview":         "interview",
}
SKIP_CATEGORIES = {"announcement", "editorial"}


def slugify(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    return re.sub(r"-+", "-", s).strip("-")[:80]


def basename_to_slug(basename: str, title: str | None) -> str:
    """Derive a resource slug from a PDF basename or title."""
    if title:
        return slugify(title)
    # Strip leading ID prefix (e.g. "02-AUSTIN-2023-12-13.pdf" → "austin-2023-12-13")
    stem = Path(basename).stem
    stem = re.sub(r"^\d+-", "", stem)  # remove leading NN- prefix
    return slugify(stem)


def map_tags(categories: list[str]) -> list[str]:
    tags = set(["protocols"])
    for cat in categories:
        if cat in CATEGORY_TAGS:
            tags.add(CATEGORY_TAGS[cat])
        elif cat not in SKIP_CATEGORIES:
            tags.add(cat)
    return sorted(tags)


def yaml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


def make_stub(basename: str, entry: dict) -> str:
    title   = entry.get("title") or basename_to_slug(basename, None).replace("-", " ").title()
    summary = entry.get("summary", "")
    cats    = entry.get("categories", [])
    tags    = map_tags(cats)
    doc_type = entry.get("doc_type", "paper")

    lines = [
        "---",
        f"title: {yaml_str(title)}",
        f"type: {doc_type}",
        "authors:",
        "  - name: \"\"  # TODO: fill in",
        "date: \"\"  # TODO: fill in (YYYY-MM-DD)",
        f"description: {yaml_str(summary) if summary else '\"\"  # TODO: fill in'}",
        "tags:",
    ] + [f"  - {t}" for t in tags] + [
        "audience:",
        "  - researcher",
        "  - practitioner",
        "featured: false",
        "file: \"\"  # TODO: https://files.protocolized.io/...",
        "---",
        "",
    ]
    return "\n".join(lines)


def find_existing_slugs_by_file() -> set[str]:
    """Return set of PDF basenames that already appear in a resource file: field."""
    seen = set()
    for path in RESOURCES_DIR.glob("*.md"):
        text = path.read_text(encoding="utf-8")
        m = re.search(r'^file:\s*"?https://files\.protocolized\.io/([^"?\s]+)', text, re.MULTILINE)
        if m:
            seen.add(m.group(1).split("/")[-1])
    return seen


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--basename", help="Process a single PDF basename")
    args = parser.parse_args()

    if not ENRICHED_META.exists():
        print(f"ERROR: {ENRICHED_META} not found", file=sys.stderr)
        sys.exit(1)
    if not RESOURCES_DIR.exists():
        print(f"ERROR: {RESOURCES_DIR} not found", file=sys.stderr)
        sys.exit(1)

    enriched: dict = json.loads(ENRICHED_META.read_text())
    existing_files = find_existing_slugs_by_file()
    existing_slugs = {p.stem for p in RESOURCES_DIR.glob("*.md")}

    targets = [args.basename] if args.basename else sorted(enriched.keys())

    created = skipped = 0
    for basename in targets:
        entry = enriched.get(basename)
        if not entry:
            print(f"  [SKIP] {basename} — not in enriched_meta")
            continue
        if entry.get("deprecated"):
            skipped += 1
            continue
        if basename in existing_files:
            skipped += 1
            continue

        title = entry.get("title")
        slug  = basename_to_slug(basename, title)
        if slug in existing_slugs:
            print(f"  [SKIP] {basename} — slug '{slug}' already exists")
            skipped += 1
            continue

        stub = make_stub(basename, entry)
        out  = RESOURCES_DIR / f"{slug}.md"
        print(f"  [CREATE] {slug}.md  ← {basename}")
        if not args.dry_run:
            out.write_text(stub, encoding="utf-8")
            existing_slugs.add(slug)
        created += 1

    print(f"\n{'DRY RUN — ' if args.dry_run else ''}Done.")
    print(f"  Created: {created}  Skipped/existing: {skipped}")
    if not args.dry_run and created:
        print(f"\nNext steps:")
        print(f"  1. Fill in date, authors, file URL in each new stub")
        print(f"  2. python3 scripts/migrate-to-d1.py --remote  (from protocolized-website/)")


if __name__ == "__main__":
    main()
