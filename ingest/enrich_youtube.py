"""
Enrich YouTube video metadata with Claude Sonnet (whole transcript): summary, categories, speakers, key concepts.

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
- "speakers": array of the people presenting — named in the title or introduced as presenters in the transcript. Exclude audience members who only ask questions, and moderators/hosts unless they contribute substantially. A presenter introduced by first name only stays first-name only
- "key_concepts": array of 3-5 specific protocol-related terms, frameworks, or concepts this talk focuses on

Return ONLY valid JSON, no other text."""


# Every video is enriched from the whole talk with Sonnet (session 56). The old
# setting — Haiku on the first 3000 chars — summarised the host's introduction
# and guessed speakers from mangled auto-captions: "Benitesh Raalo" for
# Venkatesh Rao, which also slipped past the VGR-authored intro filter.
MODEL = "claude-sonnet-5"      # VGR's standing call on dense material
EXCERPT_CHARS = 60000          # whole talk for all but the longest (median ~56K)

# Correct spellings of community members; auto-captions mangle names in
# consistent ways, and the model can only fix them if it knows the target.
KNOWN_PEOPLE_PATH = Path(__file__).resolve().parent.parent / "config" / "known_people.json"


def known_people() -> list[str]:
    try:
        return json.loads(KNOWN_PEOPLE_PATH.read_text())["names"]
    except (OSError, ValueError, KeyError):
        return []


def enrich_video(client: anthropic.Anthropic, video_id: str, meta: dict, captions_excerpt: str) -> dict:
    series = meta.get("series", "")
    title = meta.get("title", "")
    prog = meta.get("programme") or {}

    people = known_people()
    # The list is for correcting spelling, not for identifying people: given it
    # loosely, the model mapped a presenter introduced only as "Day" to Dorian
    # Taylor, a name that appears nowhere in the transcript.
    context = ("Correct spellings of some people in this community. Use one ONLY when the "
               "transcript's version is clearly a mis-transcription of that same full name "
               "(it sounds alike, e.g. 'Benitesh Raalo' = Venkatesh Rao). Never use the list "
               "to guess who an unnamed or first-name-only speaker is — keep the name as the "
               "transcript gives it. Do not list anyone the transcript does not show speaking:\n  "
               + ", ".join(people) + "\n\n") if people else ""
    if prog:
        context += (f"Programme listing for this talk (authoritative for names and spelling):\n"
                   f"  Title: {prog.get('title', '')}\n"
                   f"  Speakers: {', '.join(prog.get('speakers') or [])}\n"
                   f"  Abstract: {prog.get('abstract', '')}\n\n")
    label = "Transcript (auto-captions)"
    user_msg = f"""Video title: {title}
Series: {series}
Duration: {meta.get('duration_sec', 0) // 60} minutes

{context}{label}:
{captions_excerpt}"""

    model = MODEL
    response = client.messages.create(
        model=model,
        max_tokens=2048,   # 1024 truncated one long talk mid-JSON
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    log_api_call("enrich_youtube", model, response.usage)
    # Sonnet can lead with a thinking block; the answer is the text block.
    text = next(b.text for b in response.content if getattr(b, "type", "") == "text").strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text, strict=False)   # summaries can carry raw newlines/tabs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--video", help="Enrich a single video ID")
    parser.add_argument("--force", action="store_true", help="Re-enrich even if already in output")
    parser.add_argument("--series", help="Only videos in this series (e.g. symposium-2026)")
    parser.add_argument("--exclude-series", help="Skip videos in this series")
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
    if args.exclude_series:
        target_ids = [vid for vid in target_ids if video_meta[vid].get("series") != args.exclude_series]

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
        excerpt = caption_text[:EXCERPT_CHARS]

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

        time.sleep(0.3)  # gentle rate limiting

    OUT_PATH.write_text(json.dumps(enriched, indent=2, ensure_ascii=False))
    print(f"\nDone: {done} enriched, {skipped} skipped, {errors} errors")
    print(f"Saved to {OUT_PATH}")


if __name__ == "__main__":
    main()
