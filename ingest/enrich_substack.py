"""
Enrich Protocolized posts with Claude Haiku: summary, categories, author resolution.

Usage:
    python3 ingest/enrich_substack.py --export-dir data/substack
    python3 ingest/enrich_substack.py --export-dir data/substack --dry-run
    python3 ingest/enrich_substack.py --export-dir data/substack --slug some-slug  # single post
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
from utils import clean_text

load_dotenv()

API_METADATA_PATH = Path("sources/substack/api_metadata.json")
COLLECTIONS_PATH = Path("sources/substack/collections.json")
OUT_PATH = Path("sources/substack/enriched_meta.json")

# Resolve display handles to real names
AUTHOR_ALIASES = {
    "Thing Party": "Elizabeth Maher",
    "Sachin": "Sachin Benny",
    "Spencer Nitkey - Writer": "Spencer Nitkey",
    "Marie-Hélène Lebeault - Author": "Marie-Hélène Lebeault",
    "Protocolized": None,   # publication byline — not a person
    "Venkatesh Rao": "Venkatesh Rao",
}

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

SYSTEM_PROMPT = f"""You are a metadata enrichment assistant for Protocolized, a magazine about protocol science.
Given a post title, subtitle, bylines, and opening text, return a JSON object with:
- "summary": exactly two sentences. Be concrete about the specific argument, story, or contribution. No vague generalities.
- "categories": array of 2-4 tags from this fixed vocabulary: {json.dumps(CATEGORY_VOCAB)}
- "primary_author": the main non-editorial author's name as a clean string (resolve real names from handles). Return "Protocolized" if it is purely an editorial post with no guest author.

Respond with only the JSON object, no other text."""


def plain_text_from_html(html_bytes: bytes, max_chars: int = 1200) -> str:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("Install beautifulsoup4: pip install beautifulsoup4")
        sys.exit(1)
    soup = BeautifulSoup(html_bytes.decode("utf-8", errors="ignore"), "html.parser")
    for tag in soup.find_all(["figure", "figcaption", "button", "svg", "script", "style"]):
        tag.decompose()
    text = re.sub(r'\s+', ' ', soup.get_text()).strip()
    return text[:max_chars]


def resolve_authors(bylines: list[dict]) -> list[str]:
    """Return list of real author names, excluding the Protocolized publication byline."""
    names = []
    for b in bylines:
        name = b.get("name", "")
        resolved = AUTHOR_ALIASES.get(name, name)
        if resolved and resolved != "Protocolized":
            if resolved not in names:
                names.append(resolved)
    return names


def enrich_post(slug: str, title: str, subtitle: str, bylines: list[dict],
                html_path: Path, client) -> dict:
    """Call Haiku to enrich one post. Returns enrichment dict."""
    opening = ""
    if html_path.exists():
        opening = clean_text(plain_text_from_html(html_path.read_bytes()))

    author_names = resolve_authors(bylines)
    byline_str = ", ".join(b.get("name", "") for b in bylines if b.get("name") != "Protocolized")

    user_content = f"""Title: {title}
Subtitle: {subtitle or '(none)'}
Bylines: {byline_str or 'Protocolized (editorial)'}

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
        # Try to extract JSON if model added surrounding text
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        result = json.loads(m.group()) if m else {}

    # Override primary_author with alias-resolved version if we have a clear answer
    if author_names:
        result["primary_author"] = author_names[0]
    elif "primary_author" not in result:
        result["primary_author"] = "Protocolized"

    result["all_authors"] = author_names if author_names else ["Protocolized"]
    return result


def load_existing() -> dict:
    if OUT_PATH.exists():
        return json.loads(OUT_PATH.read_text())
    return {}


def save(enriched: dict):
    OUT_PATH.write_text(json.dumps(enriched, indent=2, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--export-dir", required=True, help="Path to unzipped Substack export")
    parser.add_argument("--dry-run", action="store_true", help="Preview without API calls")
    parser.add_argument("--slug", help="Enrich only this slug (re-enriches even if cached)")
    parser.add_argument("--force", action="store_true", help="Re-enrich all posts")
    args = parser.parse_args()

    export_dir = Path(args.export_dir)
    posts_dir = export_dir / "posts" if (export_dir / "posts").exists() else export_dir

    api_meta = json.loads(API_METADATA_PATH.read_text()) if API_METADATA_PATH.exists() else []
    api_by_slug = {p["slug"]: p for p in api_meta}

    existing = load_existing()

    # Determine which posts to process
    html_files = sorted(posts_dir.glob("*.html"))
    candidates = []
    for html_path in html_files:
        if "." not in html_path.stem:
            continue
        post_id, slug = html_path.stem.split(".", 1)
        if not post_id.isdigit():
            continue
        if args.slug and slug != args.slug:
            continue
        if not args.force and not args.slug and slug in existing:
            continue
        api_data = api_by_slug.get(slug, {})
        if api_data.get("type") == "podcast":
            continue
        candidates.append((slug, html_path, api_data))

    print(f"Posts to enrich: {len(candidates)}")
    if args.dry_run:
        for slug, _, api_data in candidates[:10]:
            print(f"  {slug}  authors={[b.get('name') for b in api_data.get('publishedBylines',[])]}")
        if len(candidates) > 10:
            print(f"  ... and {len(candidates)-10} more")
        return

    try:
        import anthropic
    except ImportError:
        print("Install anthropic: pip install anthropic")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    errors = []

    for i, (slug, html_path, api_data) in enumerate(candidates):
        title = api_data.get("title", slug.replace("-", " ").title())
        subtitle = api_data.get("subtitle", "")
        bylines = api_data.get("publishedBylines", [])
        print(f"[{i+1}/{len(candidates)}] {slug}")
        try:
            result = enrich_post(slug, title, subtitle, bylines, html_path, client)
            existing[slug] = result
            print(f"  author={result.get('primary_author')}  cats={result.get('categories')}")
            print(f"  {result.get('summary','')[:100]}...")
            if i % 10 == 9:
                save(existing)
                print("  (saved checkpoint)")
            if i < len(candidates) - 1:
                time.sleep(0.2)
        except Exception as e:
            print(f"  ERROR: {e}")
            errors.append(slug)

    save(existing)
    print(f"\nDone. Enriched: {len(candidates) - len(errors)}  Errors: {len(errors)}")
    if errors:
        print(f"Failed slugs: {errors}")


if __name__ == "__main__":
    main()
