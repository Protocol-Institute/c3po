# YouTube Integration Plan

Covers transcript extraction, slide content extraction, speaker diarization, and chunking strategy for the PI YouTube channel (talks and salon-style conversations). See `ARCHITECTURE.md` for where YouTube fits in the overall C3PO system.

---

## What's Available and What Isn't

| Content type | Extractable | Method | Quality |
|---|---|---|---|
| Spoken transcript | Yes | youtube-transcript-api or Whisper | Good–excellent |
| Speaker labels | Yes (with effort) | pyannote.audio diarization | Good |
| Chapter markers | Yes | yt-dlp `--dump-json` | Exact |
| Slide text | Yes | ffmpeg + OCR or Vision LLM | Good for text, limited for diagrams |
| Slide diagrams/charts | Partial | Vision LLM (Claude) | Described in prose, not structured |
| Video metadata | Yes | YouTube Data API v3 | Exact |
| Edit history / drafts | No | — | — |

---

## Transcript Extraction

### Primary: youtube-transcript-api

The practical default for public YouTube videos. Scrapes YouTube's internal `timedtext` endpoint — no API key required, works on any public video including auto-generated captions.

```python
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, PoTokenRequired

def get_transcript(video_id):
    try:
        segments = YouTubeTranscriptApi.get_transcript(video_id, languages=['en'])
        return segments  # [{text, start, duration}, ...]
    except PoTokenRequired:
        # YouTube increasingly requires PoTokens; retry once, then fall back
        raise
    except TranscriptsDisabled:
        return None  # Fall back to Whisper
```

Output format: list of `{text, start, duration}` dicts. Start/duration are in seconds. **No punctuation or capitalization** in auto-generated captions — requires a cleanup pass.

**Known fragility**: YouTube periodically changes the timedtext endpoint. The library maintainer patches within days, but brief outages happen. Implement retry logic and Whisper fallback.

**PoToken requirement (2025)**: YouTube now requires Proof-of-Origin tokens for some subtitle downloads. The library raises `PoTokenRequired` when this occurs. No automated workaround yet — the immediate fix is a retry with a short delay (token may not be required on retry), or fall back to Whisper.

### Alternative: Official YouTube Data API (for PI's own channel)

Since PI owns the channel, OAuth access to the official captions API is an option. This gives access to manually uploaded captions (better quality) and avoids the PoToken issue.

Quota cost: 200 units per `captions.download` call. Default quota is 10,000 units/day — sufficient for ~50 videos/day. For a bulk initial ingest of a larger archive, request a quota extension via Google Cloud Console.

This path requires OAuth 2.0 setup (service account or user auth). Worth the setup cost if the channel has high-quality manual captions.

### Fallback: Whisper

Use when: no captions exist, `TranscriptsDisabled`, or caption quality is poor (heavy accents, technical jargon, degraded audio).

Whisper advantages over YouTube auto-captions:
- Full punctuation and capitalization
- Better proper noun handling
- Better accent robustness
- Word-level timestamps (with WhisperX)

```python
import whisper

# Local inference (requires GPU for large-v3)
model = whisper.load_model("large-v3")
result = model.transcribe("audio.m4a", language="en")
# result['segments'] contains [{text, start, end}, ...]
```

Audio download via yt-dlp:
```bash
yt-dlp -f bestaudio --extract-audio --audio-format m4a \
  -o "%(id)s.%(ext)s" "https://youtube.com/watch?v=VIDEO_ID"
```

**yt-dlp ToS note**: Automated downloading technically violates YouTube's ToS (a civil matter, not criminal). For PI's own channel content, this is a non-issue practically and legally. yt-dlp has strong US legal precedent; use it.

**Cost**:
- OpenAI Whisper API: $0.006/minute → ~$0.36/hour of video
- Local (GPU): free per-run after hardware cost; RTX 4060 runs large-v3 at ~5× real-time

For a channel archive of moderate size (tens of hours), the OpenAI API is simpler and affordable. For ongoing ingestion at scale, local makes sense.

### Transcript cleanup

Auto-generated captions have no punctuation. A cheap Claude Haiku pass restores it before embedding:

```python
def clean_transcript(raw_text):
    # Claude Haiku: ~$0.001 per 1000 tokens — very cheap
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        messages=[{
            "role": "user",
            "content": f"Add punctuation and capitalization. Return only the corrected text:\n\n{raw_text}"
        }]
    )
    return response.content[0].text
```

---

## Speaker Diarization (Salon-Style Conversations)

Whisper alone does not identify speakers. For multi-speaker content, add pyannote.audio:

```python
from pyannote.audio import Pipeline

pipeline = Pipeline.from_pretrained(
    "pyannote/speaker-diarization-3.1",
    use_auth_token="HF_TOKEN"  # Hugging Face token required
)
diarization = pipeline("audio.m4a")

# Merge with Whisper transcript by timestamp alignment
for turn, _, speaker in diarization.itertracks(yield_label=True):
    print(f"{speaker}: {turn.start:.1f}s – {turn.end:.1f}s")
```

pyannote community-1 (2025) achieves 0.26 DER — the best open-source option. Runs locally on CPU (slow) or GPU (fast). Worth using for any video with 2+ speakers; skip for solo presentations.

The output labels speakers as `SPEAKER_00`, `SPEAKER_01`, etc. — anonymous but sufficient for segmenting a conversation. If speaker names are known (e.g., host and guest named in video description), map labels manually or via a short heuristic.

---

## Slide Content Extraction

### Step 1: Keyframe extraction (scene change detection)

ffmpeg detects visual changes between frames — a slide change triggers a scene change event:

```bash
ffmpeg -i video.mp4 \
  -filter_complex "select=gt(scene\,0.35),setpts=N/FRAME_RATE/TB" \
  -vsync vfr keyframes/frame_%04d.png
```

Threshold 0.35 is a good starting point for presentation videos. Too low → many near-duplicate frames. Too high → misses subtle slide transitions. Tune per video type.

For uniform sampling as fallback (simpler, catches more content):
```bash
ffmpeg -i video.mp4 -vf fps=0.5 keyframes/frame_%04d.png  # 1 frame every 2 seconds
```

### Step 2: Text extraction from keyframes

**Option A: Vision LLM (recommended for mixed content)**

Send each keyframe to Claude:

```python
import anthropic, base64

def describe_slide(image_path):
    with open(image_path, "rb") as f:
        image_data = base64.b64encode(f.read()).decode()
    
    response = client.messages.create(
        model="claude-sonnet-4-6",
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": image_data}
                },
                {
                    "type": "text",
                    "text": "Extract all text from this presentation slide verbatim. Then describe any diagrams, charts, or visual elements not captured by text alone. Format: TEXT: <verbatim text> | VISUAL: <description>"
                }
            ]
        }]
    )
    return response.content[0].text
```

Cost: ~$0.005–0.015 per slide (Claude Sonnet). For 100 keyframes per video: $0.50–$1.50/video. Handles diagrams, charts, and mixed content — OCR alone cannot.

**Option B: OCR only (cheaper, text-only slides)**

For slides that are pure text, open-source OCR (2025 models) now matches cloud services:

```bash
pip install paddleocr  # or: pip install surya-ocr
```

Cost: free (local). Best for high-volume pipelines where slide content is clean text.

**Option C: Hybrid** — OCR for text extraction, Vision LLM only when OCR confidence is low. Reduces cost while catching complex content.

### Step 3: Alignment with transcript

Each keyframe has a timestamp from ffmpeg. Match it to transcript segments:

```python
def align_slides_transcript(keyframes, transcript_segments):
    chunks = []
    for i, (kf_time, kf_text) in enumerate(keyframes):
        next_kf_time = keyframes[i+1][0] if i+1 < len(keyframes) else float('inf')
        
        # Transcript segments that fall in this slide's time window
        spoken = [
            seg['text'] for seg in transcript_segments
            if kf_time <= seg['start'] < next_kf_time
        ]
        
        chunks.append({
            "slide_number": i + 1,
            "timestamp_start": kf_time,
            "timestamp_end": next_kf_time,
            "slide_text": kf_text,
            "transcript": " ".join(spoken)
        })
    return chunks
```

---

## Chapter Markers

If the creator added chapter markers, they are the highest-quality segmentation signal — use them as primary chunk boundaries.

Extract via yt-dlp:
```bash
yt-dlp --dump-json "VIDEO_URL" | python3 -c "
import json, sys
d = json.load(sys.stdin)
for ch in d.get('chapters', []):
    print(ch['start_time'], ch['title'])
"
```

If chapters exist: chunk by chapter, with slide changes as sub-boundaries within each chapter.
If no chapters: chunk by slide change (scene detection) or by fixed time window (60–120 seconds).

---

## RAG Chunking Strategy

### For talks with slides

Primary boundary: slide change (scene detection timestamp).
Each chunk contains: slide text (OCR/Vision LLM) + transcript for that slide's duration.
Target size: 300–800 tokens. If a slide is on screen for a long time, sub-chunk by sentence.

### For salon-style conversations (no slides)

Primary boundary: chapter markers (if present), otherwise speaker turn (diarization).
Each chunk: one speaker turn or one thematic segment.
Target size: 200–500 tokens.

### Metadata per chunk

```python
{
  "video_id": "dQw4w9WgXcQ",
  "video_title": "Protocol Theory and Practice — a Conversation",
  "channel": "Protocol Institute",
  "publish_date": "2025-03-15",
  "duration_seconds": 3600,
  "chunk_type": "slide" | "chapter" | "turn" | "window",
  "timestamp_start": 920.0,   # seconds
  "timestamp_end": 1105.0,
  "slide_number": 7,          # null if no slides
  "chapter": "Part 2: Applications",  # null if no chapters
  "speaker": "SPEAKER_01",    # null if single-speaker or no diarization
  "has_slide_content": True,
  "url": "https://youtube.com/watch?v=dQw4w9WgXcQ&t=920"
}
```

The `url` with `&t=` timestamp links directly to the relevant moment in the video — useful for citations in C3PO responses.

---

## Ingestion Pipeline

```
For each video in channel:

1. Fetch metadata (YouTube Data API v3)
   - title, description, publish_date, duration, tags
   - chapters (or extract from description)
   - caption availability

2. Get transcript
   a. youtube-transcript-api (no auth, handles auto-captions)
   b. → PoTokenRequired or TranscriptsDisabled: fall back to Whisper
      - yt-dlp: download audio
      - Whisper large-v3: transcribe
      - (optional) pyannote: diarize if multi-speaker

3. Clean transcript
   - Claude Haiku punctuation pass (auto-captions only)

4. Extract slides (if content_type == "presentation")
   - yt-dlp or yt-dlp-mirror: download video
   - ffmpeg scene detection: extract keyframes
   - Vision LLM or OCR: extract slide text
   - Align keyframe timestamps with transcript

5. Chunk
   - By chapter (if available), then by slide or speaker turn
   - Build metadata dict per chunk

6. Embed + upsert
   - Voyage AI voyage-3: embed each chunk
   - Pinecone namespace "youtube": upsert with metadata
   - Update sources/youtube/registry.json: vector_count, last_ingested

7. Cleanup
   - Delete downloaded audio/video (source PDFs/assets not needed long-term)
   - Log any failures for manual review
```

---

## YouTube Data API Setup

Quota: 10,000 units/day free. For a 200-video channel:
- `channels.list` (get uploads playlist ID): 1 unit
- `playlistItems.list` (list all videos): ~4 units (50/page)
- `videos.list` (metadata per video): 200 units (1/video)
- Total: ~205 units for full channel metadata — well under limit

For caption download via official API (PI's own channel, better quality):
- `captions.download`: 200 units/video — only 50 videos/day on free quota
- For bulk initial ingest: request quota extension via Google Cloud Console

Add `YOUTUBE_API_KEY` (for public metadata) and optionally a service account JSON (for OAuth caption access) to `Code/.env.keys`.

---

## Known Issues and Gotchas

**youtube-transcript-api PoToken failures (2025):** Retry once; if still failing, fall back to Whisper. Track failure rate in `registry.json` — if consistently > 20%, shift to Whisper-first.

**Auto-caption quality on technical content:** Protocol theory vocabulary may be mangled. Whisper handles it better. Consider Whisper-first for all PI channel content rather than as a fallback.

**Slide detection on split-screen videos:** Speaker on left, slides on right — scene detection threshold may need tuning. Test on a sample video before bulk ingestion.

**Videos without chapters:** Most common case. Default to scene-detection chunking. For salon conversations, speaker turns via diarization.

**yt-dlp PoToken requirement (2026):** Some videos now require PoTokens or fresh browser cookies for download. If audio download fails, the pipeline falls to transcript-only (no slide extraction). Log these; they may require manual intervention.

**Long videos:** Talks over 2 hours produce large audio files. Whisper processes in chunks internally. No special handling needed, but local inference takes longer — GPU recommended.

**Incremental ingestion:** Store `last_checked` timestamp and video ID list in `registry.json`. Each run fetches new videos from the channel's uploads playlist since `last_checked`. Check for new videos daily via GitHub Actions cron.

---

## registry.json

```json
{
  "source": "youtube",
  "display_name": "Protocol Institute YouTube Channel",
  "pinecone_namespace": "youtube",
  "vector_count": 0,
  "last_ingested": null,
  "last_checked": null,
  "channel_id": "",
  "video_count_indexed": 0,
  "schema_version": 1,
  "access_level": "public",
  "freshness_cadence": "daily",
  "ingest_script": "ingest/ingest_youtube.py",
  "transcript_method": "youtube-transcript-api",
  "slide_extraction": true,
  "diarization": false,
  "notes": "Talks and salon-style conversations. Slide extraction via ffmpeg + Vision LLM. Whisper fallback for missing captions."
}
```
