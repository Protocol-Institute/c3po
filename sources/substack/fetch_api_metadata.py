"""
Fetch all posts from Protocolized Substack API and save to api_metadata.json.
Usage: python3 sources/substack/fetch_api_metadata.py
"""
import json
import time
import urllib.request
from pathlib import Path

BASE_URL = "https://protocolized.summerofprotocols.com"
OUT_PATH = Path(__file__).parent / "api_metadata.json"

KEEP_FIELDS = [
    "id", "slug", "title", "subtitle", "post_date", "updated_at",
    "type", "wordcount", "audience", "is_published",
    "section_id",
    "postTags", "publishedBylines",
    "reaction_count", "restacks", "comment_count",
    "previous_post_slug", "next_post_slug",
    "cover_image", "search_engine_description",
]

SECTION_NAMES = {
    333103: "Obliquities",
    333105: "Fictions",
    333110: "Articles",
    None: "Protocolized",
}


def fetch_all_posts():
    posts, offset = [], 0
    while True:
        url = f"{BASE_URL}/api/v1/posts?limit=50&offset={offset}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            batch = json.loads(r.read())
        if not batch:
            break
        posts.extend(batch)
        print(f"  Fetched {len(posts)} posts...")
        if len(batch) < 50:
            break
        offset += 50
        time.sleep(0.4)
    return posts


def slim(post):
    out = {k: post.get(k) for k in KEEP_FIELDS if k in post or post.get(k) is not None}
    out["section_name"] = SECTION_NAMES.get(post.get("section_id"), "Protocolized")
    # Simplify bylines to just what we need
    bylines = []
    for b in post.get("publishedBylines", []):
        bylines.append({
            "id": b.get("id"),
            "name": b.get("name"),
            "handle": b.get("handle"),
            "bio": b.get("bio"),
        })
    out["publishedBylines"] = bylines
    # Simplify tags
    tags = []
    for t in post.get("postTags", []):
        tags.append({"id": t.get("id"), "name": t.get("name"), "slug": t.get("slug")})
    out["postTags"] = tags
    return out


if __name__ == "__main__":
    print("Fetching Protocolized API...")
    raw = fetch_all_posts()
    print(f"Total posts fetched: {len(raw)}")
    slimmed = [slim(p) for p in raw]
    OUT_PATH.write_text(json.dumps(slimmed, indent=2, ensure_ascii=False))
    print(f"Saved to {OUT_PATH}")

    # Summary stats
    tag_counts = {}
    for p in slimmed:
        for t in p.get("postTags", []):
            tag_counts[t["name"]] = tag_counts.get(t["name"], 0) + 1
    print("\nTag distribution:")
    for tag, count in sorted(tag_counts.items(), key=lambda x: -x[1]):
        print(f"  {tag}: {count} posts")

    total_reactions = sum(p.get("reaction_count", 0) or 0 for p in slimmed)
    print(f"\nTotal reaction_count sum: {total_reactions}")
    print(f"Types: {set(p.get('type') for p in slimmed)}")
