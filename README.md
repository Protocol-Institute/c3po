# C3PO — Protocol Institute Research Assistant

A RAG (retrieval-augmented generation) agent trained on the Protocol Institute's research corpus, to be deployed at or near `protocolized.io`.

Named for C-3PO, the *Star Wars* protocol droid — explicitly described as "fluent in over six million forms of communication" and devoted to smooth inter-party protocol operation. (The briefing memo said Star Trek; it is in fact Star Wars. The name stands.)

---

## What This Is

C3PO is a research assistant that lets users query, synthesize, and explore the Protocol Institute's accumulated knowledge. It answers questions about protocol theory, surfaces connections across the research library, and cites its sources. It is not a chatbot and does not answer out-of-corpus questions.

The persona and voice are defined in `SOUL.md`.

---

## Corpus

The initial corpus comes from two sources in `protocol-institute/protocolized-website`:

| Source | Contents | Count |
|--------|----------|-------|
| `public/resources/*.pdf` | Summer of Protocols research archive — papers, working papers, games, datasets, presentations | ~82 PDFs, ~353 MB |
| Protocolized Substack export | Issues of Protocolized magazine, synced from RSS | ~200 articles |

Over time the corpus will grow to include:
- Discord messages from the Protocol Institute community server
- User-submitted URLs and PDFs (via submission mechanism — see Phase 3)
- Additional research as it is published

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  INGESTION PIPELINE                                          │
│                                                              │
│  PDFs (public/resources/)  ──┐                              │
│  Substack export           ──┼──► chunk ──► Voyage AI ──►  │
│  Discord messages (Phase 4)──┘    (512t/64t overlap)  embed │
│  Submitted URLs/PDFs (Phase 3)                               │
└─────────────────────────────────────────────┬────────────────┘
                                              │
                                        Pinecone index
                                        "c3po" (1024d cosine)
                                        metadata: title, authors,
                                        date, type, source, tags
                                              │
┌─────────────────────────────────────────────▼────────────────┐
│  QUERY API (Cloudflare Worker)                               │
│                                                              │
│  user query ──► Voyage embed ──► Pinecone retrieve (top-K)  │
│              ──► Claude (Sonnet) with SOUL.md persona        │
│              ──► response + cited sources                    │
└──────────────────────────────────────────────────────────────┘
                                              │
┌─────────────────────────────────────────────▼────────────────┐
│  UI                                                          │
│                                                              │
│  Lives in the Resources page of protocolized.io             │
│  (redesigned to accommodate the assistant alongside          │
│   the existing filterable resource library)                  │
└──────────────────────────────────────────────────────────────┘
```

---

## Tech Stack

| Component | Technology | Notes |
|-----------|-----------|-------|
| Embeddings | Voyage AI `voyage-3` (1024d) | Same model used in ribbonfarm/contraptions agents |
| Vector DB | Pinecone serverless | New index `c3po`; aws us-east-1 to match existing indexes |
| LLM | Claude Sonnet (latest) | Via Anthropic API; prompt caching enabled |
| PDF parsing | `pdfplumber` | Better table/layout handling than pdftotext |
| Chunking | 512 tokens / 64 overlap | Standard recipe from ribbonfarm; may tune |
| Worker | Cloudflare Workers | TypeScript; embed → retrieve → generate |
| Site | Cloudflare Pages (eventual) | protocolized.io migration target |
| Submission API | Cloudflare Workers + R2 | Phase 3; uploaded PDFs stored in R2 |
| Discord bot | `discord.py` | Phase 4; captures messages for indexing |

---

## Phased Roadmap

### Phase 1 — Core Corpus + Index (current)

**Goal:** Pinecone index populated and queryable via Python script. No UI yet.

- [ ] Copy PDF corpus from `protocolized-website/public/resources/` → `data/pdfs/` (gitignored)
- [ ] `ingest/ingest_pdfs.py` — parse PDFs, chunk, embed via Voyage, upsert to Pinecone with metadata
- [ ] Pull Substack export and run `ingest/ingest_substack.py`
- [ ] Create Pinecone index `c3po` (serverless, aws us-east-1, 1024d cosine)
- [ ] Verify retrieval quality with test queries
- [ ] Register `PINECONE_C3PO_HOST` in `Code/.env.keys`

**Metadata schema per chunk:**
```json
{
  "id": "sha256-of-chunk-text",
  "source": "pdf | substack | discord | submission",
  "title": "...",
  "authors": ["..."],
  "date": "YYYY-MM-DD",
  "type": "paper | article | fiction | ...",
  "tags": ["..."],
  "url": "https://... or /resources/filename.pdf",
  "chunk_index": 0,
  "chunk_total": 12
}
```

### Phase 2 — Cloudflare Worker + Basic UI

**Goal:** Live query endpoint; minimal chat UI on a subdomain of protocolized.io.

- [ ] `api/worker.js` — CF Worker: embed query → Pinecone → Claude with SOUL.md persona
- [ ] Deploy to personal CF account at `c3po.protocolized.io` (or `api.protocolized.io/c3po`)
- [ ] Minimal UI: text input, response display, source citations
- [ ] Rate limiting (20 queries/IP/hour)
- [ ] Register CF Worker secrets in `.env` + `Code/.env.keys`

**System prompt structure:**
```
[SOUL.md excerpt — identity, voice, corpus scope]
[CORPUS_MAP — what sources are indexed, date range]
[RETRIEVED_CHUNKS — top-K results from Pinecone]
[USER_QUERY]
```

### Phase 3 — Submission Mechanism

**Goal:** Allow external contributors to add resources to the corpus.

Three submission paths:

**3a. URL submission**
- User pastes a URL
- Worker fetches → extracts text → chunks → embeds → queues for review
- Reviewer approves/rejects via admin interface
- On approval: add to Pinecone; optionally add to `protocolized-website` as a resource markdown

**3b. PDF upload**
- User uploads a PDF (max 50MB)
- Stored in Cloudflare R2 (`c3po-submissions` bucket)
- Parsed → chunked → embedded → queued for review
- Same review/approval flow as URL submission

**3c. GitHub PR (for contributors with repo access)**
- Add a markdown file to `protocolized-website/src/content/resources/`
- If `file:` field points to a new PDF in `public/resources/`, include the PDF
- CI action on merge: run `ingest/ingest_single.py <slug>` to embed the new resource
- This path is most efficient for Protocol Institute team members

**Review queue:** Simple admin interface (auth via `ADMIN_KEY`). Shows pending submissions with extracted text preview. Approve → index; reject → discard with optional feedback to submitter.

### Phase 4 — Discord Ingestion

**Goal:** Protocol Institute Discord messages searchable through C3PO.

- [ ] `ingest/ingest_discord.py` — Discord bot using `discord.py`
- [ ] Capture messages from designated channels (not DMs; not all channels)
- [ ] Chunk by thread; metadata includes channel, author handle, timestamp
- [ ] Embed and index incrementally (daily batch or on-message webhook)
- [ ] Discord messages are lower-weight than peer-reviewed papers in retrieval (use metadata filter or score adjustment)
- [ ] Privacy consideration: only index public/designated channels; no DM capture

### Phase 5 — protocolized.io Integration

**Goal:** C3PO lives natively in the protocolized.io Resources page.

**Prerequisite:** Migrate `protocolized-website` from GitHub Pages to Cloudflare Pages.

Migration path:
1. Add `wrangler.toml` to `protocolized-website`
2. Build command stays `npm run build`; publish dir is `dist/`
3. Set up Cloudflare Pages project pointing to `Protocol-Institute/protocolized-website`
4. Update DNS: `protocolized.io` → Cloudflare Pages (currently pointing to GitHub Pages IPs)
5. Once on CF Pages, bind the C3PO Worker as a service binding

**Resources page redesign:**
- Current: filterable grid of resource cards
- New: split-pane layout — left/top is C3PO chat interface; below is the existing filterable library
- C3PO results can link directly to resource detail pages in the library
- "Ask about this resource" button on each resource detail page pre-seeds C3PO with the resource title

### Phase 6 — Migration to Protocol-Institute Org

**Goal:** Hand off to the org.

- [ ] Transfer repo from `vgururao/c3po` to `Protocol-Institute/c3po`
- [ ] Transfer CF Worker to Protocol Institute's CF account
- [ ] Update Pinecone index ownership (or re-create under org key)
- [ ] Update DNS and Worker routes

---

## Submission Mechanism Design (Detail)

For Phase 3, the submission form will accept:

```
Name (optional)
Email (optional, for follow-up)
Submission type: [URL] [PDF upload] [GitHub PR]
Title
Authors
Date published
Type: [paper] [article] [essay] [fiction] [other]
Tags (comma-separated)
Description (280 chars)
URL or file upload
License / permission note
```

URL submissions are scraped server-side (not client-side) to avoid CORS issues and to normalize content. PDFs are stored in R2; text is extracted server-side.

All submissions go into a review queue before indexing. The queue is visible to org admins only. Approved submissions are indexed in Pinecone and optionally added to the `protocolized-website` resource library as a markdown file (making them visible in the filterable library as well as searchable via C3PO).

---

## Development Setup

```bash
# Python environment
/opt/homebrew/bin/python3 -m venv .venv
source .venv/bin/activate
pip install pdfplumber voyageai pinecone-client anthropic python-dotenv

# Copy and fill env
cp .env.template .env
# → fill from Code/.env.keys (VOYAGE_API_KEY, PINECONE_API_KEY already exist)
# → create Pinecone index c3po and add PINECONE_C3PO_HOST

# Copy PDF corpus (from protocolized-website — do not commit)
cp ../protocolized-website/public/resources/*.pdf data/pdfs/

# Run ingestion (Phase 1)
python3 ingest/ingest_pdfs.py
python3 ingest/ingest_substack.py --export-dir $SUBSTACK_EXPORT_DIR
```

---

## Key Decisions and Rationale

**Why personal account first?**
The infrastructure (Pinecone index, CF Worker, API keys) is easier to iterate on under a personal account. The org account will own the production deployment. Migration is straightforward: transfer repo, re-key secrets, update DNS.

**Why Voyage AI voyage-3?**
Already in use for ribbonfarm and contraptions agents. The key exists. voyage-3 (1024d) performs well on academic and essay text. No reason to introduce a second embedding provider.

**Why Cloudflare Workers?**
Consistent with the planned protocolized.io migration to Cloudflare Pages. Workers integrate tightly with Pages via service bindings — no separate deployment needed once the site moves. The personal account already has Workers configured from ribbonfarm.

**Why pdfplumber over pdftotext?**
pdfplumber handles multi-column layouts and tables better, which matters for academic PDFs. pdftotext is fine for prose but loses structure in formatted documents. The Summer of Protocols papers vary in formatting quality.

**Why 512/64 chunking?**
Same recipe used for ribbonfarm (6,489 chunks from 1,132 posts). The 64-token overlap prevents context loss at chunk boundaries. May experiment with larger chunks for academic papers where longer context improves retrieval.

**Why a review queue for submissions?**
The corpus quality is the product quality. Accepting unreviewed submissions directly into the index would degrade retrieval quality. The review step keeps the corpus curated while still enabling community contribution.

---

## Repo Structure

```
c3po/
├── README.md              # This file
├── CLAUDE.md              # Dev environment and conventions
├── SOUL.md                # Bot persona, voice, intellectual commitments
├── status.md              # Activity log
├── .env.template          # Required env vars (no values)
├── .gitignore
├── ingest/
│   ├── utils.py           # Chunking, cleaning, Voyage/Pinecone helpers
│   ├── ingest_pdfs.py     # PDF corpus → Pinecone
│   ├── ingest_substack.py # Substack export → Pinecone
│   └── ingest_discord.py  # Discord messages → Pinecone (Phase 4)
├── api/
│   ├── worker.js          # Cloudflare Worker (Phase 2)
│   └── wrangler.toml      # CF Worker config (Phase 2)
├── submissions/
│   └── worker.js          # Submission intake Worker (Phase 3)
└── data/                  # Local data — gitignored
    ├── pdfs/              # PDF corpus copy
    └── exports/           # Substack exports
```
