# C3PO — Roadmap

Phased implementation plan. See `ARCHITECTURE.md` for system design and `MIGRATION.md` for personal → PI org account migration.

Current repo: `vgururao/c3po` (personal). Migration to `Protocol-Institute/c3po` is Phase 9.

**Design principle:** ingestion pipelines have no dependency on the CF Worker — they only need Python, a Pinecone index, and source credentials. All ingesters are built early to grow the corpus while the query layer is developed in parallel.

---

## Phase 1 — Core Corpus + Index *(in progress)*

**Goal:** Pinecone index created and populated with the two primary sources.

- [x] Project structure: `ingest/`, `api/`, `submissions/`, `SOUL.md`
- [ ] Create Pinecone index `c3po` (serverless, aws us-east-1, 1024d cosine)
- [ ] `ingest/ingest_pdfs.py` — parse, chunk, embed, upsert PDF corpus
- [ ] `ingest/ingest_substack.py` — Substack export → embed → upsert
- [ ] Register `PINECONE_C3PO_HOST` in `Code/.env.keys`
- [ ] Verify retrieval quality with test queries
- [ ] `sources/pdfs/registry.json`, `sources/substack/registry.json`

Pinecone namespaces: `pdfs`, `substack`

---

## Phase 1b — Discord Ingester *(parallel with Phase 1)*

**Goal:** Discord bot ingesting designated channels into Pinecone. No query interface yet — that comes in Phase 4 once the CF Worker exists. Full details in [`sources/discord/PLAN.md`](sources/discord/PLAN.md) — Ingestion section.

**Why early:** the corpus grows from day one; historical message backfill can run during Phase 2 development.

- [ ] Register Discord application under PI's Discord Developer account
- [ ] Enable `MESSAGE_CONTENT` privileged intent in Developer Portal
- [ ] `ingest/ingest_discord.py` — REST API batch crawl of whitelisted channels
  - Paginate message history (initial bulk); incremental by watermark thereafter
  - Download attachments to R2 immediately (CDN URLs expire after 24h)
  - Chunk by thread; metadata: channel, timestamp, anonymised author handle
- [ ] `sources/discord/registry.json` — per-channel `last_message_id` watermarks
- [ ] GitHub Actions cron: daily incremental run
- [ ] Pinecone namespace: `discord`; `access_level: "member"`

**No CF Worker, no slash commands, no query interface at this stage.**

---

## Phase 1c — Google Drive Ingester *(parallel with Phase 1)*

**Goal:** PI Google Drive content indexed into Pinecone. No YouTube slide matching yet — that coordination happens in Phase 3 once YouTube ingestion is built. Full details in [`sources/drive/PLAN.md`](sources/drive/PLAN.md) — Ingestion section.

**Why early:** Drive likely contains a significant fraction of the total corpus; indexing it early improves retrieval quality throughout development.

- [ ] Configure Google Cloud service account (read-only; share whitelisted folders)
- [ ] Populate `sources/drive/folder_config.json` with folder IDs and access levels
- [ ] `ingest/ingest_drive.py` — Changes API delta feed; extract by format:
  - Google Docs → Drive export as plain text
  - DOCX → `python-docx`
  - PDF → `pdfplumber`
  - Google Slides → Slides API (text + speaker notes per slide)
  - PPTX → `python-pptx`
  - Sheets → text cells only
- [ ] D1 table `drive_vectors` — maps file IDs to vector IDs for deletion tracking
- [ ] Initial bulk ingest of all whitelisted folders
- [ ] GitHub Actions cron: daily incremental run (Changes API)
- [ ] `sources/drive/registry.json` — `last_page_token` for delta feed
- [ ] Pinecone namespace: `drive`; access level per folder

**No slide-matching coordination with YouTube at this stage — that's added in Phase 3.**

---

## Phase 2 — CF Worker + MCP Server *(after Phase 1)*

**Goal:** Live query endpoint and MCP server deployed to personal CF account. By this point Phases 1b and 1c may have Discord and Drive content in the index too — the Worker queries all populated namespaces automatically via the CORPUS_MAP.

### CF Worker (`api/worker.js`)

- Embed query → Pinecone retrieve (public namespaces, or filtered by session) → Claude Sonnet with SOUL.md
- Routes: `POST /query`, `GET /search`, `GET /health`
- Rate limiting: KV-backed, 20 queries/IP/hour unauthenticated
- Deploy to personal CF account at `c3po.protocolized.io`
- No `account_id` in `wrangler.toml` — set via `CLOUDFLARE_ACCOUNT_ID` env var

**System prompt structure:**
```
[SOUL.md — identity, voice, corpus scope]
[CORPUS_MAP — built from source registry.json files at Worker startup]
[RETRIEVED_CHUNKS — top-K from Pinecone]
[USER_QUERY]
```

### MCP Server (`/mcp` route on same Worker)

- JSON-RPC 2.0 + SSE transport (CF Workers stream natively)
- Tools: `search_protocols`, `get_resource`, `ask_corpus`, `list_resources`
- Auth: API key via `Authorization: Bearer <key>`, rate-limited via KV
- `/.well-known/mcp.json` discovery endpoint
- `llms.txt` updated with MCP connection instructions

---

## Phase 3 — YouTube Ingestion *(after Phase 2; Drive data available from Phase 1c)*

**Goal:** PI YouTube channel content indexed. Drive slide matching runs here, using the Drive corpus already built in Phase 1c. Full pipeline in [`sources/youtube/PLAN.md`](sources/youtube/PLAN.md).

- [ ] `ingest/match_slides.py` — score YouTube videos against Drive files; populate `video_slide_mapping.json`
- [ ] Human review of candidate matches → confirm mapping
- [ ] `ingest/ingest_youtube.py`:
  - Metadata: YouTube Data API v3
  - Transcripts: `youtube-transcript-api` → Whisper fallback
  - Slides: Drive-first (from `video_slide_mapping.json`) → ffmpeg + Vision LLM fallback
  - Speaker diarization: pyannote.audio for multi-speaker videos
- [ ] `YOUTUBE_API_KEY` added to `Code/.env.keys`
- [ ] GitHub Actions cron: daily new-video check
- [ ] Pinecone namespace: `youtube`; `access_level: "public"`

By this point the corpus covers: PDFs, Substack, Discord, Drive, YouTube. The CF Worker from Phase 2 is already queryable over this full corpus.

---

## Phase 4 — Discord C3PO Agent *(after Phase 2)*

**Goal:** Add the query interface to the Discord bot built in Phase 1b. The ingester runs unchanged; slash commands are a new HTTP Interactions endpoint on the CF Worker. Full details in [`sources/discord/PLAN.md`](sources/discord/PLAN.md) — Query Interface section.

- [ ] Register HTTP Interactions endpoint URL in Discord Developer Portal → CF Worker
- [ ] Implement Ed25519 signature verification in Worker
- [ ] Slash commands: `/c3po <question>`, `/c3po search <terms>`, `/c3po submit <url>`, `/c3po status` (admin)
- [ ] Deferred response pattern (3s acknowledge → async result to `interaction.token`)
- [ ] `DISCORD_PUBLIC_KEY` added to Worker secrets

No changes to the ingestion bot from Phase 1b.

---

## Phase 5 — Submission Portal *(after Phase 2)*

**Goal:** External contributors can submit resources; all go through a review queue before indexing.

Three submission paths:
1. **URL submission** — CF Worker scrapes server-side → chunk → queue
2. **PDF upload** — stored in R2 (`c3po-submissions`) → parse → queue
3. **GitHub PR** — add resource Markdown to `protocolized-website` → CI triggers `ingest_single.py` on merge

Review queue: admin UI at `/admin/submissions` (auth via `ADMIN_KEY`). Approved → Pinecone (`submissions` namespace) + optional protocolized-website resource card.

---

## Phase 6 — Content Monitoring Workers *(after Phase 5)*

**Goal:** CF Workers scanning the internet for PI-relevant content; output goes to the Phase 5 review queue.

- `workers/monitor-arxiv.js` — arXiv RSS, keyword-filtered
- `workers/monitor-feeds.js` — curated org/publication RSS feeds
- `workers/monitor-custom.js` — targeted crawl on demand or schedule
- Claude Haiku relevance scoring before queuing (low scores discarded without human review)
- Config in `workers/monitor-config.json` (editable without code change)

---

## Phase 7 — Slack Integration *(after Phase 2)*

**Goal:** PI team can query C3PO from Slack via `/c3po` slash command.

- CF Worker handles Slack Events API callbacks
- Deferred response: acknowledge within 3s → POST result to `response_url` asynchronously
- No ingestion from Slack
- `SLACK_SIGNING_SECRET`, `SLACK_BOT_TOKEN` added to `Code/.env.keys`

---

## Phase 8 — Case Study Integration *(design-first, any time)*

**Goal:** Consulting case study data queryable by authorized users only. Deferred because anonymization and access control design needs dedicated thought.

- Manual ingestion only (no cron); each document requires explicit admin approval
- Anonymization layer: client identifiers replaced with tokens; token map never indexed
- Separate Pinecone index for isolation (not just a namespace)
- `/query/casestudies` endpoint, distinct from main query route

---

## Phase 9 — Migration to Protocol-Institute Org

See `MIGRATION.md` for the full procedure. Can happen any time after Phase 2.

| Resource | Migration method |
|---|---|
| GitHub repo | Transfer `vgururao/c3po` → `Protocol-Institute/c3po` |
| CF Worker | `wrangler deploy` with PI account credentials |
| KV namespace | Create new in PI account (ephemeral data, no migration needed) |
| Pinecone index | Re-ingest under PI org API key (vectors are deterministic) |
| Voyage AI | Rotate to PI org API key |
| Discord bot | Already registered under PI account — no change |
| Google Drive service account | Create new under PI Google Workspace; re-share folders |
| D1 / R2 | Export/copy to PI account (see `MIGRATION.md`) |

---

## Phase 10 — Agents-First Website *(future)*

Separate domain, C3PO as primary interface. Multi-turn sessions, tool use, longer context. Design deferred until Phase 5+ usage patterns are understood.

---

## Dependency Graph

```
Phase 1  (core corpus: PDFs + Substack)
Phase 1b (Discord ingester)        ← parallel; no CF Worker needed
Phase 1c (Drive ingester)          ← parallel; no CF Worker needed
  └── Phase 2 (CF Worker + MCP)    ← unlocks all query interfaces
        ├── Phase 3 (YouTube)      ← uses Drive data from Phase 1c
        ├── Phase 4 (Discord agent)← adds query to Phase 1b ingester
        ├── Phase 5 (submissions)
        │     └── Phase 6 (monitoring workers)
        └── Phase 7 (Slack)

Phase 8  (case studies)  — any time; access control design first
Phase 9  (PI org migration) — any time after Phase 2; earlier = better
Phase 10 (agents-first site) — after Phase 5+
```

---

## Open Questions

- **Discord access level**: `member`-gated (SIWE required) or public? Depends on whether PI Discord channels are intended to be publicly accessible content.
- **YouTube transcription**: `youtube-transcript-api` or Whisper-first? Whisper is higher quality for technical vocabulary; auto-captions are free and fast.
- **Monitoring worker targets** (Phase 6): arXiv keyword list, RSS feeds to monitor — needs a working session to define.
- **Case study anonymization** (Phase 8): client-approved summaries, full transcripts, or derived insights only?
- **Separate Pinecone index for case studies** (Phase 8): stronger isolation vs. more operational complexity.
- **Agents-first site domain** (Phase 10): TBD.
