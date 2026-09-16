# Protocol Symposium 2026 — Ingest Plan

**Status:** Design approved in outline (session 54, 2026-09-16); nothing implemented.
Phase A is the pre-event blocker. Phase B (slide decks) is **planned only — do not run until VGR says so.**

**Event:** Sept 21–25, 2026, fully virtual. Workshops Sept 21–22; talks Sept 23–25.
Registration closed at capacity; livestream public.

**Goal (VGR, session 54):** a **program content oracle**, not a scheduling app.
Through the symposium c3po should answer questions about the program, recommend
particular talks, and converse intelligently about the themes — connecting talks
to each other and to the archive.

**Explicitly out of scope** (VGR, narrowing the scope after the first draft of
this plan): being a schedule expert or doing precise calendar-assistant work.
The single temporal requirement is that c3po knows the current time well enough
that it **does not recommend a talk that has already happened**. Everything
beyond that — "what's on in 20 minutes", agenda arithmetic, timezone
conversion — is not a goal, and the plan should not carry machinery for it.

**Companion plans:** [`resource-pipeline.md`](resource-pipeline.md) (c3po is the
enrichment source, the website is a client — the same boundary holds here),
[`meeting-notes-ingest.md`](meeting-notes-ingest.md) (the audio-summary chunk
shape Phase C reuses), [`youtube-ingest.md`](youtube-ingest.md) (where the
recordings land).

---

## Phases

| Phase | Content | When | Blocker |
|---|---|---|---|
| **A** | Program metadata — 61 talks/workshops/interactives, 4 special sessions | **Before Sept 21** | none; data is live now |
| **B** | Slide decks and speaker docs from the Drive folder | **Done 2026-09-16** — 21 decks, `symposium_slides` | — |
| **C** | Recordings and transcripts | After Sept 25 | recordings do not exist yet |

The phases share one namespace and one identity scheme so that a deck and its
recording attach to the talk record Phase A already created.

---

## Source of truth

The program is **D1 behind a public JSON API on protocol-institute.org**, not
HTML to scrape. Scraping the rendered page would be the
[`pipeline_own_content_snapshots`](../CLAUDE.md) mistake again — our own page,
snapshotted, going stale, outranking the live record.

| Endpoint | Gives |
|---|---|
| `GET /api/symposium/proposals` | all 61 shortlisted items, with `workshop_sessions[]` nested on workshops |
| `GET /api/symposium/sessions` | the 4 special sessions with their block times |

Composition as of 2026-09-16: 54 talks, 5 workshops, 2 interactive. 51 carry
`scheduled_date`; the 5 workshops are anchored by website PR #14; the remaining
5 are the MRG talks inside "The Art of Memory" block, whose running order is set
by the session host on the day and may never be individually timed.

### Two things the ingest must not inherit

1. **PII.** Until website PR #13 merges, the proposals API returns
   `speaker_email`, `organizer_email`, `co_organizer_email` and `host3–5_email`
   — 51 distinct addresses — plus each submitter's private `comments` note and
   the member vote aggregates (`score`, `voter_count`, `total_votes`).
   **c3po's corpus is public and the bot is public.** The ingest applies its own
   field allowlist and never relies on the API having been fixed. The vote
   scores are internal deliberation and are excluded on the same grounds,
   whatever the API does.

2. **Dirty track values.** The same SIG appears under two spellings —
   `SIGPfB` and `SIGP4B (Protocols for Business)`, `MRG` and
   `MRG (Memory Research Group)`, `SIGFPT` and `SIGFPT (Formal Protocol Theory)`,
   `SIGPSY` and `SIGPSY (Historical Modeling / Psychistory)` (sic), plus
   `Alumni` / `Continuing alumni research (SoP 23-25 cohorts)`. Normalize to the
   canonical SIG keys c3po already uses before embedding, or "show me the MRG
   talks" under-reports. This is the same key-shape class as the
   `SIG_TITLE_MAP` bug — reuse `match_sig()` from `ingest/sync_meeting_notes.py`
   rather than writing a ninth SIG registry (see the standing TODO to
   consolidate the seven that already exist).

---

## Namespace and chunk types

A new **`symposium`** namespace rather than reuse of `sig`. The corpus is one
event, it needs to be re-syncable destructively while the program churns daily,
and it wants its own retrieval weight during the event week.

| chunk_type | One per | Carries |
|---|---|---|
| `symposium_session` | talk / workshop / interactive (61) | title, abstract, speaker + co-speakers, normalized track, session, day + UTC time, slug, permalink |
| `symposium_block` | special session (4) | the block's theme and what runs inside it — "Inventing Psychohistory", "The Art of Memory" are thematic groupings worth answering about as units. Per-day grid chunks were in the first draft and are **dropped**: that was scheduling-app machinery |
| `symposium_overview` | event (1) | dates, format, registration status, livestream, how workshops differ from talks |
| `symposium_workshop` | workshop (5) | `audience`, `takeaways`, `activities`, session times, registration URL — a separate chunk because these fields are what "converse about the themes of the workshops" actually needs, and they are far richer than the abstract |
| `symposium_slides` | deck section (Phase B) | |
| `symposium_transcript` | recording segment (Phase C) | |

**Vector ID:** `symposium__{slug}__{chunk_type}__{seq}`. The slug is the D1
`slug`, so a Phase B deck and a Phase C transcript attach to the same talk
without a second identity scheme.

**Weight:** 1.0×, matching `pdfs`/`substack`, and **unchanged after the event**
(VGR, session 54). Once the symposium is over the namespace is simply archival
material like any other — no decay schedule, no event-week boost to unwind. The
only thing that changes on Sept 26 is that the sync can stop running.

---

## Phase A — program metadata (pre-event)

New `ingest/sync_symposium.py`, wired into `bin/daemon.py`'s step list.
Remember `daemon.py`'s step list is read at process start: adding it needs
`sudo systemctl restart c3po-daemon`, not just the per-cycle self-pull.

**Change detection.** The program is still moving — the Open Mic session was
added 2026-09-15, six days before the event. Hash each record's embedded fields;
re-embed only what changed. Cheap enough to run every 30-minute cycle, which is
the right cadence this week and pointless after the 25th.

**Destructive re-sync per record.** When a record changes, delete its existing
vectors by `$eq` filter on the vector-ID prefix before upserting. Additive
upserts would leave a renamed talk indexed under both titles — the failure that
made the bot say "DRG has 0 archived sessions."

**State:** `data/symposium_state.json`, holding per-slug content hashes and the
last successful sync. Note the exe.dev lesson: `data/` is gitignored, so a fresh
VM clone re-derives from scratch. That is acceptable here (one API call, 61
records) but it means the first VM cycle re-embeds everything — budget one full
pass, not zero.

---

## Answering behaviors

### 1. Time awareness — a general c3po capability, not a symposium feature

VGR, session 54: c3po should in general know what date and time it is. The
symposium is what surfaced the gap, not the reason to fix it.

**c3po does not know what day it is today, on any path.** `SYSTEM_PROMPT` in
`api/worker.js` is a static template literal, and the user message is
`Question: {q}\n\nRelevant corpus excerpts:\n\n{context}`. No date reaches the
model from web, Discord or MCP.

**Correction to the working assumption: answers do not come from the VM.** The
VM runs the ingest daemon and the Discord *gateway*; `bin/c3po_bot.py:50` posts
to `https://c3po.protocolized.io/query`, so the gateway forwards and the
Cloudflare Worker generates the answer. Web, Discord and MCP all converge on
`worker.js`. That makes this a **single-site fix** rather than one per surface —
and the VM's system clock never enters the picture.

System time is available there: a Worker's `Date` is accurate wall-clock time,
always UTC. (Cloudflare freezes `Date.now()` during synchronous execution as a
timing-attack mitigation, so it advances only across I/O — irrelevant at the
minute granularity we need.) UTC is also the right unit: the program is stored
in UTC, and the Worker has no reliable signal about the asker's timezone.

Two pieces, and the split between them matters:

- **Static, in `SYSTEM_PROMPT`:** the instruction — that the current date and
  time are supplied in the user message and should be used when recency or
  ordering matters, including **not recommending sessions that have already
  happened**.
- **Dynamic, in the user message:** the timestamp itself, e.g.
  `Current date and time: Thursday 2026-09-24, 18:42 UTC.`

The timestamp **must not go in the system prompt.** That block is sent with
`cache_control: { type: "ephemeral" }`; a value that changes per request would
miss the prompt cache on every query c3po serves, symposium-related or not. In
the user message it costs nothing.

Each `symposium_session` chunk also carries its day and UTC time in the embedded
text, not only in metadata, so a retrieved excerpt arrives already stamped and
the model can compare it against the supplied now.

This is a **soft guarantee**, and deliberately so. It is the right strength for
an oracle: good enough that recommendations skew to what is still ahead, without
pretending to be a scheduling system.

### 2. Do not filter retrieval by date

Tempting and wrong. Mid-event, "what did Beiko argue about marking to reality"
is a perfectly good question about a talk that has already been delivered, and a
`scheduled_date >= today` filter would make the corpus go blind to the event as
it happens. Time-awareness shapes **recommendations**; it must not gate
**retrieval**.

Same caution as `feedback_intro_handler`: selection logic may decide which talks
to put forward, and must not be allowed to suppress or distort the answer.

### 3. The actual value — connecting dots

This is the part worth building well. Query `symposium` alongside the existing
namespaces so a recommendation or a thematic answer can say *why*: "Provenance
Against Fluency, Thursday — connects to the MRG thread on archival authenticity
from August, and to Kreutler's Interior Computing in the same block." Talk ↔
talk, talk ↔ SIG archive, talk ↔ the PDF corpus.

That cross-corpus grounding is the thing c3po can do that the program page
cannot, and it is the argument for a dedicated namespace queried alongside the
others rather than a standalone program-lookup tool.

### 4. No calls to action — a general stance, corpus-wide

VGR, session 54: the bot never issues calls to action. It **recommends material
and offers conversational insight about it** — it does not tell people to
register, sign up, buy, join, attend, subscribe or apply.

Like time awareness, this is a general rule rather than a symposium patch, and
belongs in the `VOICE` block of `SYSTEM_PROMPT` alongside "non-political" and
"honest about limits". It happens to matter acutely here — symposium
registration is closed at capacity, so a helpful-sounding "you can register at…"
would be both a CTA and wrong — but the same rule should keep c3po from pushing
Protocolized subscriptions or SIG sign-ups when someone asks a research
question.

The corollary for `symposium_overview`: state that registration is closed and
the livestream is public as **facts about the event**, phrased so the model has
no hook to turn them into an invitation.

### 5. Workshop themes — the `symposium_workshop` chunk

The five workshops carry `audience`, `takeaways` and `activities` that never
appear on the program page in full. That is the substance for a thematic
conversation, and it is why workshops get their own chunk type rather than being
flattened into `symposium_session`.

### Surfacing guarantee

`symposium_overview` is a single chunk and will lose a nearest-neighbour race
against 61 rich abstracts, so "what is the symposium" would depend on getting
lucky at `TOP_K_EACH`. Fire a parallel filtered sub-query for it, the way
`sig_meeting_page` already does.

## Phase B — slide decks (SHIPPED 2026-09-16, `ingest/sync_symposium_decks.py`)

**Outcome:** 30 files in the folder tree; 26 matched, 21 embedded, 5 are stubs or
image-only PDFs, 4 escalated for review. No credential needed — the folder is
world-readable and its page embeds its own inventory. Re-run it after the event
to capture finals.

Drive folder `1lyX7G4sKcZRNmHNN7EULT29SSwxK8J69`. As of 2026-09-16 it holds
**27 entries — 4 subfolders and 23 files** — against 61 program items, so
roughly a third populated, and VGR's note is that most are early drafts that
will be replaced by final versions.

Formats present: native Google Slides, `.pptx`, `.pdf`, `.docx`, `.html`.

### The hard part is not extraction, it is matching decks to talks

Filenames do not carry the slug and frequently do not carry the title:

| File | Talk |
|---|---|
| `Artisanal Bots` | Artisanal Bots with Protocolized RAG (Rao) |
| `TMDTS-Speaker-Notes.pdf` | Time Moves Down the Stack (Lohse) — initials only |
| `Cicero - A Common Protocol for…` | speaker surname as prefix |
| `NewNatureSlides_Fett.pptx` | The Inevitable Capture of Protocols (Fett) |
| `The House that Governs Itself.pdf` | unresolved — matches no program title |

**Resolution order:** exact slug → fuzzy title match (≥0.6, the threshold
`_find_mentioned_source()` already uses) → speaker surname match → explicit
override in `config/symposium_deck_map.json`. Anything still unmatched goes to a
**review queue and is never silently attached or silently dropped** — a deck
filed against the wrong talk is worse than an absent one, because the bot will
answer confidently from it.

### Draft churn is the defining constraint — how updates are handled

Decks will be replaced repeatedly up to the 25th and revised again afterwards;
the final version is the one that matters. Re-running the sync must converge on
the current folder, not accumulate every draft that ever existed.

**Key state on the Drive file `id`, never the filename.** Ids are stable across
renames and moves; names are not, and the deck→talk matching work makes it
tempting to key on them. Keyed by name, a rename reads as delete-plus-create and
churns the whole deck for nothing.

**Two-level change detection**, the shape `sync_symposium.py` already uses —
cheap signal to decide whether to fetch, content hash to decide whether to embed:

1. One `files.list` over the folder returning `id, name, mimeType, modifiedTime,
   md5Checksum, size, version`. One request, no downloads, every cycle.
2. Download/export only files whose cheap signal moved.
3. Hash the **extracted text**, not the bytes, to decide re-embedding.

Step 3 is not redundant. `modifiedTime` moves when someone opens a deck and
autosave fires, or nudges a text box; without the text hash that becomes ~40
wasted embeddings per idle edit. It also absorbs the format asymmetry below.

**Format asymmetry, worth knowing before relying on step 1.** Binary uploads
(`.pptx`, `.pdf`, `.docx`) carry an `md5Checksum` — a true content signal, free.
Native Google Slides are not blobs and have no checksum, so they fall back to
`modifiedTime`/`version`, which is noisier. Roughly half the folder is native
Slides, so those will re-export more often than strictly needed; the
extracted-text hash is what stops that becoming re-embedding.

**Removal must be handled, not just addition.** A deck withdrawn from the folder
has to have its chunks deleted or the bot keeps quoting it. Reconcile each run:
anything in state but absent from the listing is pruned. `sync_symposium.py`'s
`--prune` is the pattern.

**Deleting by explicit vector id, not by metadata filter.** An earlier draft of
this plan said "delete the file's existing chunks by filter" — that is wrong on
this index. Pinecone **serverless does not support delete-by-metadata-filter**,
which is why `sync_symposium.py` records each record's vector ids in its state
file and deletes those. Phase B must do the same.

**Subfolders.** 4 of the 27 entries are folders, so the listing has to recurse.
A file moving between folders keeps its id, which is another reason to key on id.

### The update cases — resolved by VGR, session 54

1. **Multiple files matching one talk — escalate, never auto-resolve.** People
   upload `deck v2.pptx` next to `deck.pptx` rather than overwriting, so two
   files match one talk. A newest-wins rule is wrong often enough to be unsafe:
   `katz_entrenching_privacy_through_speech.pptx` and
   `katz_on_speech_and_privacy.docx` are visibly a slides+notes pair, not a
   superseded draft, and newest-wins would silently discard half of it.
   **VGR's call: escalate conflicts to him.** Multi-file matches go to the same
   review queue as unmatched decks; the run reports them and continues with what
   it can resolve unambiguously. Nothing is discarded on a guess.

2. **Freshness is explicitly relaxed.** VGR: "no need to be super on time in
   catching up with edits." A speaker revising slides shortly before their talk
   may be a cycle behind, and that is fine for a content oracle.
   **Consequence worth taking: Phase B does not need to be a 30-minute daemon
   step at all.** Daily, or manual-on-trigger, is enough — which removes most of
   the pressure from the change-detection design above, cuts Drive API traffic to
   a trickle, and avoids adding a step that downloads large files to the same
   cycle that already carries `sync_sig`. The two-level scheme still earns its
   place at daily cadence; it just stops being load-bearing.

3. **What "final" means** — still open. The `is_final` flag needs a trigger.
   Simplest is a manual post-event pass; anything automatic has to guess when
   churn has stopped. Lowest priority of the three.

### Mechanics to settle before running

- **Drive access.** Public folder, but listing it programmatically wants the
  Drive API. Decide between an API key (register in `../.env.keys` and
  `../admin/keys.md` per policy) and `gdown`. Native Google Slides must be
  exported (`files.export`, PDF or text); binary formats download directly.
- **Extraction.** `.pptx` → `python-pptx` (speaker notes included — often more
  substantive than the slide text); `.pdf` → the existing `enrich_pdfs.py`
  path; `.docx` → `python-docx`; `.html` → strip.
- **New toolchain deps on the VM.** `python-pptx`/`python-docx` are not
  installed there. The exe.dev migration lesson was that undocumented deps
  surface as a broken daemon step; install and document them before wiring the
  step in, not after.
- **Cost.** Slide decks are mostly short text; the expense is per-chunk
  embedding, not Haiku. Should be well under a dollar for the whole folder, but
  measure the first run — `sync_sig` is already 98% of spend and this should not
  join it.

---

## Phase C — recordings and transcripts (post-event)

**Do not build a new pipeline.** Symposium talks will land on the PI YouTube
channel, and `videos` (3,127 vectors, 97 talks) plus
`ingest/enrich_youtube.py` already does this exact job — fetch, caption,
enrich, embed.

The right change is small: tag symposium videos with `symposium_slug` and
`symposium_year` metadata at ingest, so a recording joins the talk record from
Phase A rather than living as an unconnected video. Matching by video title
against program titles will have the same ambiguity Phase B has; reuse the same
resolver and the same override file.

If recordings arrive as raw audio with generated summaries instead of YouTube
uploads, the `sync_meeting_notes.py` path (`audio_meeting_summary` /
`audio_meeting_section`) is the closer fit. Decide when the format is known —
not now.

---

## Open questions for VGR

1. **Phase B trigger date.** Proposed: a first pass Sept 19–20 once decks firm
   up, a second after the event captures finals.

*Resolved in session 54:*
- **Post-event weight** — none. The namespace becomes ordinary archival material
  at the same 1.0×; nothing to unwind.
- **Registration and calls to action** — the bot never issues a CTA of any kind,
  corpus-wide. Registration status is stated as fact, never as an invitation.
- **Art of Memory running order** — not worth chasing. "Sometime in the Friday
  19:00–21:30 block" is a fine answer under the narrowed scope, and the host
  sets the order live.
