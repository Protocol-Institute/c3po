"""
Ingest YouTube video transcripts into Pinecone (namespace: videos).

Two vector types per video:
  1. body         — chunked transcript text with title/speakers/summary prefix for retrieval
  2. video_summary — one vector per video (title + speakers + summary + key_concepts)

Run fetch_youtube_meta.py and enrich_youtube.py first.

Usage:
    python3 ingest/ingest_youtube.py
    python3 ingest/ingest_youtube.py --video VIDEO_ID
    python3 ingest/ingest_youtube.py --type body
    python3 ingest/ingest_youtube.py --type summaries
    python3 ingest/ingest_youtube.py --dry-run
"""

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
from utils import clean_text, chunk_text, embed_chunks, upsert_chunks, chunk_id
from utils import get_voyage_client, get_pinecone_index, PINECONE_BATCH

load_dotenv()

NAMESPACE = "videos"
STATE_PATH = Path("data/youtube_ingest_state.json")

# metadata["text"] is the only part of a chunk the model reads. It was stored
# [:1000] against ~2.3K-char chunks, so most of every transcript chunk was
# retrieved and then thrown away — the session-55 cap pattern (1,600 there).
MAX_META_TEXT = 6000
ENRICHED_META_PATH = Path("sources/youtube/enriched_meta.json")
CAPTIONS_DIR = Path("sources/youtube/captions")


def make_prefix(meta: dict) -> str:
    speakers = ", ".join(meta.get("speakers") or []) or "Unknown"
    summary = meta.get("summary", "")
    concepts = ", ".join(meta.get("key_concepts") or [])
    return (
        f"Title: {meta['title']}\n"
        f"Series: {meta.get('series','')}\n"
        f"Speakers: {speakers}\n"
        f"Key concepts: {concepts}\n"
        f"Summary: {summary}\n\n"
    )


def ingest_body_chunks(video_id: str, meta: dict, vc, index, dry_run: bool) -> int:
    txt_path = CAPTIONS_DIR / f"{video_id}.txt"
    if not txt_path.exists():
        print(f"  No caption file for {video_id}, skipping")
        return 0

    raw = txt_path.read_text(encoding="utf-8")
    text = clean_text(raw)
    chunks = chunk_text(text)
    if not chunks:
        return 0

    prefix = make_prefix(meta)
    embed_texts = [prefix + c for c in chunks]

    if dry_run:
        print(f"  Would upsert {len(chunks)} body chunks")
        return len(chunks)

    vectors = embed_chunks(embed_texts, vc)

    meta_base = {
        "source": "youtube",
        "chunk_type": "body",
        "video_id": video_id,
        "title": meta["title"],
        "series": meta.get("series", ""),
        "url": meta.get("url", f"https://www.youtube.com/watch?v={video_id}"),
        "speakers": json.dumps(meta.get("speakers") or []),
        "categories": json.dumps(meta.get("categories") or []),
        "key_concepts": json.dumps(meta.get("key_concepts") or []),
        "duration_sec": meta.get("duration_sec", 0),
    }

    records = []
    for i, (chunk, vector) in enumerate(zip(chunks, vectors)):
        m = {**meta_base, "chunk_index": i, "chunk_total": len(chunks), "text": chunk[:MAX_META_TEXT]}
        records.append({
            "id": f"{video_id}__body__{i:04d}",
            "values": vector,
            "metadata": m,
        })

    for i in range(0, len(records), PINECONE_BATCH):
        index.upsert(vectors=records[i:i + PINECONE_BATCH], namespace=NAMESPACE)

    return len(records)


def body_hash(video_id: str, meta: dict) -> str | None:
    """Fingerprint of everything that goes into a video's vectors."""
    txt_path = CAPTIONS_DIR / f"{video_id}.txt"
    if not txt_path.exists():
        return None
    fields = {k: meta.get(k) for k in ("title", "series", "speakers", "summary",
                                         "key_concepts", "categories", "duration_sec", "url")}
    blob = txt_path.read_text(encoding="utf-8") + json.dumps(fields, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def ingest_summary(video_id: str, meta: dict, vc, index, dry_run: bool) -> int:
    speakers = ", ".join(meta.get("speakers") or []) or "Unknown"
    concepts = ", ".join(meta.get("key_concepts") or [])
    categories = ", ".join(meta.get("categories") or [])
    summary = meta.get("summary", "")

    text = (
        f"Title: {meta['title']}\n"
        f"Series: {meta.get('series','')}\n"
        f"Speakers: {speakers}\n"
        f"Key concepts: {concepts}\n"
        f"Categories: {categories}\n"
        f"Summary: {summary}"
    )

    if dry_run:
        print(f"  Would upsert 1 video_summary vector")
        return 1

    vectors = embed_chunks([text], vc)
    record = {
        "id": f"{video_id}__video_summary",
        "values": vectors[0],
        "metadata": {
            "source": "youtube",
            "chunk_type": "video_summary",
            "video_id": video_id,
            "title": meta["title"],
            "series": meta.get("series", ""),
            "url": meta.get("url", f"https://www.youtube.com/watch?v={video_id}"),
            "speakers": json.dumps(meta.get("speakers") or []),
            "categories": json.dumps(meta.get("categories") or []),
            "key_concepts": json.dumps(meta.get("key_concepts") or []),
            "duration_sec": meta.get("duration_sec", 0),
            "text": text[:MAX_META_TEXT],
        },
    }
    index.upsert(vectors=[record], namespace=NAMESPACE)
    return 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", help="Ingest a single video ID")
    parser.add_argument("--type", choices=["body", "summaries", "all"], default="all")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="Re-embed even if unchanged")
    args = parser.parse_args()

    if not ENRICHED_META_PATH.exists():
        print(f"ERROR: {ENRICHED_META_PATH} not found. Run enrich_youtube.py first.")
        sys.exit(1)

    enriched = json.loads(ENRICHED_META_PATH.read_text())
    target_ids = [args.video] if args.video else list(enriched.keys())
    target_ids = [vid for vid in target_ids if vid in enriched]
    # Symposium recordings embed into the `symposium` namespace, where scoped
    # symposium questions can see them — not here. sync_symposium_videos.py.
    target_ids = [vid for vid in target_ids if enriched[vid].get("series") != "symposium-2026"]

    print(f"Processing {len(target_ids)} videos → namespace '{NAMESPACE}'")

    if args.dry_run:
        vc, index = None, None
    else:
        vc = get_voyage_client()
        index = get_pinecone_index()

    total_body = 0
    total_summary = 0
    # Per-video content state. Without it every run re-embedded all ~100 videos.
    state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}
    unchanged = 0

    for i, vid in enumerate(target_ids):
        meta = enriched[vid]
        title = meta["title"][:55]
        h = body_hash(vid, meta)
        if not args.force and h and state.get(vid, {}).get("hash") == h:
            unchanged += 1
            continue
        print(f"[{i+1}/{len(target_ids)}] {title}...")
        if not args.dry_run:
            # A shorter re-chunk would otherwise leave the old tail's vectors live.
            prev_n = state.get(vid, {}).get("body_chunks", 0)

        if args.type in ("body", "all"):
            n = ingest_body_chunks(vid, meta, vc, index, args.dry_run)
            total_body += n
            if not args.dry_run:
                print(f"  body: {n} chunks")

        if args.type in ("summaries", "all"):
            n = ingest_summary(vid, meta, vc, index, args.dry_run)
            total_summary += n

        if not args.dry_run and args.type == "all" and h:
            new_n = len(chunk_text(clean_text((CAPTIONS_DIR / f"{vid}.txt").read_text(encoding="utf-8"))))
            stale = [f"{vid}__body__{j:04d}" for j in range(new_n, prev_n)]
            if stale:
                index.delete(ids=stale, namespace=NAMESPACE)
            state[vid] = {"hash": h, "body_chunks": new_n}
            STATE_PATH.write_text(json.dumps(state, indent=2))

    print(f"\nUnchanged (skipped): {unchanged}")
    print(f"Total: {total_body} body chunks + {total_summary} summary vectors upserted to '{NAMESPACE}'")


if __name__ == "__main__":
    main()
