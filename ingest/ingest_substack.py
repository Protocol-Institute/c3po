"""
Ingest Protocolized Substack export into Pinecone.

The export is a zip from Substack dashboard containing HTML post files.
Point SUBSTACK_EXPORT_DIR at the unzipped directory, or pass --export-dir.

Also works with the live RSS feed for incremental sync (--rss mode).

Usage:
    python3 ingest/ingest_substack.py --export-dir /path/to/export
    python3 ingest/ingest_substack.py --rss    # sync new posts from RSS
"""

import argparse
import os
import re
import sys
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
from utils import clean_text, chunk_text, embed_chunks, upsert_chunks
from utils import get_voyage_client, get_pinecone_index

load_dotenv()

RSS_FEED_URL = "https://protocolized.summerofprotocols.com/feed"


def parse_html_export(html_path: Path) -> dict | None:
    """
    Parse a Substack export HTML file.
    Returns dict with title, date, slug, body_text, or None if unparseable.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("Install beautifulsoup4: pip install beautifulsoup4")
        sys.exit(1)

    text = html_path.read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(text, "html.parser")

    title_tag = soup.find("h1") or soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else html_path.stem

    # Substack export puts date in <time> or in filename (YYYY-MM-DD prefix)
    date = ""
    time_tag = soup.find("time")
    if time_tag and time_tag.get("datetime"):
        date = time_tag["datetime"][:10]
    else:
        match = re.match(r"(\d{4}-\d{2}-\d{2})", html_path.name)
        if match:
            date = match.group(1)

    # Extract body text (remove nav, footer, script, style)
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    body = soup.get_text(separator="\n")

    return {
        "title": title,
        "date": date,
        "slug": html_path.stem,
        "url": f"https://protocolized.summerofprotocols.com/p/{html_path.stem}",
        "body_text": body,
        "source": "substack",
        "type": "article",
        "authors": [{"name": "Protocolized"}],
        "tags": ["protocols"],
    }


def ingest_export(export_dir: Path, vc, index) -> int:
    """Ingest all HTML files from a Substack export directory."""
    html_files = sorted(export_dir.glob("**/*.html"))
    if not html_files:
        print(f"No HTML files found in {export_dir}")
        return 0

    total = 0
    for html_path in html_files:
        print(f"\n[{html_path.name}]")
        post = parse_html_export(html_path)
        if not post:
            continue

        text = clean_text(post["body_text"])
        if len(text) < 100:
            print(f"  Skipping (too short)")
            continue

        chunks = chunk_text(text)
        meta = {k: v for k, v in post.items() if k != "body_text"}
        print(f"  Chunks: {len(chunks)}")

        vectors = embed_chunks(chunks, vc)
        count = upsert_chunks(chunks, vectors, meta, index)
        total += count
        print(f"  Upserted: {count}")

    return total


def ingest_rss(vc, index) -> int:
    """Sync new posts from Protocolized RSS feed."""
    try:
        import feedparser
    except ImportError:
        print("Install feedparser: pip install feedparser")
        sys.exit(1)

    feed = feedparser.parse(RSS_FEED_URL)
    total = 0

    for entry in feed.entries:
        title = entry.get("title", "")
        link = entry.get("link", "")
        date = entry.get("published", "")[:10] if entry.get("published") else ""
        slug = link.split("/p/")[-1].rstrip("/") if "/p/" in link else ""

        # Extract text from summary/content
        content = ""
        if entry.get("content"):
            content = entry["content"][0].get("value", "")
        elif entry.get("summary"):
            content = entry["summary"]

        try:
            from bs4 import BeautifulSoup
            text = clean_text(BeautifulSoup(content, "html.parser").get_text(separator="\n"))
        except ImportError:
            text = clean_text(re.sub(r"<[^>]+>", " ", content))

        if len(text) < 100:
            continue

        print(f"\n[{slug or title[:40]}]")
        chunks = chunk_text(text)
        meta = {
            "title": title, "date": date, "slug": slug, "url": link,
            "source": "substack", "type": "article",
            "authors": ["Protocolized"], "tags": ["protocols"],
        }
        vectors = embed_chunks(chunks, vc)
        count = upsert_chunks(chunks, vectors, meta, index)
        total += count
        print(f"  Upserted: {count}")

    return total


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--export-dir", help="Path to unzipped Substack export")
    parser.add_argument("--rss", action="store_true", help="Sync from live RSS feed")
    args = parser.parse_args()

    if not args.export_dir and not args.rss:
        parser.error("Provide --export-dir or --rss")

    vc = get_voyage_client()
    index = get_pinecone_index()

    if args.rss:
        total = ingest_rss(vc, index)
    else:
        total = ingest_export(Path(args.export_dir), vc, index)

    print(f"\nDone. Total chunks upserted: {total}")


if __name__ == "__main__":
    main()
