# Plan: Meeting Notes Ingest Pipeline

> Status: PLANNED — 2026-07-08

## Background

OpenRecapper-PI is a bot that records audio from PI voice channels (#whitehead-room, #kafka-room) and posts structured output to `#meeting-notes` (channel ID: 1519549380791631903). Content covers SIG calls and ad hoc working sessions.

**Currently:** meeting notes are NOT ingested into corpus and NOT published to the website. They exist only in Discord.

## Source format

Each meeting produces a burst of messages in #meeting-notes:

1. **Header message** (has file attachments):
   ```
   📝 **{title} {date}** — transcription complete for <#{voice_channel_id}>
   Duration: Xm Ys | Speakers: N | Requested by: <@uid>
   📁 Recordings:
   📝 Summary: https://pub-....r2.dev/recordings/{date}/{...}/summary.md
   📄 Transcript: https://pub-....r2.dev/recordings/{date}/{...}/transcript.txt
   🎬 Subtitles: ...
   ```
   Attachments: `summary.md`, `transcript.txt`, `transcript.srt`

2. **Summary section messages** (4–10 messages following the header):
   AI-structured sections — Key Points, Overview, Questions & Disagreements, etc. One Discord message per section.

3. **Raw transcript lines** (sometimes, posted live during the meeting):
   Format: `**Speaker | timezone:** utterance`. These are verbatim Deepgram output, one message per diarized utterance.

**Meeting types in the title:**
- SIG: `sigpsy 02July26 2026-07-02`, `sigfpt ...`, `drg ...`, etc.
- Ad hoc: `Ad hoc 2026-07-08`

**Voice channels:** `#whitehead-room` (1442955803471646792) and `#kafka-room` (1442956085215625446) — type=2 (voice).

## State / parsing approach

One state file: `data/meeting_notes_state.json` keyed by header message ID → `{processed_at, title, date, sig, r2_summary_url, r2_transcript_url, action1_done, action2_done}`.

The dedicated script reads #meeting-notes via Discord API, identifies header messages (by `📝` prefix + attachment presence), fetches content from R2 URLs (not Discord CDN — R2 URLs are permanent). Raw transcript lines and blank messages are ignored.

---

## Action 1: Website integration

**Goal:** enrich protocol-institute.org SIG meeting pages with audio-derived content (replacing the weaker Haiku-from-Discord summary where an audio recording exists).

**Script:** `ingest/enrich_meetings_from_audio.py`

**Logic:**
1. For each processed SIG meeting header in state:
   a. Parse SIG display name from title (e.g., "sigpsy" → SIGPSY).
   b. Look for matching meeting JSON in `data/sigs/meetings/` by SIG + date.
   c. If no matching entry exists: create a new one from the audio summary (participants from summary.md header, date from header message).
   d. Fetch `summary.md` from R2.
   e. Parse sections from summary.md:
      - `## 📖 The Reading` → `reading` field
      - `## 🧭 Overview` → `audio_overview`
      - `## 💡 Key Points & Themes` → `audio_key_insights` (list)
      - `## 🔀 Questions & Disagreements` → `audio_questions`
   f. Write enriched fields to meeting JSON.
   g. Run `update_sig_pages.py --sig {slug}` to regenerate website page.

**Template change needed:** `update_sig_pages.py` must be updated to render a new "Audio Summary" section in the detail page HTML when `audio_overview` or `audio_key_insights` is present. This is additive — existing Haiku summary stays as "Discussion Thread Summary"; audio content appears as a separate "Session Recording Summary" block.

**Ad hoc meetings:** Skip for website purposes. Ad hoc calls are not SIG-affiliated.

**Complexity:** Medium. Requires matching logic and a template addition to `update_sig_pages.py`.

---

## Action 2: Corpus ingestion

**Goal:** ingest the audio meeting summaries into Pinecone so C3PO can answer questions about meeting content, including meetings with no or sparse Discord thread discussion.

**Script:** `ingest/sync_meeting_notes.py`

**Logic:**
1. Scan #meeting-notes for header messages (OpenRecapper-PI author, `📝` prefix, has attachments).
2. For each new header (not in state):
   a. Parse title, date, source voice channel, R2 summary URL, speakers, duration.
   b. Determine SIG (from title) or classify as `ad_hoc`.
   c. Fetch `summary.md` from R2.
   d. Chunk by section (one vector per section, plus a combined summary vector).
   e. Embed via Voyage and upsert to Pinecone:
      - SIG meetings → `sig` namespace
      - Ad hoc → skip for now (content is too varied; can revisit)
   f. Metadata per vector:
      ```
      chunk_type: "audio_meeting_section" | "audio_meeting_summary"
      sig_display: "SIGPSY" | ...
      meeting_title: "sigpsy 02July26"
      date: "2026-07-02"
      voice_channel: "whitehead-room"
      speakers_count: N
      duration_mins: M
      section: "Overview" | "Key Points" | ...
      url: null  (no public URL yet; could link to meeting page once published)
      ```

3. State: update `data/meeting_notes_state.json` with `action2_done: true`.

**Full transcript (transcript.txt):** Do NOT ingest by default. It is long (~50K chars for a 50-min meeting), repetitive, and noisier than the structured summary. The summary already captures the substance. Revisit if there's a specific retrieval need for verbatim quotes.

**normalizeAudioMeeting() in worker.js:** New normalization function needed. Minimal: reuse `normalizeSig()` with `isMeetingSummary` path since the metadata schema is compatible. Or add `audio_meeting_summary` and `audio_meeting_section` as new cases in `buildContextBlock()`.

**Daemon integration:** Add as step 12.5 (after `sync_sig`, before `rebuild_sig_summaries`). This ensures audio content is ingested before the optional website enrichment step.

---

## Implementation order

1. **Now:** Add `#meeting-notes` to `config/discord_channels.json` (discord guide).
2. **Action 2 first:** `ingest/sync_meeting_notes.py` — self-contained, no website coordination needed. Then add worker.js support for new chunk types.
3. **Action 1 second:** `ingest/enrich_meetings_from_audio.py` + `update_sig_pages.py` template change. Depends on Action 2 state file being populated.

---

## Open questions

1. **Ad hoc meetings:** Ingest into corpus at all? They cover a wide range (working sessions, one-offs, demos). Could add a flag in the state so you can selectively mark ad hoc meetings as corpus-worthy.
2. **URL for audio sections:** Once a meeting detail page is published (Action 1), back-fill the `url` field in Pinecone vectors. Or accept null for now.
3. **Transcript verbatim:** If any SIG host specifically wants verbatim quotes searchable, add a flag per-meeting to also ingest `transcript.txt` in 500-token chunks.
4. **Matching audio to Discord threads:** For Action 1, if a SIGPSY meeting on Jul 2 has both a Discord thread (from sync_sig.py) and audio (from this pipeline), the audio summary is likely richer. The enrichment script should prefer audio where available.
