# C3PO — Roadmap

Phased implementation plan. See `ARCHITECTURE.md` for system design and `MIGRATION.md` for personal → PI org account migration.

Current repo: `vgururao/c3po` (personal). Migration to `Protocol-Institute/c3po` is Phase 9.

---

## Phase 1 — Core Corpus + Index *(in progress)*

**Goal:** Pinecone index populated and queryable via Python script.

- [x] Project structure: `ingest/`, `api/`, `submissions/`, `SOUL.md`
- [ ] Create Pinecone index `c3po` (serverless, aws us-east-1, 1024d cosine)
- [ ] `ingest/ingest_pdfs.py` — parse, chunk, embed, upsert PDF corpus
- [ ] `ingest/ingest_substack.py` — Substack export → embed → upsert
- [ ] Register `PINECONE_C3PO_HOST` in `Code/.env.keys`
- [ ] Verify retrieval quality with test queries
- [ ] Create `sources/pdfs/registry.json` and `sources/substack/registry.json`

Pinecone namespaces: `pdfs`, `substack`

---

## Phase 2 — CF Worker + MCP Server *(after Phase 1)*

**Goal:** Live query endpoint deployed to personal CF account; MCP server on same Worker; minimal web UI.

### CF Worker (`api/worker.js`)

- Embed query → Pinecone retrieve (public namespaces) → Claude Sonnet with SOUL.md
- Routes: `POST /query`, `GET /search`, `GET /health`
- Rate limiting: KV-backed, 20 queries/IP/hour unauthenticated
- Deploy to personal CF account: `c3po.protocolized.io` or `api.protocolized.io/c3po`
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
- `/.well-known/mcp.json` discovery endpoint on the Worker
- `llms.txt` updated with MCP connection instructions

### wrangler.toml (account-neutral)

```toml
name = "c3po"
main = "api/worker.js"
compatibility_date = "2025-01-01"

# Secrets (set via wrangler secret put — never committed):
# VOYAGE_API_KEY, PINECONE_API_KEY, PINECONE_C3PO_HOST,
# ANTHROPIC_API_KEY, ADMIN_KEY, MCP_API_KEY

[[kv_namespaces]]
binding = "RATE_LIMIT"
id = ""  # set per-account; document in MIGRATION.md
```

---

## Phase 3 — Discord Integration *(dual-purpose: ingestion + query)*

**Goal:** Discord bot that both ingests designated channels and answers queries.

### Ingestion

- `sources/discord/ingest.py` — Discord bot via `discord.py`
- Designated channels only (no DMs, no general channels unless explicitly scoped)
- Chunk by thread; metadata: channel, author handle (anonymized), timestamp
- Daily batch incremental indexing (only new messages since last run)
- Pinecone namespace: `discord`; `access_level: "member"` (requires SIWE to query)
- `sources/discord/registry.json` updated after each run

### Query interface

Bot commands in Discord:
- `/c3po <question>` — RAG query, responds with answer + cited sources
- `/c3po search <terms>` — semantic search
- `/c3po submit <url>` — add URL to submissions review queue
- `/c3po status` (admin only) — corpus stats

Deferred response pattern: Discord's 3-second timeout → bot acknowledges immediately, posts result when ready.

The Discord bot token and permissions are separate from the CF Worker secrets. Bot is registered under PI's Discord developer account (not personal) from the start — no migration needed.

---

## Phase 4 — YouTube Ingestion

**Goal:** PI YouTube channel content indexed and searchable.

- `sources/youtube/ingest.py`
- YouTube Data API v3 for video metadata (title, description, publish date, tags)
- YouTube transcript API for auto-generated captions; Whisper (`openai-whisper`) as fallback for videos without auto-captions
- Chunk by transcript segment with timestamps; metadata includes `video_id`, `timestamp_start`, `timestamp_end`
- CF Worker Cron Trigger for new videos (daily check against D1 `youtube_videos` table)
- Pinecone namespace: `youtube`; `access_level: "public"`
- `sources/youtube/registry.json`

Add `YOUTUBE_API_KEY` to `Code/.env.keys`.

---

## Phase 5 — Submission Portal

**Goal:** External contributors can submit resources; all go through review before indexing.

Three submission paths (see `ARCHITECTURE.md` for detail):
1. **URL submission** — server-side scrape → chunk → queue for review
2. **PDF upload** — stored in R2 (`c3po-submissions` bucket) → parse → queue
3. **GitHub PR** — add resource Markdown to `protocolized-website` → CI triggers `ingest_single.py` on merge

Review queue: admin UI at `/admin/submissions` (auth via `ADMIN_KEY`). Shows extracted text preview, metadata fields, accept/reject. Approved → Pinecone (`submissions` namespace) + optional protocolized-website resource card.

Submission form fields: title, authors, date, type, tags, description (280 chars), URL or file, license/permission note, submitter name/email (optional).

---

## Phase 6 — Content Monitoring Workers

**Goal:** CF Workers that continuously scan the internet for PI-relevant content.

Each monitoring worker is a CF Worker with a Cron Trigger:
- `workers/monitor-arxiv.js` — arXiv RSS for protocol-related papers (filter by keywords)
- `workers/monitor-feeds.js` — curated list of org/publication RSS feeds
- `workers/monitor-custom.js` — targeted crawl of specific sites (on-demand or scheduled)

**Relevance scoring before queuing:** each candidate document is scored by Claude Haiku against a relevance rubric before entering the submissions review queue. Low-scoring documents are discarded without human review.

**Human review:** all monitoring output goes to the same submissions review queue as Phase 5 — no automatic indexing from crawlers.

The list of monitored feeds/sites lives in `workers/monitor-config.json` (committed; editable without code change).

---

## Phase 7 — Slack Integration

**Goal:** PI team can query C3PO from Slack.

- Slack app with `/c3po` slash command
- CF Worker handles Slack Events API callbacks
- Deferred response pattern: immediate acknowledgment (`200 OK` within 3s) → async result posted to `response_url`
- Same query/search tools as Discord; no ingestion from Slack
- Auth: Slack signing secret validates requests; no per-user auth (relies on Slack workspace membership)

Add `SLACK_SIGNING_SECRET`, `SLACK_BOT_TOKEN` to `Code/.env.keys`.

---

## Phase 8 — Case Study Integration

**Goal:** Consulting case study data searchable by authorized users only.

- `sources/casestudies/ingest.py` — manual ingestion (no cron; each document requires explicit admin approval)
- Anonymization layer: client names, identifying details replaced with tokens before indexing; token map stored separately (never in Pinecone)
- Pinecone namespace: `casestudies`; `access_level: "private"`
- Consider separate Pinecone index for stronger isolation (prevents namespace misconfiguration from leaking private data)
- Query access: admin-only auth or explicit client-specific API key
- Separate from the main C3PO query route — `/query/casestudies` endpoint, distinct rate limiting and logging

This phase is intentionally deferred: the anonymization and access control design needs more thought than the other sources.

---

## Phase 9 — Migration to Protocol-Institute Org

**Goal:** Hand off the full system to the PI organization.

See `MIGRATION.md` for the complete step-by-step procedure. Summary:

| Resource | Migration method |
|---|---|
| GitHub repo | Transfer `vgururao/c3po` → `Protocol-Institute/c3po` |
| CF Worker | `wrangler deploy` with PI account credentials (code unchanged) |
| KV namespace | Create new in PI account; no data to migrate (rate limits are ephemeral) |
| Pinecone index | Option A: re-run ingestion scripts under PI org API key. Option B: export vectors via Pinecone API → import to new index under PI key. Re-ingestion is cleaner; vectors are deterministic given the same source content. |
| Voyage AI | PI org account API key; rotate `VOYAGE_API_KEY` in all scripts and Worker |
| Discord bot | Bot is already registered under PI Discord account (Phase 3) — no change |
| Slack app | Reinstall to PI Slack workspace if workspace changes |
| D1 / R2 | Create in PI account; migrate data (see `MIGRATION.md` for per-resource instructions) |

---

## Phase 10 — Agents-First Website *(future, design deferred)*

A dedicated site on a separate domain, designed around C3PO as the primary interface rather than a secondary widget. Characteristics:
- Multi-turn sessions with longer context
- Tool use: C3PO can query external sources, synthesize across the corpus, generate structured outputs
- Designed for researchers and practitioners, not casual visitors
- The protocolized.io web widget remains the primary human-accessible face

Domain, stack, and design to be decided when Phases 1–5 are stable and usage patterns are understood.

---

## Dependency Graph

```
Phase 1 (core corpus + index)
  └── Phase 2 (CF Worker + MCP) ← unlocks all delivery interfaces
        ├── Phase 3 (Discord integration)
        ├── Phase 4 (YouTube ingestion)
        ├── Phase 5 (submission portal) ← prerequisite for Phase 6
        │     └── Phase 6 (content monitoring workers)
        ├── Phase 7 (Slack integration)
        └── Phase 8 (case studies — can start any time, access control design first)

Phase 9 (PI org migration) — can happen after Phase 2; earlier = better
Phase 10 (agents-first site) — after Phase 5+
```

---

## Open Questions

- **Case study anonymization** (Phase 8): what level of anonymization is needed? Client-approved summaries vs. full transcripts vs. derived insights only?
- **Discord access level**: should Discord content be `member`-gated (SIWE required) or public? Depends on whether PI Discord channels are publicly readable.
- **YouTube transcription**: auto-captions vs. Whisper? Auto-captions are free and fast; Whisper costs compute but handles channels without auto-captions.
- **Monitoring worker targets** (Phase 6): initial list of arXiv keyword filters and RSS feeds to monitor — needs a working session to define.
- **Separate Pinecone index for case studies** (Phase 8): stronger isolation vs. more operational complexity. Decide before starting Phase 8.
- **Agents-first site domain** (Phase 10): TBD.
