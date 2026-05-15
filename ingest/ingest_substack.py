"""
Ingest Protocolized Substack export into Pinecone (namespace: substack).

Four vector types:
  1. Body chunks    — chunked post text, enriched prefix
  2. post_summary   — one vector per post (title + summary + categories)
  3. collection_card — one per collection/series/SIG (for "what's in X" queries)
  4. author_profile  — one per recurring author (for author-centric queries)

Usage:
    python3 ingest/ingest_substack.py --export-dir data/substack
    python3 ingest/ingest_substack.py --export-dir data/substack --dry-run
    python3 ingest/ingest_substack.py --export-dir data/substack --type body
    python3 ingest/ingest_substack.py --export-dir data/substack --type collections
    python3 ingest/ingest_substack.py --export-dir data/substack --type authors
"""

import argparse
import csv
import json
import os
import re
import sys
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
from utils import clean_text, chunk_text, embed_chunks, upsert_chunks, chunk_id
from utils import get_voyage_client, get_pinecone_index

load_dotenv()

NAMESPACE = "substack"
SUBSTACK_URL = "https://protocolized.summerofprotocols.com/p"

API_METADATA_PATH = Path("sources/substack/api_metadata.json")
COLLECTIONS_PATH = Path("sources/substack/collections.json")
ENRICHED_META_PATH = Path("sources/substack/enriched_meta.json")

# Publication-specific collection tag slugs (exclude generic discovery tags)
COLLECTION_TAG_SLUGS = {
    "terminological-twists", "ghosts-in-machines", "the-librarians",
    "building-and-burning-bridges", "zoothesia", "bridge-atlas",
    "sigfpt", "sigbiz", "sigmem", "sigfic", "obliquities",
    "protocols-for-the-long-now", "sig",
}

# Page-type posts with actual essay/guide content (not nav stubs)
PAGE_ALLOWLIST = {
    "ghosts-in-machines",
    "building-and-burning-bridges",
    "submission-guidelines",
}


def plain_text_from_html(html_bytes: bytes) -> str:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("Install beautifulsoup4: pip install beautifulsoup4")
        sys.exit(1)
    soup = BeautifulSoup(html_bytes.decode("utf-8", errors="ignore"), "html.parser")
    for tag in soup.find_all(["figure", "figcaption", "button", "svg", "script", "style"]):
        tag.decompose()
    return re.sub(r'\s+', ' ', soup.get_text()).strip()


def load_posts_csv(export_dir: Path) -> dict:
    csv_path = export_dir / "posts.csv"
    if not csv_path.exists():
        return {}
    meta = {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            post_id_field = row.get("post_id", "")
            if "." in post_id_field:
                stem = post_id_field
                numeric_id, slug = post_id_field.split(".", 1)
            else:
                numeric_id = post_id_field
                slug = ""
                stem = post_id_field
            meta[stem] = {
                "post_id": numeric_id,
                "slug": slug,
                "title": row.get("title", ""),
                "subtitle": row.get("subtitle", ""),
                "date": row.get("post_date", "")[:10] if row.get("post_date") else "",
                "type": row.get("type", "newsletter"),
                "audience": row.get("audience", "everyone"),
                "is_published": row.get("is_published", "false").lower() == "true",
            }
    return meta


def build_collection_lookup(collections: dict, api_by_slug: dict) -> dict:
    """Return dict: slug → {collection_name, collection_slug, collection_type, sig_name}"""
    lookup = {}

    def add(post_slug, coll_slug, coll_name, coll_type):
        if post_slug not in lookup:
            lookup[post_slug] = []
        lookup[post_slug].append({
            "collection_slug": coll_slug,
            "collection_name": coll_name,
            "collection_type": coll_type,
        })

    for coll_slug, coll in collections.get("fiction-collections", {}).items():
        for ps in coll.get("posts", []):
            add(ps, coll_slug, coll["name"], "fiction-collection")

    for coll_slug, coll in collections.get("series", {}).items():
        for ps in coll.get("posts", []):
            add(ps, coll_slug, coll["name"], "series")

    for coll_slug, coll in collections.get("proto-collections", {}).items():
        for ps in coll.get("posts", []):
            add(ps, coll_slug, coll["name"], "proto-collection")

    for sig_slug, sig in collections.get("sigs", {}).items():
        for ps in sig.get("posts", []):
            add(ps, sig_slug, sig["name"], "sig")

    for coll_slug, coll in collections.get("editorial", {}).items():
        for ps in coll.get("posts", []):
            add(ps, coll_slug, coll["name"], "editorial")

    return lookup


def post_substack_categories(api_post: dict) -> list[str]:
    """Extract publication-defined categories (non-collection tags)."""
    return [
        t["name"] for t in api_post.get("postTags", [])
        if t.get("slug") not in COLLECTION_TAG_SLUGS
    ]


def ingest_body_chunks(export_dir: Path, vc, index, dry_run: bool = False) -> int:
    posts_dir = export_dir / "posts" if (export_dir / "posts").exists() else export_dir
    csv_meta = load_posts_csv(export_dir)
    api_meta = json.loads(API_METADATA_PATH.read_text()) if API_METADATA_PATH.exists() else []
    api_by_slug = {p["slug"]: p for p in api_meta}
    enriched = json.loads(ENRICHED_META_PATH.read_text()) if ENRICHED_META_PATH.exists() else {}
    collections = json.loads(COLLECTIONS_PATH.read_text()) if COLLECTIONS_PATH.exists() else {}
    coll_lookup = build_collection_lookup(collections, api_by_slug)

    html_files = sorted(posts_dir.glob("*.html"))
    total = 0

    for html_path in html_files:
        if "." not in html_path.stem:
            continue
        post_id, slug = html_path.stem.split(".", 1)
        if not post_id.isdigit():
            continue

        stem = html_path.stem
        meta_row = csv_meta.get(stem, {})
        post_type = meta_row.get("type", "newsletter")
        is_published = meta_row.get("is_published", True)
        has_delivers = (posts_dir / f"{post_id}.delivers.csv").exists()

        if post_type == "podcast":
            continue
        if post_type == "page" and slug not in PAGE_ALLOWLIST:
            continue
        if post_type != "page" and not is_published and not has_delivers:
            print(f"  [skip draft] {slug}")
            continue

        api_post = api_by_slug.get(slug, {})
        enrich = enriched.get(slug, {})
        title = meta_row.get("title") or api_post.get("title") or slug.replace("-", " ").title()
        date = meta_row.get("date", "") or (api_post.get("post_date") or "")[:10]
        subtitle = meta_row.get("subtitle", "") or api_post.get("subtitle", "")
        section = api_post.get("section_name", "Protocolized")
        substack_categories = post_substack_categories(api_post)
        author = enrich.get("primary_author", "Protocolized")
        all_authors = enrich.get("all_authors", ["Protocolized"])
        summary = enrich.get("summary", "")
        categories = enrich.get("categories", [])
        colls = coll_lookup.get(slug, [])

        print(f"\n[{slug}]  {date}  section={section}  author={author}")

        text = clean_text(plain_text_from_html(html_path.read_bytes()))
        if len(text) < 100:
            print(f"  Skipping (too short: {len(text)} chars)")
            continue

        # Build chunk prefix to improve retrieval relevance
        collection_line = ""
        if colls:
            names = ", ".join(c["collection_name"] for c in colls)
            collection_line = f"Collection: {names}\n"
        prefix = (
            f"Title: {title}\n"
            f"Author: {author}\n"
            f"{collection_line}"
            f"Summary: {summary}\n\n"
        ) if summary else f"Title: {title}\nAuthor: {author}\n\n"

        chunks = chunk_text(text)
        prefixed_chunks = [prefix + c for c in chunks]

        meta = {
            "source": "substack",
            "namespace": NAMESPACE,
            "chunk_type": "body",
            "title": title,
            "subtitle": subtitle,
            "slug": slug,
            "url": f"{SUBSTACK_URL}/{slug}",
            "date": date,
            "section": section,
            "primary_author": author,
            "authors": all_authors,
            "substack_categories": substack_categories,
            "enriched_categories": categories,
            "summary": summary[:500] if summary else "",
            "collection": colls[0]["collection_name"] if colls else "",
            "collection_slug": colls[0]["collection_slug"] if colls else "",
            "collection_type": colls[0]["collection_type"] if colls else "",
            "all_collections": [c["collection_name"] for c in colls],
            "reaction_count": api_post.get("reaction_count") or 0,
            "restacks": api_post.get("restacks") or 0,
            "access_level": "public",
        }

        print(f"  Chunks: {len(chunks)}  prefix={len(prefix)} chars")
        if dry_run:
            total += len(chunks)
            continue

        vectors = embed_chunks(prefixed_chunks, vc)
        count = upsert_chunks(chunks, vectors, meta, index, namespace=NAMESPACE)
        total += count
        print(f"  Upserted: {count}")

    return total


def ingest_post_summaries(export_dir: Path, vc, index, dry_run: bool = False) -> int:
    """Embed one post_summary vector per post."""
    posts_dir = export_dir / "posts" if (export_dir / "posts").exists() else export_dir
    csv_meta = load_posts_csv(export_dir)
    api_meta = json.loads(API_METADATA_PATH.read_text()) if API_METADATA_PATH.exists() else []
    api_by_slug = {p["slug"]: p for p in api_meta}
    enriched = json.loads(ENRICHED_META_PATH.read_text()) if ENRICHED_META_PATH.exists() else {}
    collections = json.loads(COLLECTIONS_PATH.read_text()) if COLLECTIONS_PATH.exists() else {}
    coll_lookup = build_collection_lookup(collections, api_by_slug)

    html_files = sorted(posts_dir.glob("*.html"))
    texts, metas = [], []

    for html_path in html_files:
        if "." not in html_path.stem:
            continue
        post_id, slug = html_path.stem.split(".", 1)
        if not post_id.isdigit():
            continue

        stem = html_path.stem
        meta_row = csv_meta.get(stem, {})
        post_type = meta_row.get("type", "newsletter")
        is_published = meta_row.get("is_published", True)
        has_delivers = (posts_dir / f"{post_id}.delivers.csv").exists()

        if post_type == "podcast":
            continue
        if post_type == "page" and slug not in PAGE_ALLOWLIST:
            continue
        if post_type != "page" and not is_published and not has_delivers:
            continue

        api_post = api_by_slug.get(slug, {})
        enrich = enriched.get(slug, {})
        title = meta_row.get("title") or api_post.get("title") or slug.replace("-", " ").title()
        date = meta_row.get("date", "") or (api_post.get("post_date") or "")[:10]
        section = api_post.get("section_name", "Protocolized")
        author = enrich.get("primary_author", "Protocolized")
        all_authors = enrich.get("all_authors", ["Protocolized"])
        summary = enrich.get("summary", "")
        categories = enrich.get("categories", [])
        substack_categories = post_substack_categories(api_post)
        colls = coll_lookup.get(slug, [])

        if not summary:
            continue  # skip posts without enrichment

        collection_line = ""
        if colls:
            names = ", ".join(c["collection_name"] for c in colls)
            collection_line = f"Collection: {names}\n"

        text = (
            f"Title: {title}\n"
            f"Author: {author}\n"
            f"Date: {date}\n"
            f"{collection_line}"
            f"Section: {section}\n"
            f"Categories: {', '.join(categories)}\n"
            f"Summary: {summary}"
        )
        texts.append(text)
        metas.append({
            "id": f"{slug}__post_summary",
            "source": "substack",
            "namespace": NAMESPACE,
            "chunk_type": "post_summary",
            "title": title,
            "slug": slug,
            "url": f"{SUBSTACK_URL}/{slug}",
            "date": date,
            "section": section,
            "primary_author": author,
            "authors": all_authors,
            "substack_categories": substack_categories,
            "enriched_categories": categories,
            "summary": summary[:500],
            "collection": colls[0]["collection_name"] if colls else "",
            "collection_slug": colls[0]["collection_slug"] if colls else "",
            "all_collections": [c["collection_name"] for c in colls],
            "reaction_count": api_post.get("reaction_count") or 0,
            "restacks": api_post.get("restacks") or 0,
            "access_level": "public",
        })

    print(f"Post summary vectors: {len(texts)}")
    if dry_run:
        return len(texts)

    vectors = embed_chunks(texts, vc)
    records = []
    for text, vector, m in zip(texts, vectors, metas):
        rec_id = m.pop("id")
        meta_with_text = {**m, "text": text[:1000]}
        records.append({"id": rec_id, "values": vector, "metadata": meta_with_text})

    from utils import PINECONE_BATCH
    for i in range(0, len(records), PINECONE_BATCH):
        index.upsert(vectors=records[i:i + PINECONE_BATCH], namespace=NAMESPACE)

    print(f"Upserted: {len(records)} post_summary vectors")
    return len(records)


def ingest_collection_cards(vc, index, dry_run: bool = False) -> int:
    """Embed one collection_card vector per collection/series/SIG."""
    if not COLLECTIONS_PATH.exists():
        print("collections.json not found")
        return 0

    api_meta = json.loads(API_METADATA_PATH.read_text()) if API_METADATA_PATH.exists() else []
    api_by_slug = {p["slug"]: p for p in api_meta}
    enriched = json.loads(ENRICHED_META_PATH.read_text()) if ENRICHED_META_PATH.exists() else {}
    collections = json.loads(COLLECTIONS_PATH.read_text())

    texts, records = [], []

    all_sections = [
        ("fiction-collection", collections.get("fiction-collections", {})),
        ("series", collections.get("series", {})),
        ("proto-collection", collections.get("proto-collections", {})),
        ("sig", collections.get("sigs", {})),
        ("editorial", collections.get("editorial", {})),
    ]

    for coll_type, section in all_sections:
        for coll_slug, coll in section.items():
            name = coll["name"]
            description = coll.get("description", "")
            posts = coll.get("posts", [])
            author = coll.get("author", "")

            post_lines = []
            for ps in posts:
                api_p = api_by_slug.get(ps, {})
                enr = enriched.get(ps, {})
                ptitle = api_p.get("title", ps)
                year = (api_p.get("post_date") or "")[:4]
                auth = enr.get("primary_author", "")
                line = f"- {ptitle} ({year})" + (f" by {auth}" if auth and auth not in ("Protocolized", author) else "")
                post_lines.append(line)

            author_line = f"Author: {author}\n" if author else ""
            text = (
                f"{coll_type.replace('-', ' ').title()}: {name}\n"
                f"{author_line}"
                f"{description}\n"
                f"Posts ({len(posts)} total):\n"
                + "\n".join(post_lines)
            )
            texts.append(text)
            rec_id = f"collection__{coll_slug}"
            records.append({
                "id": rec_id,
                "text": text,
                "metadata": {
                    "source": "substack",
                    "namespace": NAMESPACE,
                    "chunk_type": "collection_card",
                    "collection_slug": coll_slug,
                    "collection_name": name,
                    "collection_type": coll_type,
                    "collection_author": author,
                    "description": description,
                    "post_count": len(posts),
                    "post_slugs": posts,
                    "text": text[:1000],
                },
            })
            print(f"  {coll_slug}: {len(posts)} posts")

    print(f"Collection card vectors: {len(records)}")
    if dry_run:
        return len(records)

    vectors = embed_chunks(texts, vc)
    pinecone_records = [
        {"id": r["id"], "values": v, "metadata": r["metadata"]}
        for r, v in zip(records, vectors)
    ]

    from utils import PINECONE_BATCH
    for i in range(0, len(pinecone_records), PINECONE_BATCH):
        index.upsert(vectors=pinecone_records[i:i + PINECONE_BATCH], namespace=NAMESPACE)

    print(f"Upserted: {len(pinecone_records)} collection_card vectors")
    return len(pinecone_records)


def ingest_author_profiles(vc, index, dry_run: bool = False) -> int:
    """Embed one author_profile vector per recurring non-editorial author."""
    api_meta = json.loads(API_METADATA_PATH.read_text()) if API_METADATA_PATH.exists() else []
    api_by_slug = {p["slug"]: p for p in api_meta}
    enriched = json.loads(ENRICHED_META_PATH.read_text()) if ENRICHED_META_PATH.exists() else {}

    # Aggregate posts per author
    author_posts = {}
    for slug, enr in enriched.items():
        for author in enr.get("all_authors", []):
            if author in ("Protocolized", "Venkatesh Rao", "Timber Stinson-Schroff"):
                continue  # editorial team — skip dedicated profile vectors
            author_posts.setdefault(author, []).append(slug)

    texts, records = [], []

    for author, post_slugs in sorted(author_posts.items(), key=lambda x: -len(x[1])):

        post_lines = []
        for ps in sorted(post_slugs, key=lambda s: (api_by_slug.get(s, {}).get("post_date") or "")):
            api_p = api_by_slug.get(ps, {})
            ptitle = api_p.get("title", ps)
            year = (api_p.get("post_date") or "")[:7]
            colls = [t["name"] for t in api_p.get("postTags", []) if t.get("slug") in COLLECTION_TAG_SLUGS]
            coll_note = f" [{', '.join(colls)}]" if colls else ""
            post_lines.append(f"- {ptitle} ({year}){coll_note}")

        n = len(post_slugs)
        contributor_type = (
            "Regular contributor" if n >= 3
            else "Guest contributor"
        )
        text = (
            f"Author: {author}\n"
            f"Contributor type: {contributor_type} ({n} post{'s' if n != 1 else ''})\n"
            + "\n".join(post_lines)
        )
        texts.append(text)
        author_slug = re.sub(r'[^a-z0-9]+', '-', author.lower()).strip('-')
        records.append({
            "id": f"author__{author_slug}",
            "text": text,
            "metadata": {
                "source": "substack",
                "namespace": NAMESPACE,
                "chunk_type": "author_profile",
                "author_name": author,
                "post_count": len(post_slugs),
                "post_slugs": post_slugs,
                "text": text[:1000],
            },
        })
        print(f"  {author}: {len(post_slugs)} posts")

    print(f"Author profile vectors: {len(records)}")
    if dry_run:
        return len(records)

    vectors = embed_chunks(texts, vc)
    pinecone_records = [
        {"id": r["id"], "values": v, "metadata": r["metadata"]}
        for r, v in zip(records, vectors)
    ]

    from utils import PINECONE_BATCH
    for i in range(0, len(pinecone_records), PINECONE_BATCH):
        index.upsert(vectors=pinecone_records[i:i + PINECONE_BATCH], namespace=NAMESPACE)

    print(f"Upserted: {len(pinecone_records)} author_profile vectors")
    return len(pinecone_records)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--export-dir", help="Path to unzipped Substack export")
    parser.add_argument("--dry-run", action="store_true", help="Count only, no API calls")
    parser.add_argument("--type", choices=["body", "summaries", "collections", "authors", "all"],
                        default="all", help="Which vector types to ingest")
    args = parser.parse_args()

    if not args.export_dir and args.type in ("body", "summaries", "all"):
        print("--export-dir required for body and summary vectors")
        sys.exit(1)

    if not args.dry_run:
        vc = get_voyage_client()
        index = get_pinecone_index()
    else:
        vc = index = None

    export_dir = Path(args.export_dir) if args.export_dir else None
    total = 0

    if args.type in ("body", "all"):
        print("\n=== Body chunks ===")
        total += ingest_body_chunks(export_dir, vc, index, dry_run=args.dry_run)

    if args.type in ("summaries", "all"):
        print("\n=== Post summaries ===")
        total += ingest_post_summaries(export_dir, vc, index, dry_run=args.dry_run)

    if args.type in ("collections", "all"):
        print("\n=== Collection cards ===")
        total += ingest_collection_cards(vc, index, dry_run=args.dry_run)

    if args.type in ("authors", "all"):
        print("\n=== Author profiles ===")
        total += ingest_author_profiles(vc, index, dry_run=args.dry_run)

    print(f"\nDone. Total vectors: {total}")


if __name__ == "__main__":
    main()
