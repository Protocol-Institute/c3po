# C3PO — Architecture

> Last updated: 2026-06-06 (session 31)

C3PO is the Protocol Institute's knowledge infrastructure: a multi-source corpus, a retrieval-augmented query engine, and a set of delivery interfaces. It is not a general chatbot; it is a research assistant that answers questions strictly within the PI corpus and cites its sources.

This document covers current live state and planned future work in one place. For the phased build log, see `DEVLOG.md`. For per-session notes and open TODOs, see `status.md`.

---

## What C3PO Is

C3PO serves two audiences simultaneously:

- **Web researchers** — anyone who visits `c3po.protocolized.io` and asks a question. Gets the full research-librarian voice: dense, source-specific, 3–5 paragraphs.
- **Discord members** — PI Discord community members who @mention the bot or use `/ask`. Gets an office-manager voice: 2–3 sentences, one named resource, direct.

The same corpus and query engine powers both. The persona — system prompt — is what differs.

The name is a deliberate reference to C-3PO, the Star Wars protocol droid described as "fluent in over six million forms of communication" and devoted to smooth protocol operation. The full identity and voice spec lives in `SOUL.md`.

---

## System Overview

```
┌────────────────────────────────────────────────────────────────────────┐
│  CORPUS SOURCES                                                         │
│                                                                         │
│  PDFs (82) ─────────────────────────────────────────────────────────┐  │
│  Substack (117+ posts, continuous) ─────────────────────────────────┤  │
│  YouTube (91 videos) ───────────────────────────────────────────────┤  │
│  Discord general channels ──────────────────────────────────────────┤─►│ chunk
│  Discord SIG channels ──────────────────────────────────────────────┤  │ enrich
│  Discord shared links (fetched + scored) ───────────────────────────┤  │ embed
│  Discord channel guide ─────────────────────────────────────────────┤  │ index
│  PI lexicon (914 terms, triage a/b) ────────────────────────────────┤  │
│  Bibliography (external works cited by PI corpus) ──────────────────┤  │
│  Bot conversation transcripts (self-memory) ────────────────────────┘  │
└─────────────────────────────────────────────────┬──────────────────────┘
                                                  │
                                         Voyage AI voyage-3
                                         1024-dim embeddings
                                                  │
                                         Pinecone index "c3po"
                                         1024d cosine · aws us-east-1
                                         PI org account
                                         10 namespaces · ~26,000 vectors
                                                  │
┌─────────────────────────────────────────────────▼──────────────────────┐
│  C3PO WORKER  (Cloudflare Worker · c3po.protocolized.io)               │
│                                                                         │
│  POST /query  →  embed → 10-namespace parallel query → merge →         │
│                  secondary retrieval → Claude Sonnet + system prompt   │
│                                                                         │
│  Routes: /query · /search · /mcp · /interactions · /chats · /health   │
│                                                                         │
│  Two system prompts:                                                    │
│    SYSTEM_PROMPT          — web, research librarian, 3–5 paragraphs    │
│    DISCORD_SYSTEM_PROMPT  — Discord, office manager, 2–3 sentences     │
│    context="discord" field in request body selects the variant         │
└────────────┬──────────────────────┬──────────────────────────────────┘
             │                      │
     ┌───────▼──────┐     ┌─────────▼──────────────────────────────────┐
     │  MCP Server   │     │  DELIVERY INTERFACES                        │
     │  /mcp (SSE)   │     │                                             │
     │               │     │  Web UI ·  c3po.protocolized.io             │
     │  Tools:        │     │                                             │
     │  search_corpus│     │  Discord bot — @mention + threads +         │
     │  ask_corpus   │     │    #introductions + slash commands           │
     │  get_resource │     │    (c3po_bot — discord.py gateway)          │
     │  list_sources │     │                                             │
     └───────────────┘     │  Discord slash commands — /ask /search      │
                           │    /help (via CF Queue deferred response)   │
                           │                                             │
                           │  Planned: Slack, email, agents-first site   │
                           └────────────────────────────────────────────┘
```

---

## Running Nodes

Three nodes are currently live, all under launchd or Cloudflare-managed:

| Node | Script | Type | Managed by | Logs |
|------|--------|------|------------|------|
| `c3po_listener` | `bin/daemon.py` | Ingest poller — REST, no gateway | launchd `org.protocol-institute.c3po.daily` | `~/Library/Logs/c3po/daemon.log` |
| `c3po_bot` | `bin/c3po_bot.py` | Discord gateway bot — WebSocket | launchd `org.protocol-institute.c3po-bot` | `~/Library/Logs/c3po/c3po_bot.log` |
| `c3po_web` | `api/worker.js` | Web chat + API — HTTP stateless | Cloudflare managed | KV store + `/api/chats` |

Node registry: `config/bot_registry.json` — canonical source for IDs, types, plist paths, log paths.

### c3po_listener

Runs a full sync cycle every 30 minutes via `bin/daemon.py`. Each cycle in order:

1. `sync_discord_channels.py` — rebuild `discord_guide` namespace from live guild channel list
2. `sync_discord_events.py` — fetch scheduled events → update cadence + next_event_time in channel registry
3. `sync_discord.py` — general channels (incremental by watermark) → `discord` namespace
4. `fetch_discord_links.py` — harvest URLs from discord+sig namespaces, fetch content (cap: 200/cycle)
5. `enrich_discord_links.py` — score each URL 0–3 for protocol relevance via Haiku; delete score-0
6. `sync_sig.py` — SIG channels (incremental + full thread rescan) → `sig` namespace
7. `rebuild_sig_summaries.py` — generate structured meeting summaries for new SIG meetings
8. `generate_sig_pages.py` — write SIG archive HTML; outputs both `sigs/{slug}.html` (relative paths) and `sigs/{slug}/index.html` (absolute paths, clean-URL format served at `/sigs/{slug}/`)
9. `generate_monitoring_page.py` — rebuild monitoring dashboard at `../website/monitoring.html`
10. Website push — `git commit + push` if any HTML files changed
11. `sync_bot_conversations.py` — drain `data/spool/bot_conversations/` → `transcripts` namespace
12. `sync_web_chats.py` — poll `/api/chats` for new public web conversations → `transcripts` namespace

Substack sync runs separately via GitHub Actions (`.github/workflows/sync-substack.yml`), daily at 08:00 UTC.

Per-cycle structured log: `~/Library/Logs/c3po/daemon_sessions.jsonl`.

### c3po_bot

Discord gateway bot (discord.py, WebSocket). Launchd-managed with KeepAlive — auto-restarts on crash or reboot.

**Interaction modes:**
- `@c3po` mention in any channel — opens a thread, responds with answer + sources
- Thread follow-up — continues for up to 5 turns without re-mention; passes full history to Worker. Cap notice sent exactly once (subsequent messages silently ignored). Messages that are replies to another human (not the bot) are skipped unless the bot is @mentioned — prevents responding to side conversations.
- `#introductions` monitoring — welcomes new members with 1 corpus resource + channel recommendation
- Nav-intent detection — "where should I post about X?" routes to `discord_guide` query instead of corpus RAG
- `/ask <question>` — Discord slash command (deferred via CF Queue, then followup webhook)
- `/search <query>` — sources only, no synthesis
- `/help` — brief description + web UI link

**Personality:** passes `"context": "discord"` in every Worker request → Worker uses `DISCORD_SYSTEM_PROMPT` (office manager: 2–3 sentences, one named resource, plain prose). Does NOT send `context` field for web callers — those get the full research librarian voice.

**Introductions handler specifics:**
- Only top-level posts (no replies)
- New-member filter: only members who joined > 30 days ago get automated welcome (recent joins handled differently)
- Corpus query: asks for single most relevant resource in 1 sentence
- VGR-authored results filtered out (ensures diverse recommendations)
- Cover-letter PDFs filtered out
- Suggested reading link: prefers the source whose title appears in Claude's answer (title-match scan of `all_sources[:8]`), so the link is coherent with what Claude recommended rather than being independently rank-ordered
- Fallback: Summer of Protocols Reader if all sources are filtered
- Running tally: `data/intro_recs_tally.json`

**Session log:** `~/Library/Logs/c3po/bot_sessions.jsonl`.

**Spool output:** completed conversations written to `data/spool/bot_conversations/{thread_id}_{turn}.json` for the listener to embed.

### c3po_web

Cloudflare Worker at `c3po.protocolized.io` (PI org account `7e8c7969b2464d23795c555bc6a32af8`). Stateless HTTP. All Worker routes:

| Route | Description |
|-------|-------------|
| `GET /` | Web UI (embedded HTML) |
| `POST /query` | Full RAG: embed → Pinecone → Claude |
| `GET /search` | Semantic search, sources only |
| `POST /interactions` | Discord slash command handler (Ed25519 sig verify + CF Queue enqueue) |
| `POST /mcp` | MCP server (JSON-RPC 2.0 + SSE) |
| `GET /chats` | Transcript browser (public + admin views) |
| `GET /chats/:id` | Individual transcript page |
| `POST /share` | Submit conversation for transcript archive |
| `GET /stats` | KV-backed usage aggregates |
| `GET /health` | Pinecone ping |
| `GET /how-it-works` | Static explainer page |
| `GET /terms` | Terms of use |
| `GET /api/chats` | Admin: list stored conversations |
| `GET /api/chat/:id` | Admin: fetch one conversation |

---

## Pinecone Index

**Index:** `c3po` — 1024 dimensions, cosine metric, serverless (aws us-east-1), PI org account.

| Namespace | Vectors | Source | Weight | Notes |
|-----------|---------|--------|--------|-------|
| `discord_links` | 9,640 | Community-shared URLs, fetched + Haiku-scored | 0.55–0.75× | Score 0 deleted; score bonus applied |
| `discord` | 5,578 | General + forum channels; starred msgs weighted | 0.85× | Starred: 1.0×, unstarred: 0.70× |
| `sig` | 5,311 | 6 SIG channels + 91 .org meeting pages | 0.90× | `sig_meeting_page` type gets parallel filtered query |
| `videos` | 2,940 | 91 PI YouTube talks | 0.90× | |
| `substack` | 1,080 | Protocolized magazine (118+ posts) | 1.0× | GitHub Actions daily sync |
| `definitions` | 560 | PI lexicon (914 terms, triage a/b) | 1.0× | |
| `pdfs` | 750 | 82 papers/essays | 1.0× | 11 cover letters/title pages absent from PI migration |
| `bibliography` | 278 | External works cited by PI corpus | 0.85× | Relevance-scored 0–3 |
| `discord_guide` | 78 | All active guild channels with blurbs + SIG cadence | — | Used only for nav/intro queries, not corpus RAG |
| `meta` | 31 | C3PO devlog sessions | — | Queried at top 3 alongside all other namespaces |
| `transcripts` | 22 | Bot self-memory: web + Discord conversations | 0.85× | Cache-hit threshold 0.52; tiered boost above 0.60 |
| **Total** | **~26,268** | | | |

Note: `humboldt` namespace is owned by the humboldt project and lives in its own Pinecone index — not present in the c3po PI org index.

**Secondary retrieval:** when a `doc_summary` or `post_summary` vector ranks in the top results, the worker fires a follow-up filtered query to fetch real body chunks from the same document. Summary hits are then replaced in the result set by their body-chunk siblings — giving Claude actual prose rather than an abstract.

**Transcript cache hits:** transcript vectors scoring ≥ 0.60 are extracted from regular sources and surfaced as "Similar conversation" links (score ≥ 0.60 → 1.10× boost, ≥ 0.52 → 0.92×, else 0.80×).

---

## Ingest Pipeline Pattern

Every corpus source follows the same three-layer pattern. Deviating requires explicit justification.

### Layer 1 — Haiku Enrichment

One Claude Haiku call per document. Input: title + authors + type/tags + first ~1,500 chars of text. Output saved to `sources/<type>/enriched_meta.json` (keyed by document ID):

```json
{
  "summary": "Two concrete sentences about the specific argument or contribution.",
  "categories": ["protocol-theory", "governance"],
  "primary_author": "Author Name",
  "all_authors": ["Author Name"]
}
```

Idempotent (skips existing entries unless `--force`). Checkpoints every 10 documents.

**Shared category vocabulary** (used across all sources for cross-corpus query consistency):
`protocol-fiction` · `protocol-theory` · `protocol-watching` · `editorial` · `research-report` · `technology-ai` · `governance` · `announcement` · `interview` · `memory-archival` · `organizations`

### Layer 2 — Body Chunks

Text chunked at 512-token windows with 64-token overlap. A structured prefix is prepended to each chunk before embedding (to anchor chunk context) but is NOT stored in Pinecone metadata (stored text is clean, readable). IDs are SHA-256 hashes of raw chunk text — duplicate passages across documents are automatically deduplicated.

Prefix format:
```
Title: {title}
Authors: {authors}
Type: {type}
Summary: {summary}

{chunk text}
```

### Layer 3 — Document Summary Vector

One additional vector per document for "what is this document about" queries. Vector ID: `{id}__doc_summary` (or `{slug}__post_summary` for Substack). Contains full enrichment metadata, not a body chunk.

### Optional Layer — Collection/Series Vectors

Used for Substack: one vector per named collection (SIGFPT, Ghosts in Machines, Zoothesia, etc.) listing all member posts — enables queries like "what are the themes in Ghosts in Machines?"

---

## Source Notes

### PDFs
82 documents from Summer of Protocols (53 with public R2 URLs at `files.protocolized.io`, remainder at various origins). 5 are image-only (no body chunks, doc_summary only). 11 deprecated (cover letters, title pages) — excluded from intro recs, still retrievable for meta queries. Ingested once; re-ingest triggered manually when PDF set changes.

### Substack
117+ posts across 4 sections: **Fictions** (58 posts), **Articles** (47), **Obliquities** (5 — VGR editorial), **Protocolized** (5 default). Continuous sync via GitHub Actions daily cron. New posts: full enrichment + all vector types. Edited posts: re-embed body + post_summary; re-enrich via Haiku only if wordcount delta >15%. Tag changes: re-embed affected collection cards.

### YouTube
91 videos across 6 series (Guest Talks, Town Halls, Protocol School 2025, Researcher Salons, Symposium 2024, Bridge Atlas). English auto-captions downloaded via `yt-dlp`, parsed (timestamps + deduplication), chunked. Speaker introductions in transcript openings make Haiku enrichment notably richer than for PDFs.

### Discord
Two namespaces:
- **`discord`**: general channels + forum channels (#idle-protocol-musings, #protocol-watch, #credit-protocols, #death-memory, #unconscious-protocols, #tech-standards, #built-environment, #organizational-protocols, #field-reports, #affiliate-chat, #reading-room, others). Incremental by message-ID watermark. Starred messages weighted 1.0× (vs. 0.85× for unstarred). Forum posts (Discord type=15) bundled as thread chunks.
- **`sig`**: six SIG channels (SIGFPT, MRG, SIGPfB, ProtFiSIG, SIGPSY, DRG). Meeting threads detected by per-channel name patterns. AI-generated meeting summaries via Claude. Also includes 91 published `.org` meeting pages as `sig_meeting_page` chunks (absolute URLs for deep-link citations). `sync_discord_channels.py` auto-seeds `sig_display` from `channel_manifest.json` for SIG-type channels, enabling calendar event matching for new SIGs.

Attachment CDN URLs expire after 24h — downloads happen at ingest time.

### Discord Links
~9,200 community-shared URLs harvested from the `discord` and `sig` namespaces, fetched server-side, and scored 0–3 for protocol relevance by Claude Haiku. Score-0 entries deleted from Pinecone. Score used as a retrieval weight multiplier. 260 Twitter/X URLs deferred (need paid API). 17 YouTube links fetched via transcript API.

### Discord Guide
78 active guild channels with Haiku-generated blurbs, SIG cadence, and next scheduled event time. Used only for intro/nav queries — not included in corpus RAG. Rebuilt on every listener cycle by `sync_discord_channels.py`.

### Definitions
560 vectors from the PI lexicon (914 terms extracted from PDFs by `extract_lexicon.py`, triage a+b ingested). Vector IDs: `lexicon__{term_slug}__{source_slug}`. Retrieved on demand like any other corpus material — the system prompt includes a 40-term anchor block for the most frequently needed terms, everything else surfaces via semantic retrieval.

### Bibliography
278 vectors from external works cited by PI corpus authors. Scored 0–3 for protocol relevance; relevance stored as retrieval weight.

### Transcripts
Bot self-memory. Two `chunk_type` values: `discord_conversation` (from Discord bot spool) and `web_conversation` (from public web chats). 0.85× weight; cache hits above 0.60 threshold get tiered boost and are surfaced as "Similar conversation" links rather than regular sources.

---

## Personality Split

The Worker maintains two system prompts and selects between them based on a `context` field in the request body:

| Context | Prompt | Voice | Length |
|---------|--------|-------|--------|
| *(none / web)* | `SYSTEM_PROMPT` | Research librarian: scholarly, dense, source-specific | 3–5 paragraphs, ~350–500 words |
| `"discord"` | `DISCORD_SYSTEM_PROMPT` | Office manager: friendly, businesslike, direct | 2–3 sentences max |

The Discord variant appends a `DISCORD VOICE OVERRIDE` block to the base prompt, superseding the voice and length instructions. Both variants are long enough for Anthropic's prompt cache (`cache_control: ephemeral`) — they cache independently.

The Discord bot passes `"context": "discord"` on every `call_worker()` call. The MCP server and web UI do not pass `context`, so they always get the research librarian voice.

---

## Infrastructure

| Resource | Details |
|----------|---------|
| **Cloudflare account** | PI org (`7e8c7969b2464d23795c555bc6a32af8`) — Worker, KV, Queue |
| **Worker live URL** | `https://c3po.protocolized.io` (custom domain on protocolized.io zone) |
| **Workers subdomain** | `c3po.team-7e8.workers.dev` |
| **GitHub repo** | `Protocol-Institute/c3po` (transferred from `vgururao/c3po` 2026-05-31) |
| **Pinecone** | PI org account; index `c3po` at `c3po-1os2tli.svc.aped-4627-b74a.pinecone.io` |
| **Voyage AI** | PI org account; model `voyage-3` (1024d) |
| **Discord bots** | `DISCORD_BOT_TOKEN` (listener/ingest) · `ORACLE_BOT_TOKEN` (oracle/query) |
| **Local runtime** | macOS · launchd · `/opt/homebrew/bin/python3` (3.14) · `.venv/` |
| **Substack sync** | GitHub Actions `.github/workflows/sync-substack.yml` (daily 08:00 UTC) |

Worker secrets (all set via `wrangler secret put`): `VOYAGE_API_KEY`, `PINECONE_API_KEY`, `PINECONE_C3PO_HOST`, `ANTHROPIC_API_KEY`, `ADMIN_KEY`, `MCP_API_KEY`, `DISCORD_BOT_TOKEN`, `ORACLE_BOT_TOKEN`, `ORACLE_APPLICATION_ID`, `ORACLE_PUBLIC_KEY`.

**Deploy command:**
```bash
cd api/
CLOUDFLARE_API_TOKEN=$(grep CLOUDFLARE_API_TOKEN ../../.env.keys | cut -d= -f2) \
CLOUDFLARE_ACCOUNT_ID=7e8c7969b2464d23795c555bc6a32af8 \
npx wrangler deploy
```

**Launchd management:**
```bash
launchctl list | grep protocol-institute          # check status
launchctl kickstart -k gui/$(id -u)/org.protocol-institute.c3po-bot  # restart bot
launchctl kickstart -k gui/$(id -u)/org.protocol-institute.c3po.daily  # restart listener
```

---

## Registry Pattern

C3PO uses config files (not code) as the authoritative registry for sources, sinks, and nodes.

| File | Purpose |
|------|---------|
| `config/source_registry.json` | 11 sources: type, namespace, ownership (`owned`/`subscribed`/`aware`), ingest script |
| `config/sink_registry.json` | 7 sinks: 3 active (web_ui, discord_bot, mcp), 4 planned |
| `config/corpus_map.json` | 10 namespaces: ownership, vector counts, query weights |
| `config/bot_registry.json` | 3 nodes: id, type, plist path, log paths, spool path |
| `config/discord_channels.json` | Channel guide: blurbs, cadence, next_event_time, SIG metadata |

Adding a new source: create the ingest script, add an entry to `source_registry.json` and `corpus_map.json`, run once, add to daemon cycle if incremental.

---

## Design Principles

**One ingest owner.** Only `c3po_listener` (daemon.py) writes to Pinecone. All other nodes spool output locally; the listener picks it up each cycle. No concurrent writers.

**Stateless gateways.** `c3po_bot` and `c3po_web` hold no persistent state. They call the Worker API and optionally spool Q&A output. Fully restartable without data loss.

**Spool as inbox.** `data/spool/` is a bounded queue. Any node drops a JSON file; the listener embeds and deletes it on the next cycle. Future nodes (Slack, email) are new spool writers — no pipeline changes.

**bot_id on every transcript vector.** Each ingested conversation carries a `bot_id` field (`c3po_bot`, `c3po_web`). Enables per-node memory queries.

**Registry-driven.** Config files (not code constants) describe what nodes and sources exist. CLAUDE.md and the monitoring page read from these.

**Scope separation.** C3PO is the AI backend. Web rendering (routes, HTML, D1, R2 image hosting) belongs in `protocol-institute/protocolized-website`. Static org content belongs in `protocol-institute/website`. When a request touches both layers, the right split is: ingest/embed/query → c3po; display/storage/routes → protocolized-website.

---

## Build History

| Sessions | Date | What shipped |
|----------|------|--------------|
| 1–2 | 2026-05-14 | Substack corpus (1,040 vectors): API archaeology, section/tag/collection/author mapping, four vector types, `sync_substack.py` |
| 3 | 2026-05-14 | Devlog system, PI admin repo, expense tracking framework |
| 4 | 2026-05-15 | PDF corpus (766 vectors): canonical three-layer ingest pattern, Haiku enrichment, `ingest_pdfs.py` |
| 5 | 2026-05-15 | Oracle Worker v1 (`api/worker.js`): RAG endpoint, embedded web UI, PI design tokens, secondary retrieval, Sonnet throughout |
| 6–7 | 2026-05-16–17 | Security filters (injection/sysextract/credential/KBA), transcript loop, `/chats` page, curly-quote root-cause fix, shared subnav, How It Works + Terms pages |
| 8 | 2026-05-17–18 | YouTube ingest (2,940 vectors): caption pipeline, Haiku enrichment; bibliography mining (278 vectors); full-corpus lexicon extraction (914 terms from 66 PDFs via Haiku) |
| 9–11 | 2026-05-19 | Discord ingestion (5,552 vectors): incremental by watermark, star weighting, forum channel support; security hardening (IP strike/ban, history smuggling, DARKBECOME/WIELD filters); PI website updated |
| 12–13 | 2026-05-20 | Discord links pipeline (9,188 vectors): URL harvest → fetch → Haiku relevance scoring → delete score-0; all namespaces wired into Worker |
| 14–15 | 2026-05-20 | Exhibit extraction plan; Discord Bot Design doc; missed SIG meetings fix (active-thread archival bug) |
| 16–18 | 2026-05-20 | Channel manifest + `onboard_channel.py`; daily launchd plist; lexicon namespace (560 vectors); attachment capture; monitoring dashboard |
| 19 | 2026-05-26 | Oracle bot code complete: `/interactions` endpoint, CF Queue consumer, `/ask /search /help` slash commands |
| 20–21 | 2026-05-27 | Oracle bot live: Discord app created, secrets set, commands registered; thread continuation (5 turns with history); `#introductions` monitoring |
| 22 | 2026-05-28 | Pubsub refactor: `source_registry`, `sink_registry`, `corpus_map`, BaseSource ABC, `bot_registry`; Durable AI Adoption guide ingested |
| 23 | 2026-05-28 | SIG meeting page ingest (91 pages → `sig` namespace); bot ecology Phase A (launchd for c3po_bot, per-conversation session logging) |
| 24 | 2026-05-28 | Bot ecology Phases B+C: Discord conversation spool + `sync_bot_conversations.py`; web chat ingest (`sync_web_chats.py`); transcript cache-hit warm-return |
| 25 | 2026-05-29 | `discord_guide` namespace (78 vectors): channel blurbs + SIG cadence + scheduled events via `sync_discord_channels.py` + `sync_discord_events.py`; intro handler shows next meeting time |
| 26 | 2026-05-30 | Welcome queue (`welcome_queue.py`); intro recs overhaul (VGR filter, fallback source, tally); Phase D (bot_registry, shared session_log.py, monitoring page); nav query handler; GitHub Actions cron for Substack |
| 27 | 2026-05-31 | Full PI org migration: CF Worker → PI account + custom domain; Pinecone → PI org; Voyage → PI org; GitHub repo transfer `vgururao` → `Protocol-Institute` |
| 28 | 2026-05-31 | Systemic PDF URL fix (487 vectors updated via `fix_pdf_urls.py`); cover-letter filter in intro recs; forward-only watermark + new-member filter in intro handler |
| 29 | 2026-06-01 | Discord/web personality split: `DISCORD_SYSTEM_PROMPT` (office manager, 2–3 sentences) vs `SYSTEM_PROMPT` (research librarian, 3–5 paragraphs); CLAUDE.md scope boundaries |
| 30 | 2026-06-03 | VPS migration plan (`plans/vps-migration.md`): Hetzner CX22, 6-phase plan to move bot + daemon off laptop; personal infra cleanup plan |
| 31 | 2026-06-06 | SIGPSY + DRG onboarded (108 vectors); `generate_sig_pages.py` now writes clean-URL index format; `sync_discord_channels.py` auto-seeds `sig_display` from manifest; bot fixes: 5-turn cap once-only, side-conversation filtering, intro suggested-reading coherence |

---

## Planned Work

### High priority (next sessions)

| Item | Notes |
|------|-------|
| **Delete personal CF Worker** | `c3po` on `vgr-702` — verification window passed 2026-06-07, overdue |
| **Delete personal Pinecone index** | After confirming humboldt updated to its own key path |
| **VPS migration** | Full plan at `plans/vps-migration.md`. 6 phases: provision Hetzner CX22, deploy keys, c3po systemd units (absorbing GHA Substack cron), humboldt migration, retire GHA, decommission personal infra |
| **Starter page** | Compile `data/intro_recs_tally.json` once ~20 welcome events accumulated (~18 as of session 31); curated "good first reads" page; update intro handler to use starter page + 1 long-tail pick |
| **Exhibit extraction** | `ingest/extract_structure.py`: section summaries + list/figure/table exhibits from PDFs (plan: `plans/structural-navigation.md`). ~450–550 new vectors, ~$1.30 |
| **`sync_sig_pages.py` in daemon** | Add as daemon step (alongside or after VPS migration); SIGPSY and DRG already in `SIG_CONFIG` |

### Medium priority

| Item | Notes |
|------|-------|
| **Twitter/X URL pass** | 194 deferred URLs in `discord_links_registry.json`; needs paid Twitter API v2 |
| **Anthropic key rotation** | Rotate to PI org Anthropic account |
| **Voyage humboldt key** | Create in PI Voyage account, wire into humboldt project |
| **Phase E — swarm planning** | Multi-node architecture for parallel ingest and multiple gateway channels |

### Longer-term (planned, not scoped)

| Item | Notes |
|------|-------|
| **Google Drive ingestion** | PI Drive: working papers, meeting notes, slide decks. Changes API delta feed; D1 for deletion tracking. Plan: `sources/drive/PLAN.md` |
| **Submissions portal** | URL submission, PDF upload, GitHub PR → review queue → Pinecone + protocolized-website card |
| **Content monitoring workers** | CF Workers scanning arXiv, curated RSS; Haiku relevance scoring before queue |
| **Slack integration** | Slash command `/c3po`; deferred response via CF Worker |
| **Case studies** | Private namespace; anonymized; separate Pinecone index for isolation |
| **Agents-first site** | Separate domain; multi-turn sessions, tool use, longer context. Design deferred |

---

## Open Questions

1. **Twitter/X pass** — paid API or skip indefinitely? 194 deferred URLs.
2. **Discord `discord` access level** — currently public (queryable by anyone hitting the web UI). Was originally planned as `member`-gated (SIWE). Still unresolved.
3. **Substack mirroring** — image hosting on R2 + post HTML in D1 → Hono route `/p/:slug` is planned in `protocolized-website` project (see `plans/phase2-substack-mirror.md`). C3PO's role: update Pinecone `substack` vector URLs from `protocolized.summerofprotocols.com/p/{slug}` to `protocolized.io/p/{slug}` once live.
4. **Meeting detection robustness** — SIG thread naming varies; active threads miss archival window. Long-term fix: fuzzy date-match fallback per channel or message-count heuristic.
5. **c3po.protocolized.io → protocolized.io/c3po** — subdomain preferred; no migration planned. Noted here for completeness.
