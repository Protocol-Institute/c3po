# Plan: vgr_zirp Reuse and Adaptation for C3PO

**Status:** Draft  
**Reference:** `/Users/Venkat/Dropbox/Code/Publishing/ribbonfarm_site/`  
**Based on:** Deep code + devlog audit, sessions 1–51

---

## Context and Corpus Differences

Before listing what to copy, the differences that shape what to adapt vs. skip:

| Dimension | vgr_zirp | C3PO |
|---|---|---|
| Corpus character | Long-form prose essays, Twitter threads, books | Academic papers with frameworks, protocol fiction, Substack essays |
| Author scope | Single author (Venkat Rao) | 100+ authors across disciplines |
| Source count | 71K vectors across 3 Pinecone indexes | 1,810 vectors, single index + namespaces |
| Chunking problem | Prose flows naturally into 512-token windows | Numbered frameworks span 8+ pages; two-column PDFs corrupt extraction |
| Lexicon origin | Coined terms from one writer's idiolect | Shared theoretical vocabulary across a field |
| Persona | One reconstructed voice | Institutional voice citing many authors |
| Private transcripts | KBA filter needed (Venkat's personal auth data at risk) | Not applicable — no single person's identity at risk |
| A/B testing | v2 vs. v3 soul/style, ongoing experiment | Not applicable — one persona, no era-switching |

---

## Part 1: Copy Directly (Little or No Adaptation)

### 1.1 Title-Anchored Embeddings

**What vgr_zirp does:** When embedding a body chunk, prepends `"Title: {section_title}\nSummary: {summary}\n\n"` to the text sent to Voyage (the `_embed` field). The stored `text` field remains clean (no prefix) for display purposes. This makes retrieval significantly more accurate for title-centric queries like "tell me about [paper name]."

**Status in C3PO:** Not implemented. Current ingest does prepend a prefix but embeds and stores the same string. The decoupling (`_embed` vs. `text`) is not present.

**Action:** In `ingest/ingest_pdfs.py` and `ingest/ingest_substack.py`, separate the string passed to Voyage from the string stored in metadata. The Voyage call gets the prefix + chunk; Pinecone metadata `text` field gets only the raw chunk. This affects all body chunks.

**Cost:** Re-embedding all current body chunks (~1,500 vectors × Voyage cost). At $0.06/1M tokens: ~$0.01. Worth doing before corpus grows.

---

### 1.2 Retrieval Tier Weighting

**What vgr_zirp does:** Each corpus type gets a score multiplier applied to the raw Pinecone cosine score before merge-and-sort. Eight tiers from 1.15 (books) down to 0.80 (individual tweets). This lifts book-sourced results over random blog hits for the same query.

**Adaptation for C3PO:**

| Tier | Source | Weight |
|---|---|---|
| 0 | Academic paper (SoP corpus PDF) | 1.15 |
| 1 | Research report / working paper | 1.10 |
| 2 | Essay / interview PDF | 1.05 |
| 3 | Substack: Fictions section (protocol fiction) | 1.00 |
| 4 | Substack: Articles / Obliquities | 1.00 |
| 5 | Collection card / author profile (metadata vectors) | 0.95 |
| 6 | Transcript (shared conversation) | 0.85 |

Rationale: academic papers are the Institute's primary research output and should win ties. Protocol fiction is high-quality and on-par with essays. Transcripts are useful for surfacing prior answers but should not dominate.

**Action:** Add weighted score computation in `mergeResults()` in `api/worker.js`. Requires `type` metadata field on each vector (already present in PDFs as `chunk.type`; Substack needs `section` field used as proxy).

---

### 1.3 D1 Database for Transcripts and Query Logging

**What vgr_zirp does:** Cloudflare D1 (SQLite) stores:
- `transcripts` — shared conversations with full Q/A, metadata, encryption, self-notes
- `query_log` — per-query logging (cost, tokens, session_id, turn, source, persona_version)
- `blocked_probes` — security probe log (type, excerpt, ip_hash)

**Why better than KV for C3PO:** Current KV-based transcript storage can't be queried — admin has to list + fetch all. D1 enables: "show me all transcripts about fiction," "what's the cost per day per session," "how many probe attempts this week." As the corpus and usage grow, this becomes essential.

**Adaptation:** Schema is nearly verbatim. Drop: `ab_choices` (no A/B experiment), `persona_version` field (one persona). Keep: `transcripts`, `query_log`, `blocked_probes`. Add C3PO-specific fields:
- `transcripts.primary_source_types` (TEXT, comma-separated: 'pdfs', 'substack', 'transcripts') — enables filtering by what the user was exploring
- `query_log.namespace_hits` (TEXT, JSON) — which Pinecone namespaces contributed to each query

**Migration path:** Keep KV transcript store operational while D1 is built. New submissions write to D1; existing KV entries remain readable but are not migrated.

---

### 1.4 AES-256-GCM Encryption for Private Transcripts

**What vgr_zirp does:** Private transcript fields (`turns_json`, `first_q`, `review`, `user_name`, `user_email`) are encrypted with AES-256-GCM before D1 insert. IV generated per field. Key derived via PBKDF2 from `PRIVATE_ENCRYPTION_KEY` env var.

**vgr_zirp incident:** Key generated, never saved → 6 private transcripts permanently unreadable. Lesson: generate key → print in terminal → copy to `.env.keys` → `wrangler secret put` → verify with test round-trip before any live submissions.

**Adaptation:** Copy `encryptField()` / `decryptField()` from `workers/oracle/index.js` verbatim. Apply to the same fields. Add `PRIVATE_ENCRYPTION_KEY` to `.env.template` and generate a fresh key per `../admin/security.md`.

---

### 1.5 Strike/Ban Tracking

**What vgr_zirp does:** 3 probe detections from the same IP (within 7-day window) → 24-hour IP ban. Stored in KV: `probe:strikes:{ipHash}` (TTL 7 days), `probe:banned:{ipHash}` (TTL 24h). Probes also logged to D1 `blocked_probes`.

**Status in C3PO:** Security regex filters exist but no strike tracking — same IP can probe indefinitely.

**Adaptation:** Copy the strike-tracking logic verbatim. Compute IP hash with `crypto.subtle.digest("SHA-256", ...)`. Log to D1 `blocked_probes` once D1 is live; until then, log to KV.

**Drop:** KBA probe (`VENKAT_TARGET_RE`) — not applicable to C3PO (no single person's biographical data at risk). Keep: injection, credential, sysextract.

---

### 1.6 Self-Notes at Share Time

**What vgr_zirp does:** When a conversation is submitted (POST /share), fires a Haiku call via `ctx.waitUntil` that writes a first-person reflection: what did I find, what did I miss, did I handle the register correctly, did the user have to prompt for citations. Result stored in `transcripts.self_notes` (never encrypted). Cost: ~$0.002/transcript.

**Why valuable for C3PO:** The corpus is academic and citation-dense. The failure mode (hallucinating corpus coverage, denying something exists, truncating a list) is specific and learnable. Self-notes build a feedback loop that doesn't require manual review of every transcript.

**Adaptation:** Adapt the prompt for C3PO's failure modes:
- Did I correctly identify which namespace (pdfs vs. substack) contained the answer?
- Did I deny something exists that might just have been retrieved poorly?
- Did I synthesize across multiple authors or anchor on one?
- Were citations specific (named paper + author) or vague ("the corpus suggests")?
- Was anything visibly truncated mid-definition or mid-list?

Private submissions: constrain to own-performance reflection only (vgr_zirp pattern).

---

### 1.7 Per-Query D1 Logging

**What vgr_zirp does:** Every answered query inserts a row to `query_log` with tokens, cost, session_id, turn number, source (web/mcp), and where in the site the query originated (`src_type`, `src_place`).

**Status in C3PO:** Per-query KV logging exists (`log:{ts}:{rand}`, 7-day TTL). Loses everything after 7 days; can't aggregate or query.

**Adaptation:** Replace KV-based `logQuery()` with D1 insert. Keep session_id (already implemented) and turn number (already implemented). Add `namespace_hits` JSON — which namespaces contributed top results — for corpus coverage analysis.

---

## Part 2: Adapt Significantly

### 2.1 System Prompt Structure — CORPUS_MAP

**What vgr_zirp does:** `CORPUS_MAP` block in system prompt tells Claude exactly what's indexed — blog post count, date range, which books, what's NOT indexed. Prevents hallucination about corpus coverage. Also includes ALIAS_TABLE (Breaking Smart Newsletter = Contraptions 2015–2019), series list, and guest author profiles.

**Adaptation for C3PO:**
- **Keep CORPUS_MAP concept:** Tell Claude exactly what's in Pinecone — 82 PDFs (SoP program 2023–2024, list major papers), 116+ Substack posts (Fictions / Articles / Obliquities sections), 13 collection cards, 38 author profiles, ~4 transcripts. Include date ranges.
- **Drop ALIAS_TABLE:** The PI corpus doesn't have the same rebranding history.
- **Adapt NOT INDEXED list:** Be explicit about what's missing — pre-2023 PI work, external papers by non-SoP authors, Discord, YouTube (planned but not yet indexed). The 10 dimensions failure happened partly because Claude didn't know whether the list existed in the corpus or was just obscured.
- **Drop guest author profiles:** 100+ authors makes this intractable as a static injection. Authors are already represented as `author_profile` vectors in Pinecone — retrieve when relevant rather than injecting all 38+ upfront.

**Action:** Write a `CORPUS_MAP` constant in `api/worker.js` that replaces the current approximate description in `SYSTEM_PROMPT`. Update when new namespaces are added.

---

### 2.2 Lexicon Injection

**What vgr_zirp does:** `LEXICON_MD` (~40 terms, ~4K tokens) injected verbatim into the system prompt via `${LEXICON_MD}` in `build-prompt.js`. Terms were identified by ML candidate extraction (`api_tagger.py`), then hand-curated with usage counts and definitions. Prompt caching makes the cost negligible on warm queries.

**Key insight:** The lexicon handles term-level questions ("what is premium mediocre?"). It does NOT handle structural enumeration ("list all 10 dimensions of X") — that's the structural navigation problem addressed in `plans/structural-navigation.md`.

**Adaptation for C3PO:**
- Scope: Protocol Institute theoretical vocabulary — Kafka protocol, dynamic non-event, Whitehead advance, protocol gap, legibility, protocol fiction, Summer of Protocols, sufficiency (the concept, not the list), protocol stack, etc.
- Identification method: Same ML extraction approach (`api_tagger.py` adapted for this corpus) — run against enriched_meta.json summaries + a sample of body chunks to find high-frequency technical terms.
- Format: Adopt vgr_zirp format (term + usage count + 2-sentence definition with paper citation).
- Size: Aim for ~40 terms initially; expect to grow to ~80 as corpus expands.
- Separate plan: tracked as its own work item (distinct from structural navigation).

---

### 2.3 MCP Worker

**What vgr_zirp does:** JSON-RPC 2.0 over HTTP POST, 3 tools (`ask_vgr_zirp`, `search_corpus`, `submit_mcp_session`), stateless session model with compression + prior_session parameter for resumption, HELLO/NUDGE/ALERT messages at exchange milestones.

**Adaptation for C3PO:**
- Tool names: `ask_corpus`, `search_corpus`, `submit_session` — institutional rather than persona-named.
- Drop HELLO message (too chatty for a research tool). Keep NUDGE (exchange 10) and ALERT (exchange 30) — researchers may run long sessions.
- Drop prior_session resumption for now — add once usage patterns are understood.
- Auth: Rate-limit by IP (same as vgr_zirp). No API key required for initial launch.
- `submit_session` stores to D1 (same as web submissions). Same encryption for private.
- The 3 ARCHITECTURE.md-defined tools (`search_protocols`, `get_resource`, `ask_corpus`, `list_resources`) are a superset of vgr_zirp's 3. Start with the vgr_zirp 3; add `list_resources` (filtered listing) as Phase 2B.2.

**Key difference:** No persona-specific HELLO. vgr_zirp's intro message sets up the "archival reconstruction" fiction. C3PO is institutional — just open with capability summary and rate-limit warning.

---

### 2.4 Search Worker

**What vgr_zirp does:** `/api/search` routes by `corpus=` param (ribbonfarm | twitter | books | bibliography | all), queries respective indexes with high TOP_K, applies tier weights, returns with tab UI.

**Adaptation for C3PO:**
- Route by `source=` (pdfs | substack | all) since there are only 2 real corpus types now.
- Extend when Discord, YouTube, Drive come online.
- No separate search worker needed yet — the `/search` endpoint in the main worker is sufficient until request volume justifies splitting.
- The GET `/search?q=` endpoint already exists in C3PO; refine it with tier weighting and source filtering.

---

## Part 3: Skip for Now (With Reasons)

### 3.1 Multiple Pinecone Indexes

vgr_zirp uses 3 separate indexes (ribbonfarm, vgr-twitter, vgr-books) because Twitter and books arrived at different times and have different retrieval characteristics. C3PO's single index with namespaces works fine at current scale (~2K vectors). Revisit when Discord or YouTube brings volume above ~50K vectors.

### 3.2 A/B Testing Infrastructure

`ab_choices` D1 table, `buildSystemPrompt(version)` switching, experiment_id logging. Not applicable — C3PO has one persona and no planned voice experiments.

### 3.3 Site-Embedded Hooks

vgr_zirp injects "Discuss with vgr_zirp →" boxes into every post page, series page, cluster page, author page. C3PO's equivalent is integration into the protocolized.io Resources page. This is a separate deliverable (Phase 4 in ROADMAP.md) tied to the protocolized-website project, not the worker.

### 3.4 Session Compression

vgr_zirp runs a Haiku compression pass at submission time, returning a summary the client can pass as `prior_session` to a new session. Useful for MCP users who want continuity across days. Add this when MCP is live and there's evidence of users returning for follow-on sessions.

### 3.5 KBA Probe

`KBA_RE` catches "mother's maiden name," "first pet," "where were you born" — these are authentication questions about Venkat personally. Not applicable to an institutional research assistant.

---

## Part 4: Architectural Divergences — Where C3PO Needs Different Approaches

### 4.1 Structural Retrieval (Biggest Divergence)

vgr_zirp's corpus is prose — 512-token windows naturally capture complete thoughts. C3PO's corpus has numbered frameworks spanning 8+ pages. This requires `section_summary` and `list_extract` chunk types that don't exist in vgr_zirp. Full plan in `plans/structural-navigation.md`.

### 4.2 Multi-Author Citation Density

vgr_zirp has one author and system prompt can inject full corpus context. C3PO has 100+ authors and 82+ papers; the system prompt can't carry full bibliographic context. Solution: CORPUS_MAP with paper titles and author names (to prevent false denials), plus precise retrieval (titles/authors in metadata for filtering). The `author_profile` and `collection_card` vector types address this from the retrieval side.

### 4.3 Fiction vs. Non-Fiction Handling

vgr_zirp doesn't have fiction in its corpus. C3PO's Fictions section (58 posts) requires the retrieval layer to understand that asking "what are the protocol mechanics in Timber's story X" is a valid literary question, not a factual claim. The tier weighting (fiction = 1.00, same as essays) and the `source_type` badge in the UI handle this for display. The system prompt should have a brief note: "Protocol fiction in the corpus makes arguments through narrative — analyze it as deliberate worldbuilding, not factual claims."

### 4.4 Corpus Growth Pattern

vgr_zirp's corpus is frozen (Venkat's ZIRP-era writing, 2007–2022). C3PO's corpus grows continuously — Substack syncs daily, PDFs are added per SoP publication cycle, and eventually Discord/YouTube/Drive are added. This changes the CORPUS_MAP (needs to be dynamic or at least regularly updated) and makes the `doc_summary` + `section_summary` approach more important (they're the primary mechanism for answering "what's in the corpus now").

---

## Implementation Priority Order

Given the above, the recommended build sequence (after structural navigation plan):

1. **Tier weighting** — small change to `mergeResults()`, immediate retrieval quality improvement. No new infrastructure.

2. **CORPUS_MAP in system prompt** — prevents false denials like "the corpus doesn't contain a canonical list." Reduces the worst failure mode.

3. **D1 setup** — `CREATE TABLE transcripts`, `query_log`, `blocked_probes`. Migrate new submissions to D1; keep KV for legacy.

4. **Encryption + D1 transcript storage** — private submissions need encryption before D1 goes live.

5. **Strike/ban tracking** — copy from vgr_zirp once D1 is in place (logs to `blocked_probes`).

6. **Self-notes** — add Haiku reflection call at share time. Requires D1 `self_notes` column.

7. **Per-query D1 logging** — replace KV `logQuery()` with D1 insert.

8. **Title-anchored embeddings** — re-embed body chunks with `_embed` prefix. Small cost, run once.

9. **Lexicon** — ML extraction pass + hand curation. Separate plan.

10. **MCP worker** — after D1 is stable (submissions need D1). Adapts vgr_zirp's `workers/mcp/index.js`.

11. **Structural navigation** — section_summary + list_extract chunk types. See `plans/structural-navigation.md`.

---

## Files to Read Before Each Phase

| Phase | Read first |
|---|---|
| Tier weighting | `workers/oracle/index.js` → `mergeResults()` and tier weight constants |
| D1 schema | `workers/oracle/index.js` → `initDb()` and D1 insert/query calls |
| Encryption | `workers/oracle/index.js` → `encryptField()`, `decryptField()`, `ENCRYPTION_KEY` derivation |
| Strike/ban | `workers/oracle/index.js` → `checkProbe()`, `recordStrike()`, `checkBan()` |
| Self-notes | `workers/oracle/index.js` → `runSelfNotes()` and the Haiku prompt |
| MCP | `workers/mcp/index.js` — full file, ~400 lines |
| Lexicon | `workers/oracle/persona.js` → `LEXICON_MD` structure; `api_tagger.py` → candidate extraction logic |
