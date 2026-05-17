# Plan: YouTube Transcript Ingest

**Status:** In progress  
**Namespace:** `videos`  
**Scripts:** `ingest/fetch_youtube_meta.py`, `ingest/enrich_youtube.py`, `ingest/ingest_youtube.py`

---

## Context

The Protocol Institute YouTube channel has ~83 unique videos, almost all 45–90 minutes long, organized into 8+ named series (Bridge Atlas, Guest Talk Series, Researcher Salon, Protocol Town Hall, Protocol School 2025, 2024 Protocol Symposium, Program Recordings, Popular Lectures). The content is 100% protocol-relevant — curated by PI itself — so no relevance filtering is needed.

Auto-generated English captions are available on all videos via YouTube's auto-caption system. `yt-dlp` can download these as clean text without touching the video files.

---

## Pipeline (3 scripts, mirroring PDF pattern)

### Script 1: `fetch_youtube_meta.py`

Fetches metadata and downloads captions for all channel videos.

**Input:** Channel URL + playlist IDs (hardcoded)  
**Output:** `sources/youtube/video_meta.json`, `sources/youtube/captions/{video_id}.txt`

Steps:
1. Run `yt-dlp --flat-playlist` across all playlist IDs to collect video metadata (id, title, duration, playlist name, upload date). Deduplicate by video ID. Skip private videos.
2. Assign `series` from playlist name (canonical mapping: playlist ID → series slug).
3. For each video: download English auto-captions via `yt-dlp --write-auto-subs --sub-format vtt --skip-download`. Save raw `.vtt` to `sources/youtube/captions_raw/{video_id}.vtt`.
4. Parse VTT → clean text: strip timestamp lines (`\d{2}:\d{2}:\d{2}.\d{3} --> ...`), strip `<...>` tags, deduplicate adjacent repeated lines (VTT often repeats partials), join into a single string.
5. Save clean text to `sources/youtube/captions/{video_id}.txt`.
6. Save `video_meta.json`: dict keyed by video ID, fields: `title`, `video_id`, `url`, `duration_sec`, `series`, `playlist_id`, `upload_date`, `has_captions`.

Idempotent: skip videos where `captions/{video_id}.txt` already exists unless `--force`.

---

### Script 2: `enrich_youtube.py`

Haiku enrichment pass: summary + categories + speakers per video.

**Input:** `sources/youtube/video_meta.json` + `sources/youtube/captions/{video_id}.txt`  
**Output:** `sources/youtube/enriched_meta.json`

For each video, send first 3,000 chars of clean transcript + title to Haiku. Ask for:
- `summary`: 2–3 sentences. Name the speaker and the specific argument/protocol concept addressed. No vague generalities.
- `categories`: 2–4 tags from shared vocabulary (same as PDF/Substack).
- `speakers`: list of names mentioned or identifiable from title.
- `key_concepts`: list of 3–5 protocol-related terms or concepts the talk focuses on.

Same checkpoint/idempotent pattern as `enrich_pdfs.py`: skip videos already in `enriched_meta.json` unless `--force`.

Cost estimate: ~83 videos × ~1,500 input tokens = ~125K tokens → ~$0.015 at Haiku pricing.

---

### Script 3: `ingest_youtube.py`

Embeds and upserts to Pinecone namespace `videos`.

**Input:** `sources/youtube/enriched_meta.json` + `sources/youtube/captions/{video_id}.txt`  
**Two vector types per video:**

**1. `body` chunks** (bulk of retrieval hits)
- Chunk clean transcript text with `chunk_text()` (512-token windows, 64-token overlap)
- Prefix each chunk for embedding (not stored): `"Title: {title}\nSpeakers: {speakers}\nSummary: {summary}\n\n{chunk}"`
- Metadata: `source=youtube`, `chunk_type=body`, `video_id`, `title`, `series`, `url`, `speakers`, `categories`, `chunk_index`, `chunk_total`, `text` (clean chunk, no prefix)
- ID: `{video_id}__body__{chunk_index:03d}`

**2. `video_summary` vector** (one per video)
- Text: full enriched summary + key_concepts + speakers + title
- Metadata: same fields + `chunk_type=video_summary`
- ID: `{video_id}__video_summary`

**CLI flags:** `--video VIDEO_ID`, `--type body|summaries`, `--dry-run`

---

## Pinecone Impact

~83 videos × ~60 min average × ~125 words/min ≈ 620K words → ~10,000–12,000 body chunks.  
Plus ~83 summary vectors.  
**Estimated: ~10,000–12,000 new vectors** in namespace `videos`.

---

## Series → Slug Mapping

| Playlist ID | Series slug | Label |
|---|---|---|
| `PLIk0EtKZjVlv8VMGoIrENsV_LP-bdr_28` | `protocol-school-2025` | Protocol School 2025 |
| `PLIk0EtKZjVltdB39Tzin_NqRyDxsapYGG` | `bridge-atlas` | Bridge Atlas |
| `PLIk0EtKZjVlubAzl5w31GQdbo0yTbqXu-` | `recommended` | Recommended Talks |
| `PLIk0EtKZjVltAKN-jscJHaGRe99xEK2LC` | `town-hall` | Protocol Town Hall Podcasts |
| `PLIk0EtKZjVluisGvbw94g9ppIAw6JIcEl` | `guest-talks-2025` | 2025 Guest Talks |
| `PLIk0EtKZjVltdB39Tzin_NqRyDxsapYGG` | `bridge-atlas` | Bridge Atlas |
| `PLIk0EtKZjVlvA3RUUQi9Bt7uZacjfJQXr` | `popular-lectures` | Popular Lectures |
| `PLIk0EtKZjVlsZ2BQDzA0-TIOMulYoVuC8` | `symposium-2024` | 2024 Protocol Symposium |
| `PLIk0EtKZjVls1kkd9s75K7sdg-DX85sFx` | `researcher-salon` | Researcher Salon Series |
| `PLIk0EtKZjVlsgMwsz5ghqUEtbsvnI1tkm` | `guest-talks` | Guest Talk Series |

Videos that appear in multiple playlists: assign the more specific series (e.g. `researcher-salon` over `recommended`).

---

## Sources Directory Layout

```
sources/youtube/
  video_meta.json          # all video metadata, keyed by video_id
  enriched_meta.json       # Haiku-enriched, keyed by video_id
  captions_raw/            # raw .vtt files (gitignored)
    {video_id}.vtt
  captions/                # clean .txt files (gitignored)
    {video_id}.txt
```

Add `sources/youtube/captions_raw/` and `sources/youtube/captions/` to `.gitignore`.

---

## Open Questions / Deferred

- **Speaker attribution per chunk:** Currently only video-level. Could add a pass to detect speaker-change markers in VTT for multi-speaker panels — deferred.
- **Sync / new video detection:** Like `sync_substack.py`, a future `sync_youtube.py` would periodically check for new uploads using the same playlist fetch + diff against `video_meta.json`. Deferred until initial ingest complete.
- **Description text:** YouTube video descriptions sometimes contain links, reading lists, or speaker bios. Could be added as a `description` chunk — deferred.
