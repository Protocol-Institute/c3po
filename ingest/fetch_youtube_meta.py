"""
Fetch Protocol Institute YouTube metadata and download auto-captions.

Outputs:
  sources/youtube/video_meta.json      -- all videos, keyed by video_id
  sources/youtube/captions_raw/        -- raw .vtt files
  sources/youtube/captions/            -- clean .txt files (timestamps stripped)

Usage:
    python3 ingest/fetch_youtube_meta.py
    python3 ingest/fetch_youtube_meta.py --force          # re-download all captions
    python3 ingest/fetch_youtube_meta.py --video VIDEO_ID # single video
    python3 ingest/fetch_youtube_meta.py --dry-run        # list videos, no download
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Playlist → series mapping
# ---------------------------------------------------------------------------
PLAYLISTS = {
    "PLIk0EtKZjVlv8VMGoIrENsV_LP-bdr_28": "protocol-school-2025",
    "PLIk0EtKZjVltdB39Tzin_NqRyDxsapYGG": "bridge-atlas",
    "PLIk0EtKZjVltAKN-jscJHaGRe99xEK2LC": "town-hall",
    "PLIk0EtKZjVluisGvbw94g9ppIAw6JIcEl": "guest-talks-2025",
    "PLIk0EtKZjVlubAzl5w31GQdbo0yTbqXu-": "recommended",
    "PLIk0EtKZjVlvA3RUUQi9Bt7uZacjfJQXr": "popular-lectures",
    "PLIk0EtKZjVlsZ2BQDzA0-TIOMulYoVuC8": "symposium-2024",
    "PLIk0EtKZjVls1kkd9s75K7sdg-DX85sFx": "researcher-salon",
    "PLIk0EtKZjVlsgMwsz5ghqUEtbsvnI1tkm": "guest-talks",
    "PLIk0EtKZjVlvAnw12zyLw_Jp73ojcnKUj": "town-hall-new-nature",
}

# Priority order for series assignment when a video appears in multiple playlists.
# Earlier = higher priority (more specific series wins over generic buckets).
SERIES_PRIORITY = [
    "bridge-atlas",
    "researcher-salon",
    "symposium-2024",
    "protocol-school-2025",
    "town-hall",
    "town-hall-new-nature",
    "guest-talks-2025",
    "guest-talks",
    "popular-lectures",
    "recommended",
]

CHANNEL = "https://www.youtube.com/@protocol-institute"
OUT_DIR = Path("sources/youtube")
CAPTIONS_RAW_DIR = OUT_DIR / "captions_raw"
CAPTIONS_DIR = OUT_DIR / "captions"
META_PATH = OUT_DIR / "video_meta.json"


# ---------------------------------------------------------------------------
# VTT → clean text
# ---------------------------------------------------------------------------
def parse_vtt(vtt_text: str) -> str:
    """Strip VTT timestamps and tags, deduplicate repeated partial lines."""
    lines = vtt_text.splitlines()
    cleaned = []
    for line in lines:
        line = line.strip()
        # Skip: WEBVTT header, blank lines, timestamp lines, NOTE lines
        if not line:
            continue
        if line.startswith("WEBVTT") or line.startswith("NOTE") or line.startswith("Kind:") or line.startswith("Language:"):
            continue
        if re.match(r"^\d{2}:\d{2}:\d{2}[.,]\d{3}\s*-->\s*", line):
            continue
        if re.match(r"^\d+$", line):  # cue number
            continue
        # Strip inline timing tags like <00:01:23.456> and <c>
        line = re.sub(r"<[\d:.]+>", "", line)
        line = re.sub(r"</?c>", "", line).strip()
        if not line:
            continue
        # Deduplicate: VTT often emits partial lines then the completed line
        if cleaned and cleaned[-1].endswith(line):
            continue
        if cleaned and line.startswith(cleaned[-1]):
            cleaned[-1] = line
            continue
        cleaned.append(line)
    return " ".join(cleaned)


# ---------------------------------------------------------------------------
# yt-dlp wrappers
# ---------------------------------------------------------------------------
def fetch_playlist_videos(playlist_id: str) -> list[dict]:
    """Return list of {video_id, title, duration_sec, upload_date} from a playlist."""
    cmd = [
        "yt-dlp", "--flat-playlist",
        "--print", "%(id)s\t%(title)s\t%(duration)s\t%(upload_date)s",
        f"https://www.youtube.com/playlist?list={playlist_id}",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    videos = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        vid_id, title = parts[0], parts[1]
        if title == "[Private video]" or title == "[Deleted video]":
            continue
        duration_sec = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        upload_date = parts[3] if len(parts) > 3 else ""
        videos.append({
            "video_id": vid_id,
            "title": title,
            "duration_sec": duration_sec,
            "upload_date": upload_date,
        })
    return videos


def download_captions(video_id: str, out_dir: Path) -> Path | None:
    """Download English auto-captions for a video. Returns .vtt path or None."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cmd = [
            "yt-dlp",
            "--write-auto-subs",
            "--sub-langs", "en",
            "--sub-format", "vtt",
            "--skip-download",
            "--output", f"{tmpdir}/%(id)s.%(ext)s",
            f"https://www.youtube.com/watch?v={video_id}",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        # Find any .vtt file written
        vtt_files = list(Path(tmpdir).glob("*.vtt"))
        if not vtt_files:
            return None
        vtt_src = vtt_files[0]
        vtt_dst = out_dir / f"{video_id}.vtt"
        vtt_dst.write_bytes(vtt_src.read_bytes())
        return vtt_dst


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Re-download even if captions exist")
    parser.add_argument("--video", help="Process a single video ID only")
    parser.add_argument("--dry-run", action="store_true", help="List videos only, no download")
    args = parser.parse_args()

    CAPTIONS_RAW_DIR.mkdir(parents=True, exist_ok=True)
    CAPTIONS_DIR.mkdir(parents=True, exist_ok=True)

    # Load existing metadata
    meta: dict[str, dict] = {}
    if META_PATH.exists():
        meta = json.loads(META_PATH.read_text())

    # --- Step 1: Collect all videos from all playlists ---
    print("Fetching video lists from all playlists...")
    # video_id → list of series slugs (may appear in multiple playlists)
    video_series: dict[str, list[str]] = {}

    for playlist_id, series_slug in PLAYLISTS.items():
        print(f"  {series_slug} ({playlist_id[:20]}...)")
        videos = fetch_playlist_videos(playlist_id)
        print(f"    {len(videos)} videos")
        for v in videos:
            vid = v["video_id"]
            if vid not in video_series:
                video_series[vid] = []
                # Merge metadata
                if vid not in meta:
                    meta[vid] = {
                        "video_id": vid,
                        "title": v["title"],
                        "url": f"https://www.youtube.com/watch?v={vid}",
                        "duration_sec": v["duration_sec"],
                        "upload_date": v["upload_date"],
                        "playlist_ids": [],
                        "series": None,
                        "has_captions": False,
                    }
                meta[vid]["playlist_ids"] = list(set(meta[vid].get("playlist_ids", []) + [playlist_id]))
            video_series[vid].append(series_slug)

    # Assign canonical series by priority
    for vid, series_list in video_series.items():
        for s in SERIES_PRIORITY:
            if s in series_list:
                meta[vid]["series"] = s
                break
        else:
            meta[vid]["series"] = series_list[0] if series_list else "other"

    print(f"\nTotal unique videos: {len(meta)}")

    # Filter to single video if requested
    target_ids = [args.video] if args.video else list(meta.keys())

    # Bootstrap stub for --video IDs not found in any playlist
    if args.video and args.video not in meta:
        print(f"  Note: {args.video} not found in any tracked playlist — bootstrapping stub entry")
        meta[args.video] = {
            "video_id": args.video,
            "title": args.video,
            "url": f"https://www.youtube.com/watch?v={args.video}",
            "duration_sec": 0,
            "upload_date": "",
            "playlist_ids": [],
            "series": "other",
            "has_captions": False,
        }

    if args.dry_run:
        print("\nDry run — listing videos:")
        for vid in target_ids:
            v = meta.get(vid, {})
            mins = v.get("duration_sec", 0) // 60
            print(f"  [{v.get('series','?'):25s}] {v.get('title','?')[:60]} ({mins}m)")
        return

    # --- Step 2: Download captions ---
    print("\nDownloading captions...")
    success = 0
    skipped = 0
    failed = 0

    for i, vid in enumerate(target_ids):
        if vid not in meta:
            print(f"  Unknown video ID: {vid}")
            continue

        txt_path = CAPTIONS_DIR / f"{vid}.txt"
        if txt_path.exists() and not args.force:
            skipped += 1
            meta[vid]["has_captions"] = True
            continue

        title = meta[vid]["title"][:50]
        print(f"  [{i+1}/{len(target_ids)}] {title}...")

        vtt_path = download_captions(vid, CAPTIONS_RAW_DIR)
        if vtt_path is None:
            print(f"    No captions available")
            meta[vid]["has_captions"] = False
            failed += 1
        else:
            vtt_text = vtt_path.read_text(encoding="utf-8", errors="replace")
            clean = parse_vtt(vtt_text)
            if len(clean) < 200:
                print(f"    Captions too short ({len(clean)} chars) — skipping")
                meta[vid]["has_captions"] = False
                failed += 1
            else:
                txt_path.write_text(clean, encoding="utf-8")
                meta[vid]["has_captions"] = True
                word_count = len(clean.split())
                print(f"    OK — {word_count:,} words")
                success += 1

        # Save checkpoint after each video
        META_PATH.write_text(json.dumps(meta, indent=2, ensure_ascii=False))
        time.sleep(1.0)  # gentle rate limiting

    # Final save
    META_PATH.write_text(json.dumps(meta, indent=2, ensure_ascii=False))

    print(f"\nDone: {success} downloaded, {skipped} already cached, {failed} no captions")
    print(f"Metadata saved to {META_PATH}")


if __name__ == "__main__":
    main()
