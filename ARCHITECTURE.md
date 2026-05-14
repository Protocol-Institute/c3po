# C3PO — Architecture

C3PO is the Protocol Institute's knowledge infrastructure — a multi-source corpus, a query engine, and a set of delivery interfaces. It is not a chatbot; it is a research assistant that answers questions strictly within the PI corpus and cites its sources.

This document describes the system design. For the phased implementation plan see `ROADMAP.md`. For account migration see `MIGRATION.md`.

---

## System Overview

```
┌─────────────────────────────────────────────────────────────────┐
│  CORPUS SOURCES                                                  │
│                                                                  │
│  PDFs ─────────────────────────────────────────────────────┐   │
│  Substack (continuous) ────────────────────────────────────┤   │
│  YouTube channel ──────────────────────────────────────────┤   │
│  Discord (designated channels) ────────────────────────────┤──►│ chunk
│  Submissions portal (URLs + PDFs) ─────────────────────────┤   │ embed
│  Content monitoring workers ───────────────────────────────┤   │ index
│  Case studies (private, access-controlled) ────────────────┘   │
└──────────────────────────────────────────────┬──────────────────┘
                                               │
                                         Pinecone index
                                         "c3po" (1024d cosine)
                                         Voyage AI voyage-3
                                         namespaced by source
                                               │
┌──────────────────────────────────────────────▼──────────────────┐
│  C3PO WORKER (Cloudflare Worker)                                 │
│                                                                  │
│  query → Voyage embed → Pinecone retrieve → Claude Sonnet       │
│  SOUL.md persona · CORPUS_MAP · cited sources · access check    │
│                                                                  │
│  Routes: /query  /search  /mcp  /health                         │
└───────────────────┬──────────────────┬──────────────────────────┘
                    │                  │
        ┌───────────▼──┐     ┌─────────▼──────────────────────────┐
        │  MCP Server   │     │  DELIVERY INTERFACES               │
        │  /mcp (SSE)   │     │                                    │
        │               │     │  Web bot — protocolized.io         │
        │  Tools:        │     │    (service binding, no CORS)      │
        │  search       │     │                                    │
        │  get_resource │     │  Discord bot — query + admin       │
        │  ask_corpus   │     │                                    │
        │  list         │     │  Slack app — /c3po slash command   │
        └───────────────┘     │                                    │
                              │  Agents-first site (future)        │
                              └────────────────────────────────────┘
```

---

## Source Registry Pattern

*Adapted from the mixture-of-vgrs sovereignty model.* Each corpus source is sovereign over its own ingestion pipeline and metadata. C3PO is sovereign over nothing except the query layer.

Each source maintains a registry file at `sources/<type>/registry.json`:

```json
{
  "source": "substack",
  "display_name": "Protocolized Substack",
  "pinecone_namespace": "substack",
  "vector_count": 2840,
  "last_ingested": "2026-05-14T00:00:00Z",
  "schema_version": 1,
  "access_level": "public",
  "freshness_cadence": "realtime",
  "ingest_script": "sources/substack/ingest.py",
  "notes": "Continuously mirrored from Substack RSS via CF cron trigger"
}
```

The C3PO Worker reads all `registry.json` files at startup and builds the `CORPUS_MAP` injected into the system prompt. This tells the LLM exactly what's in the index — preventing hallucination about corpus coverage.

Adding a new source: create `sources/<type>/`, write `registry.json`, write `ingest.py` or `worker.js`, register in `sources/REGISTRY.md`.

---

## Pinecone Index Structure

**Index:** `c3po` — 1024 dimensions, cosine metric, serverless (aws us-east-1)

**Namespaces** (one per source, allows independent ingestion and per-source stats):

| Namespace | Source | Access |
|---|---|---|
| `pdfs` | Summer of Protocols PDF archive | public |
| `substack` | Protocolized Substack | public |
| `youtube` | PI YouTube channel | public |
| `discord` | PI Discord (designated channels) | member |
| `submissions` | Community-submitted resources | public (post-review) |
| `crawl` | Content monitoring worker output | public (post-review) |
| `casestudies` | Consulting case study data | private |

**Metadata schema per chunk:**

```json
{
  "id": "sha256-of-chunk-text",
  "source": "pdfs | substack | youtube | discord | submission | crawl | casestudy",
  "namespace": "pdfs",
  "title": "...",
  "authors": ["..."],
  "date": "YYYY-MM-DD",
  "type": "paper | article | video | message | submission | crawl | casestudy",
  "tags": ["protocol", "..."],
  "url": "https://... or ipfs://<cid>",
  "access_level": "public | member | private",
  "chunk_index": 0,
  "chunk_total": 12,
  "word_count": 400
}
```

**Access control at query time:** the C3PO Worker filters Pinecone results by `access_level` based on the caller's session. Unauthenticated: `public` only. Authenticated member (SIWE): `public` + `member`. Admin: all namespaces including `casestudies`.

---

## Corpus Sources — Design Notes

### PDFs
~82 PDFs, ~353 MB from Summer of Protocols. Ingested once; re-ingested if PDF set changes. Source of truth will move to IPFS (see protocolized-website ROADMAP.md Phase 3); ingest script reads from IPFS CID or local copy.

### Substack
Protocolized newsletter. Continuous ingestion via CF Worker Cron Trigger (every 30 min). Source of truth: Substack RSS feed. The same RSS sync also drives the live magazine on protocolized.io — these are two consumers of the same source, not duplicated infrastructure.

### YouTube
PI YouTube channel transcripts + metadata. YouTube Data API v3 for video metadata; `youtube-transcript-api` for captions (Whisper fallback for missing/poor captions). For talks with visible slides: ffmpeg scene detection extracts keyframes, Vision LLM (Claude) extracts slide text and describes diagrams. Speaker diarization via pyannote.audio for multi-speaker salon conversations. Chunked by slide change or speaker turn with timestamps; chapter markers used as primary boundaries when present.

**Full integration plan, transcript pipeline, slide extraction, and chunking strategy:** see [`sources/youtube/PLAN.md`](sources/youtube/PLAN.md).

### Discord
Discord bot with maximum channel read permissions. Ingests designated public/research channels only — no DMs, no general chat unless explicitly included by channel owners. Chunked by thread. Incremental daily batch. Content weighted lower than peer-reviewed papers in retrieval (metadata field `weight: 0.7` used in scoring). Privacy: only channels the bot is explicitly given access to; channel owners can opt out.

The same Discord bot handles query interface (see Delivery Interfaces).

**Full integration plan, API constraints, CDN expiry handling, and bot setup:** see [`sources/discord/PLAN.md`](sources/discord/PLAN.md).

### Submissions portal
Three paths: URL submission (server-side scrape), PDF upload (stored in R2, parsed server-side), GitHub PR (for team members with repo access). All submissions go to a review queue; no automatic indexing. Approved → Pinecone + optional protocolized-website resource markdown. The review interface is the C3PO admin panel, auth-gated.

### Content monitoring workers
CF Workers with Cron Triggers that monitor specific sources for PI-relevant content. Initial targets: arXiv (protocol-related papers), specific org sites, curated RSS feeds. Each crawl result is AI-scored for relevance before entering the review queue. Same approval flow as submissions.

### Case studies
Consulting work data. Private namespace. Anonymized before indexing (client names + identifying details replaced with tokens). Access restricted to explicit admin auth — not accessible to public or member-level queries. May use a separate Pinecone index for stronger isolation. Indexing is manual (no cron) and requires explicit admin approval for each document.

---

## Delivery Interfaces

### Web bot — protocolized.io
Primary human-accessible face. Integrated into the Resources page via CF Pages service binding — queries go from the page to C3PO Worker with no cross-origin hop. Chat panel alongside the filterable resource library. Rate limited by KV: unauthenticated users get a daily query budget; authenticated members get more.

### MCP Server
Route `/mcp` on the C3PO Worker. Implements Model Context Protocol (JSON-RPC 2.0 + SSE transport). CF Workers' native streaming support makes this clean. Makes C3PO callable from Claude, Cursor, and other MCP-aware clients.

Tools:
- `search_protocols(query, n?)` — semantic search, returns top-N with metadata
- `get_resource(slug)` — full metadata + abstract for one resource
- `ask_corpus(question)` — full RAG: retrieve + synthesize with citations
- `list_resources(type?, tag?, source?)` — filtered listing

Auth: API key per client, rate-limited via KV. Discovery via `/.well-known/mcp.json` on `protocolized.io`.

### Discord bot
Dual-purpose: corpus ingestion (reads channels) + query interface (responds to commands).

Query commands:
- `/c3po <question>` — RAG query, responds in channel with answer + cited sources
- `/c3po search <terms>` — semantic search, returns list of matching resources
- `/c3po submit <url>` — submit a URL to the review queue

Admin commands (restricted to admin role):
- `/c3po approve <submission_id>` — approve a queued submission
- `/c3po status` — corpus stats from source registries

### Slack integration
Slack app with slash command `/c3po`. CF Worker handles Slack Events API callbacks (3-second timeout handled via deferred response pattern — Worker posts result to `response_url` asynchronously). Same query/search tools as Discord. No ingestion from Slack.

### Agents-first website (future)
Separate domain (`protocolols.ai` or similar — TBD). Heavy C3PO integration as the primary interface. Multi-turn sessions, tool use, longer context. Targeted at researchers and practitioners who want a deeper interface than the web widget on protocolized.io. Design deferred until Phases 1–5 are stable.

---

## CF Deployment — Account-Neutral Setup

Same guardrails as protocol-institute.org and protocolized.io. The Worker is designed to be deployable to any CF account without code changes.

- **No `account_id` in `wrangler.toml`** — set `CLOUDFLARE_ACCOUNT_ID` env var or in `~/.wrangler/config.toml`
- **No hardcoded resource IDs** — D1 database IDs are account-specific; document migration in `MIGRATION.md`
- **All secrets via `wrangler secret put`** — never committed; listed in `.env.template` and `Code/.env.keys`

See `MIGRATION.md` for the step-by-step account migration procedure.

---

## SOUL.md

C3PO's persona and voice are defined in `SOUL.md`. Key properties:
- In-corpus only: refuses to answer questions outside the PI research corpus
- Cites sources: every factual claim is attributed to a specific resource
- Named for C-3PO (Star Wars): "fluent in over six million forms of communication," devoted to smooth protocol operation
- Tone: precise, institutional, intellectually serious — matches the Protocolized editorial voice

---

## Relationship to mixture-of-vgrs

C3PO adapts the MoV source-sovereignty and capability-registry patterns to the PI context. Key differences:

| | mixture-of-vgrs | C3PO |
|---|---|---|
| Sub-bots | Era-bots (zirp, gramsci) | Source types (pdfs, substack, etc.) |
| Routing | Era classifier → dispatch | Access-level filter → single index query |
| Synthesis | Multi-era voice blending | Single C3PO persona across all sources |
| Registry | Bot capabilities | Source freshness + coverage |
| Delivery | Single oracle UI | Web, MCP, Discord, Slack |

C3PO has one corpus and one persona; MoV has multiple personas over the same subject. The shared pattern is: sovereign ingestion pipelines, a registry layer for coverage tracking, and a query layer that operates above the sources.
