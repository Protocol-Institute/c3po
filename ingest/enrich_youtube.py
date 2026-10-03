"""
Enrich YouTube video metadata with Claude Haiku: summary, categories, speakers, key concepts.

Reads captions and titles from sources/youtube/.
Outputs sources/youtube/enriched_meta.json.

Run fetch_youtube_meta.py first.

Usage:
    python3 ingest/enrich_youtube.py
    python3 ingest/enrich_youtube.py --dry-run
    python3 ingest/enrich_youtube.py --video VIDEO_ID
    python3 ingest/enrich_youtube.py --force
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent))
from cost_logger import log_api_call

VIDEO_META_PATH = Path("sources/youtube/video_meta.json")
CAPTIONS_DIR = Path("sources/youtube/captions")
OUT_PATH = Path("sources/youtube/enriched_meta.json")

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

SYSTEM_PROMPT = f"""You are a metadata enrichment assistant for the Protocol Institute research corpus.
Given a video title, series name, and transcript excerpt, return a JSON object with:
- "summary": 2-3 sentences. Name the speaker(s) and their specific argument or protocol concept. Be concrete — no vague generalities.
- "categories": array of 2-4 tags from this fixed vocabulary: {json.dumps(CATEGORY_VOCAB)}
- "speakers": array of speaker names identifiable from the title or transcript (exclude moderators/hosts unless they contribute substantially)
- "key_concepts": array of 3-5 specific protocol-related terms, frameworks, or concepts this talk focuses on

Return ONLY valid JSON, no other text."""


DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_EXCERPT = 3000

# A video that sync_symposium_videos.py matched to a programme entry carries a
# `programme` block. Two things change for it: the model gets the listing's
# spelling of every name (auto-captions mangle them) and its abstract, and it
# reads the whole talk — the first 3000 chars of a symposium recording are
# mostly the host's introduction, so a summary from them describes the wrong
# speaker's framing. Sonnet, per VGR's standing call on dense material.
PROGRAMME_MODEL = "claude-sonnet-5"
PROGRAMME_EXCERPT = 60000


def enrich_video(client: anthropic.Anthropic, video_id: str, meta: dict, captions_excerpt: str) -> dict:
    series = meta.get("series", "")
    title = meta.get("title", "")
    prog = meta.get("programme") or {}

    context = ""
    if prog:
        context = (f"Programme listing for this talk (authoritative for names and spelling):\n"
                   f"  Title: {prog.get('title', '')}\n"
                   f"  Speakers: {', '.join(prog.get('speakers') or [])}\n"
                   f"  Abstract: {prog.get('abstract', '')}\n\n")
    label = "Transcript (auto-captions)" if prog else "Transcript excerpt (first ~3000 chars)"
    user_msg = f"""Video title: {title}
Series: {series}
Duration: {meta.get('duration_sec', 0) // 60} minutes

{context}{label}:
{captions_excerpt}"""

    model = PROGRAMME_MODEL if prog else DEFAULT_MODEL
    response = client.messages.create(
        model=model,
        max_tokens=512 if not prog else 1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    log_api_call("enrich_youtube", model, response.usage)
    text = response.content[0].text.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--video", help="Enrich a single video ID")
    parser.add_argument("--force", action="store_true", help="Re-enrich even if already in output")
    parser.add_argument("--series", help="Only videos in this series (e.g. symposium-2026)")
    args = parser.parse_args()

    if not VIDEO_META_PATH.exists():
        print(f"ERROR: {VIDEO_META_PATH} not found. Run fetch_youtube_meta.py first.")
        sys.exit(1)

    video_meta = json.loads(VIDEO_META_PATH.read_text())
    enriched: dict = {}
    if OUT_PATH.exists():
        enriched = json.loads(OUT_PATH.read_text())

    target_ids = [args.video] if args.video else list(video_meta.keys())
    # Only process videos that have captions
    target_ids = [vid for vid in target_ids if video_meta.get(vid, {}).get("has_captions")]
    if args.series:
        target_ids = [vid for vid in target_ids if video_meta[vid].get("series") == args.series]

    if args.dry_run:
        need = [vid for vid in target_ids if vid not in enriched or args.force]
        print(f"Would enrich {len(need)} videos (skipping {len(target_ids) - len(need)} already done)")
        for vid in need[:10]:
            print(f"  {video_meta[vid]['title'][:70]}")
        return

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    done = 0
    skipped = 0
    errors = 0

    for i, vid in enumerate(target_ids):
        if vid in enriched and not args.force:
            skipped += 1
            continue

        meta = video_meta[vid]
        txt_path = CAPTIONS_DIR / f"{vid}.txt"
        if not txt_path.exists():
            continue

        caption_text = txt_path.read_text(encoding="utf-8")
        excerpt = caption_text[:PROGRAMME_EXCERPT if meta.get("programme") else DEFAULT_EXCERPT]

        title = meta["title"][:60]
        print(f"[{i+1}/{len(target_ids)}] {title}...")

        try:
            result = enrich_video(client, vid, meta, excerpt)
            enriched[vid] = {**meta, **result}
            done += 1
            print(f"  OK — {result.get('summary','')[:80]}...")
        except Exception as e:
            print(f"  ERROR: {e}")
            errors += 1

        # Checkpoint every 10 videos
        if done % 10 == 0:
            OUT_PATH.write_text(json.dumps(enriched, indent=2, ensure_ascii=False))

        time.sleep(0.3)  # Haiku rate limiting

    OUT_PATH.write_text(json.dumps(enriched, indent=2, ensure_ascii=False))
    print(f"\nDone: {done} enriched, {skipped} skipped, {errors} errors")
    print(f"Saved to {OUT_PATH}")


if __name__ == "__main__":
    main()
