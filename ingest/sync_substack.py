"""
Daily API sync for Protocolized Substack (replaces RSS + export two-track).

Detects:
  - New posts (slug not seen before) → full enrichment + all 4 vector types
  - Edited posts (updated_at advanced) → re-embed body chunks + post_summary
  - Tag changes (postTags changed) → re-embed affected collection_card vectors

Usage:
    python3 ingest/sync_substack.py
    python3 ingest/sync_substack.py --dry-run
    python3 ingest/sync_substack.py --force-slug some-slug
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
from utils import clean_text, chunk_text, embed_chunks, chunk_id, get_voyage_client, get_pinecone_index, PINECONE_BATCH

load_dotenv()

BASE_URL = "https://protocolized.summerofprotocols.com"
SUBSTACK_URL = "https://protocolized.summerofprotocols.com/p"

API_METADATA_PATH = Path("sources/substack/api_metadata.json")
COLLECTIONS_PATH = Path("sources/substack/collections.json")
ENRICHED_META_PATH = Path("sources/substack/enriched_meta.json")
REGISTRY_PATH = Path("sources/substack/registry.json")

NAMESPACE = "substack"

SECTION_NAMES = {
    333103: "Obliquities",
    333105: "Fictions",
    333110: "Articles",
    None: "Protocolized",
}

COLLECTION_TAG_SLUGS = {
    "terminological-twists", "ghosts-in-machines", "the-librarians",
    "building-and-burning-bridges", "zoothesia", "bridge-atlas",
    "sigfpt", "sigbiz", "sigmem", "sigfic", "obliquities",
    "protocols-for-the-long-now", "sig",
}

AUTHOR_ALIASES = {
    "Thing Party": "Elizabeth Maher",
    "Sachin": "Sachin Benny",
    "Spencer Nitkey - Writer": "Spencer Nitkey",
    "Marie-Hélène Lebeault - Author": "Marie-Hélène Lebeault",
}

ENRICH_SYSTEM = """You are a metadata enrichment assistant for Protocolized, a magazine about protocol science.
Given a post title, subtitle, bylines, and opening text, return a JSON object with:
- "summary": exactly two sentences about the specific argument, story, or contribution
- "categories": array of 2-4 tags from: ["protocol-fiction","protocol-theory","protocol-watching","editorial","research-report","technology-ai","governance","announcement","interview","memory-archival","organizations"]
- "primary_author": the main non-editorial author's real name. Return "Protocolized" if editorial only.

Respond with only the JSON object."""


def fetch_all_posts_api():
    posts, offset = [], 0
    while True:
        url = f"{BASE_URL}/api/v1/posts?limit=50&offset={offset}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            batch = json.loads(r.read())
        if not batch:
            break
        posts.extend(batch)
        if len(batch) < 50:
            break
        offset += 50
        time.sleep(0.4)
    return posts


def fetch_post_body(slug: str) -> str:
    """Fetch full body_html for a single post via API."""
    url = f"{BASE_URL}/api/v1/posts/{slug}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        post = json.loads(r.read())
    return post.get("body_html", "")


def plain_text_from_html(html: str) -> str:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("Install beautifulsoup4")
        sys.exit(1)
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["figure", "figcaption", "button", "svg", "script", "style"]):
        tag.decompose()
    return re.sub(r'\s+', ' ', soup.get_text()).strip()


def resolve_authors(bylines: list[dict]) -> tuple[str, list[str]]:
    """Returns (primary_author, all_authors)."""
    names = []
    for b in bylines:
        name = AUTHOR_ALIASES.get(b.get("name", ""), b.get("name", ""))
        if name and name != "Protocolized" and name not in names:
            names.append(name)
    primary = names[0] if names else "Protocolized"
    return primary, (names if names else ["Protocolized"])


def enrich_post_haiku(title, subtitle, bylines, opening_text, client):
    """Call Claude Haiku to get summary, categories, author."""
    byline_str = ", ".join(b.get("name", "") for b in bylines if b.get("name") != "Protocolized")
    user_content = (
        f"Title: {title}\nSubtitle: {subtitle or '(none)'}\n"
        f"Bylines: {byline_str or 'Protocolized (editorial)'}\n\nOpening text:\n{opening_text}"
    )
    try:
        import anthropic
        client_obj = client if hasattr(client, "messages") else anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        resp = client_obj.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": user_content}],
            system=ENRICH_SYSTEM,
        )
        raw = resp.content[0].text.strip()
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        return json.loads(m.group()) if m else {}
    except Exception as e:
        print(f"  Haiku enrichment failed: {e}")
        return {}


def upsert_body_and_summary(slug, title, subtitle, date, section, author, all_authors,
                             summary, categories, colls, api_post, body_text, vc, index):
    """Embed and upsert body chunks + post_summary for one post."""
    substack_categories = [t["name"] for t in api_post.get("postTags", [])
                           if t.get("slug") not in COLLECTION_TAG_SLUGS]

    # Body chunks
    collection_line = ""
    if colls:
        names = ", ".join(c["collection_name"] for c in colls)
        collection_line = f"Collection: {names}\n"
    prefix = (f"Title: {title}\nAuthor: {author}\n{collection_line}Summary: {summary}\n\n"
               if summary else f"Title: {title}\nAuthor: {author}\n\n")

    text = clean_text(body_text)
    chunks = chunk_text(text)
    prefixed = [prefix + c for c in chunks]

    base_meta = {
        "source": "substack", "namespace": NAMESPACE, "chunk_type": "body",
        "title": title, "subtitle": subtitle or "", "slug": slug,
        "url": f"{SUBSTACK_URL}/{slug}", "date": date, "section": section,
        "primary_author": author, "authors": all_authors,
        "substack_categories": substack_categories, "enriched_categories": categories,
        "summary": summary[:500] if summary else "",
        "collection": colls[0]["collection_name"] if colls else "",
        "collection_slug": colls[0]["collection_slug"] if colls else "",
        "collection_type": colls[0]["collection_type"] if colls else "",
        "all_collections": [c["collection_name"] for c in colls],
        "reaction_count": api_post.get("reaction_count") or 0,
        "restacks": api_post.get("restacks") or 0,
        "access_level": "public",
    }

    vectors = embed_chunks(prefixed, vc)
    records = []
    for i, (chunk, vector) in enumerate(zip(chunks, vectors)):
        meta = {**base_meta, "chunk_index": i, "chunk_total": len(chunks), "text": chunk[:1000]}
        records.append({"id": chunk_id(chunk), "values": vector, "metadata": meta})
    for i in range(0, len(records), PINECONE_BATCH):
        index.upsert(vectors=records[i:i + PINECONE_BATCH], namespace=NAMESPACE)

    # Post summary
    summary_text = (
        f"Title: {title}\nAuthor: {author}\nDate: {date}\n"
        f"{collection_line}Section: {section}\n"
        f"Categories: {', '.join(categories)}\nSummary: {summary}"
    )
    [sv] = embed_chunks([summary_text], vc)
    index.upsert(vectors=[{
        "id": f"{slug}__post_summary",
        "values": sv,
        "metadata": {**base_meta, "chunk_type": "post_summary",
                     "chunk_index": 0, "chunk_total": 1, "text": summary_text[:1000]},
    }], namespace=NAMESPACE)

    return len(records) + 1


def upsert_collection_card(coll_slug, coll, api_by_slug, enriched, vc, index):
    """Re-embed one collection_card vector."""
    name = coll["name"]
    description = coll.get("description", "")
    posts = coll.get("posts", [])
    coll_type = coll.get("type", "collection")
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
        f"{author_line}{description}\nPosts ({len(posts)} total):\n"
        + "\n".join(post_lines)
    )
    [v] = embed_chunks([text], vc)
    index.upsert(vectors=[{
        "id": f"collection__{coll_slug}",
        "values": v,
        "metadata": {
            "source": "substack", "namespace": NAMESPACE, "chunk_type": "collection_card",
            "collection_slug": coll_slug, "collection_name": name,
            "collection_type": coll_type, "collection_author": author,
            "description": description, "post_count": len(posts),
            "post_slugs": posts, "text": text[:1000],
        },
    }], namespace=NAMESPACE)


def load_last_sync() -> dict:
    """Load slug → {updated_at, postTags} from registry."""
    if REGISTRY_PATH.exists():
        reg = json.loads(REGISTRY_PATH.read_text())
        return reg.get("last_sync", {})
    return {}


def save_last_sync(sync_map: dict):
    reg = json.loads(REGISTRY_PATH.read_text()) if REGISTRY_PATH.exists() else {}
    reg["last_sync"] = sync_map
    reg["last_sync_timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    REGISTRY_PATH.write_text(json.dumps(reg, indent=2, ensure_ascii=False))


def build_collection_lookup(collections: dict) -> dict:
    lookup = {}
    def add(ps, cs, cn, ct):
        lookup.setdefault(ps, []).append({"collection_slug": cs, "collection_name": cn, "collection_type": ct})
    for cs, c in collections.get("fiction-collections", {}).items():
        [add(p, cs, c["name"], "fiction-collection") for p in c.get("posts", [])]
    for cs, c in collections.get("series", {}).items():
        [add(p, cs, c["name"], "series") for p in c.get("posts", [])]
    for cs, c in collections.get("proto-collections", {}).items():
        [add(p, cs, c["name"], "proto-collection") for p in c.get("posts", [])]
    for cs, c in collections.get("sigs", {}).items():
        [add(p, cs, c["name"], "sig") for p in c.get("posts", [])]
    for cs, c in collections.get("editorial", {}).items():
        [add(p, cs, c["name"], "editorial") for p in c.get("posts", [])]
    return lookup


def slim_api_post(post: dict) -> dict:
    return {
        "id": post.get("id"),
        "slug": post.get("slug"),
        "title": post.get("title"),
        "subtitle": post.get("subtitle"),
        "post_date": post.get("post_date"),
        "updated_at": post.get("updated_at"),
        "type": post.get("type"),
        "wordcount": post.get("wordcount"),
        "section_id": post.get("section_id"),
        "section_name": SECTION_NAMES.get(post.get("section_id"), "Protocolized"),
        "postTags": [{"id": t["id"], "name": t["name"], "slug": t["slug"]}
                     for t in post.get("postTags", [])],
        "publishedBylines": [{"id": b.get("id"), "name": b.get("name"), "handle": b.get("handle"), "bio": b.get("bio")}
                             for b in post.get("publishedBylines", [])],
        "reaction_count": post.get("reaction_count"),
        "restacks": post.get("restacks"),
        "comment_count": post.get("comment_count"),
        "previous_post_slug": post.get("previous_post_slug"),
        "next_post_slug": post.get("next_post_slug"),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force-slug", help="Force re-sync a specific slug")
    args = parser.parse_args()

    print("Fetching Protocolized API...")
    raw_posts = fetch_all_posts_api()
    current = {p["slug"]: p for p in raw_posts if p.get("type") == "newsletter"}
    print(f"  {len(current)} newsletter posts in API")

    last_sync = load_last_sync()
    enriched = json.loads(ENRICHED_META_PATH.read_text()) if ENRICHED_META_PATH.exists() else {}
    collections = json.loads(COLLECTIONS_PATH.read_text()) if COLLECTIONS_PATH.exists() else {}
    coll_lookup = build_collection_lookup(collections)

    # Categorize work
    new_slugs, edited_slugs, tag_changed_slugs = [], [], []
    for slug, post in current.items():
        if args.force_slug and slug != args.force_slug:
            continue
        prev = last_sync.get(slug, {})
        if not prev:
            new_slugs.append(slug)
        elif post.get("updated_at") != prev.get("updated_at"):
            edited_slugs.append(slug)
            curr_tags = {t["slug"] for t in post.get("postTags", [])}
            prev_tags = set(prev.get("postTags", []))
            if curr_tags != prev_tags:
                tag_changed_slugs.append(slug)
        else:
            curr_tags = {t["slug"] for t in post.get("postTags", [])}
            prev_tags = set(prev.get("postTags", []))
            if curr_tags != prev_tags:
                tag_changed_slugs.append(slug)

    print(f"New: {len(new_slugs)}  Edited: {len(edited_slugs)}  Tag changes: {len(tag_changed_slugs)}")
    if args.dry_run:
        if new_slugs:
            print(f"  New posts: {new_slugs}")
        if edited_slugs:
            print(f"  Edited posts: {edited_slugs}")
        if tag_changed_slugs:
            print(f"  Tag changes: {tag_changed_slugs}")
        return

    if not new_slugs and not edited_slugs and not tag_changed_slugs:
        print("Nothing to sync.")
        # Still update last_sync with fresh updated_at values
        new_sync = {slug: {"updated_at": p.get("updated_at"),
                            "postTags": [t["slug"] for t in p.get("postTags", [])]}
                    for slug, p in current.items()}
        save_last_sync(new_sync)
        return

    vc = get_voyage_client()
    index = get_pinecone_index()
    total_upserted = 0

    try:
        import anthropic
        haiku_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    except ImportError:
        haiku_client = None

    # Process new posts
    for slug in new_slugs:
        print(f"\n[NEW] {slug}")
        post = current[slug]
        title = post.get("title", slug)
        subtitle = post.get("subtitle", "")
        date = (post.get("post_date") or "")[:10]
        section = SECTION_NAMES.get(post.get("section_id"), "Protocolized")
        bylines = post.get("publishedBylines", [])
        primary_author, all_authors = resolve_authors(bylines)

        # Fetch body
        body_html = fetch_post_body(slug)
        body_text = plain_text_from_html(body_html)
        time.sleep(0.3)

        # Haiku enrichment
        enrich = {}
        if haiku_client and body_text:
            enrich = enrich_post_haiku(title, subtitle, bylines, body_text[:1200], haiku_client)
        summary = enrich.get("summary", "")
        categories = enrich.get("categories", [])
        if enrich.get("primary_author"):
            primary_author = enrich["primary_author"]

        # Save enrichment
        enriched[slug] = {
            "summary": summary, "categories": categories,
            "primary_author": primary_author, "all_authors": all_authors,
        }
        ENRICHED_META_PATH.write_text(json.dumps(enriched, indent=2, ensure_ascii=False))

        colls = coll_lookup.get(slug, [])
        n = upsert_body_and_summary(slug, title, subtitle, date, section,
                                     primary_author, all_authors, summary, categories,
                                     colls, post, body_text, vc, index)
        total_upserted += n
        print(f"  Upserted {n} vectors")
        time.sleep(0.3)

    # Process edited posts
    for slug in edited_slugs:
        print(f"\n[EDITED] {slug}")
        post = current[slug]
        title = post.get("title", slug)
        subtitle = post.get("subtitle", "")
        date = (post.get("post_date") or "")[:10]
        section = SECTION_NAMES.get(post.get("section_id"), "Protocolized")
        bylines = post.get("publishedBylines", [])
        primary_author, all_authors = resolve_authors(bylines)

        body_html = fetch_post_body(slug)
        body_text = plain_text_from_html(body_html)
        time.sleep(0.3)

        # Re-enrich only if wordcount delta > 15%
        old_wc = last_sync.get(slug, {}).get("wordcount", 0) or 0
        new_wc = post.get("wordcount", 0) or 0
        enrich = enriched.get(slug, {})
        if old_wc and new_wc and abs(new_wc - old_wc) / old_wc > 0.15 and haiku_client:
            print(f"  Wordcount changed {old_wc}→{new_wc}: re-enriching")
            new_enrich = enrich_post_haiku(title, subtitle, bylines, body_text[:1200], haiku_client)
            if new_enrich:
                enrich = new_enrich
                enriched[slug] = {**enrich, "all_authors": all_authors}
                ENRICHED_META_PATH.write_text(json.dumps(enriched, indent=2, ensure_ascii=False))

        summary = enrich.get("summary", "")
        categories = enrich.get("categories", [])
        colls = coll_lookup.get(slug, [])
        n = upsert_body_and_summary(slug, title, subtitle, date, section,
                                     primary_author, all_authors, summary, categories,
                                     colls, post, body_text, vc, index)
        total_upserted += n
        print(f"  Upserted {n} vectors")
        time.sleep(0.3)

    # Re-embed collection cards where tags changed
    affected_collections = set()
    for slug in tag_changed_slugs:
        for coll in coll_lookup.get(slug, []):
            affected_collections.add(coll["collection_slug"])
        # Also check new tags that may map to a collection
        for t in current[slug].get("postTags", []):
            if t["slug"] in COLLECTION_TAG_SLUGS:
                affected_collections.add(t["slug"])

    all_colls = {}
    for section_key in ["fiction-collections", "series", "proto-collections", "sigs", "editorial"]:
        all_colls.update(collections.get(section_key, {}))

    api_by_slug = {p["slug"]: p for p in raw_posts}
    for coll_slug in affected_collections:
        if coll_slug in all_colls:
            print(f"\n[RE-EMBED COLLECTION] {coll_slug}")
            upsert_collection_card(coll_slug, all_colls[coll_slug], api_by_slug, enriched, vc, index)
            total_upserted += 1

    # Update api_metadata.json and last_sync
    slimmed = [slim_api_post(p) for p in raw_posts]
    API_METADATA_PATH.write_text(json.dumps(slimmed, indent=2, ensure_ascii=False))

    new_sync = {slug: {"updated_at": p.get("updated_at"),
                        "wordcount": p.get("wordcount"),
                        "postTags": [t["slug"] for t in p.get("postTags", [])]}
                for slug, p in current.items()}
    save_last_sync(new_sync)

    print(f"\nSync complete. Total vectors upserted: {total_upserted}")


if __name__ == "__main__":
    main()
