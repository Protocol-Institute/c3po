# C3PO — Build Log

A build log for C3PO, the Protocol Institute's research assistant — how the corpus was assembled, what decisions were made, and where things stand. Written for readers curious about the process and for future maintainers.

---

## Sessions 1–2: Corpus Archaeology and Substack Ingestion

*2026-05-14*

**Tracks:** corpus-ingestion, vector-architecture

**Session costs:** haiku_enrichment: $0.05 · voyage_embedding: $0.04

**Vectors upserted:** substack: 1,040

- **Starting point:** Protocolized Substack HTML export (unzipped) — 131 HTML files in `posts/`, a `posts.csv` with metadata, and per-post `.delivers.csv` and `.opens.csv` email delivery files. The export is the fastest way to get all body HTML at once; but it is missing tags, authors, sections, and edit timestamps — all of which live only in the Substack API.

- **Export archaeology:** Parsed `posts.csv` to determine post counts and types: 113 newsletters, 3 content pages (the collection announcement pages for Ghosts in Machines, Building and Burning Bridges, and Submission Guidelines), 2 podcasts (skipped), and a handful of drafts. Draft detection: no `.delivers.csv` *and* `is_published = false`. Pages required an allowlist because some collection announcement pages have real content worth embedding even though they aren't newsletter posts.

- **API discovery:** The Substack REST API at `/api/v1/posts` is public and unauthenticated for any public publication — it is the same endpoint Substack's own web interface uses to render publication pages. It returns fields entirely absent from the export: `postTags` (series/collection membership), `publishedBylines` (author attribution with handles), `section_id` (which subscription section a post belongs to), `updated_at` (last edit timestamp), `reaction_count`, `restacks`, and `wordcount`. Fetched all 115 posts in ~10 paginated calls. Saved to `sources/substack/api_metadata.json`.

- **Section discovery:** Protocolized uses Substack's multi-section feature — readers can subscribe to sections independently. The sections were not visible in the API metadata list directly, but `section_id` integers appeared on each post. Mapped IDs to names by extracting the preloaded JSON state from the publication homepage HTML: **Fictions** (ID 333105, 58 posts — fiction contests and series), **Articles** (ID 333110, 47 posts — essays, SIG reports, case studies), **Obliquities** (ID 333103, 5 posts — VGR's editorial column), and the catch-all **Protocolized** default (5 posts, no section_id). Section stored as `section_name` metadata on every Pinecone vector.

- **Tag archaeology:** Protocolized uses two layers of Substack tags. The first layer is publication-level category tags chosen by the editors (*Stories*, *Technology*, *Fiction*, *Culture*, *Philosophy*, etc.) — applied per-post and used for reader discovery. The second layer is publication-specific collection tags that carry series or SIG membership: *Terminological Twists*, *Ghosts in Machines*, *The Librarians*, *Building and Burning Bridges*, *Zoothesia*, *Bridge Atlas*, *SIGFPT*, *SIGBIZ*, *SIGMEM*, *SIGFIC*, *Obliquities*, *Protocols for the Long Now*.

- **Collections map:** Built `sources/substack/collections.json` — the authoritative membership roster for all 13 named collections. Structure: fiction contests (4), series (3 including one emerging), proto-collections (1: Sachin Benny's UE-T1 train stories, which have no Substack tag yet), SIGs (4), and editorial column (1). Proto-collections are manually curated author series whose posts are thematically linked but haven't received an official tag yet.

- **Author attribution:** Author data is not in the export CSV or in post HTML bodies — only in the API `publishedBylines` field. Some bylines use display handles rather than real names: *Thing Party* = Elizabeth Maher (T.R.O.(L.L.) universe author), *Sachin* = Sachin Benny, *Spencer Nitkey - Writer* = Spencer Nitkey. 38 unique non-Protocolized contributors identified; 6 are regular contributors (3+ posts). Timber Stinson-Schroff is co-editor, not a guest contributor — his editorial posts are authoritative publication voice. Handle-to-name resolution table stored in `ingest/enrich_substack.py` and `sources/CORPUS_MAP.md`.

- **Haiku enrichment (~$0.05):** One Claude Haiku call per post. Input: title + subtitle + bylines + first 1,200 characters of body text. Output: a two-sentence concrete summary, 2–4 categories from a fixed 11-term vocabulary (*protocol-fiction*, *protocol-theory*, *protocol-watching*, *editorial*, *research-report*, *technology-ai*, *governance*, *announcement*, *interview*, *memory-archival*, *organizations*), and a resolved primary author name. Ran on 129 posts (all non-podcast), 0 errors. Checkpoint saves every 10 posts so a mid-run failure loses at most 10 calls. Idempotent: skips slugs already in `enriched_meta.json`.

- **Embedding strategy — four vector types:** The corpus supports four distinct retrieval patterns, each needing a different vector shape. **Body chunks**: each post is chunked at ~512 tokens (64-token overlap); every chunk gets a prefix of *Title / Author / Collection (if any) / Summary* prepended before embedding, so a chunk about a specific narrative moment is anchored to its full semantic context. Vectors use the raw chunk for the stored `text` metadata field (clean, readable) but the prefixed version for embedding (richer signal). **Post summaries**: one vector per post at ID `{slug}__post_summary`, embedding the full title + author + date + section + categories + Haiku summary — catches queries that match the gist of a piece without matching any specific passage. **Collection cards**: one vector per collection/series/SIG at ID `collection__{slug}`, describing the collection and listing all its posts — catches queries like *what are the common themes in Ghosts in Machines*. **Author profiles**: one per contributor at ID `author__{slug}`, listing all their posts with collection membership — catches author-centric queries. Regular contributors (3+ posts) are labeled as such; one-off contributors are labeled as guest contributors.

- **Ingest results:** 1,040 vectors in Pinecone namespace `substack` — approximately 873 body chunks, 116 post summaries, 13 collection cards, 38 author profiles. Voyage AI `voyage-3` model (1024 dimensions, cosine metric). Embedding cost approximately $0.04 at $0.06/M tokens.

- **Sync strategy:** The two-track RSS + export approach (used for the Contraptions project) was rejected in favor of an API-first daily sync. RSS misses all post edits. The export requires a manual download. The API provides `updated_at` per post, enabling edit detection without HTML diffing: compare each slug's current `updated_at` against a stored value in `registry.json`. New posts get full enrichment + all 4 vector types. Edited posts get body chunks + post summary re-embedded (re-enriched via Haiku only if wordcount delta exceeds 15%). Tag changes trigger re-embedding of affected collection cards. `sync_substack.py` implements this; the initial `last_sync` state was populated from the current API metadata so the first run correctly detects zero changes.

- **Corpus map:** Created `sources/CORPUS_MAP.md` to record structural information that is not derivable from the code or API. Key entries: the four Substack sections and their IDs, Timber's co-editor status, the author handle resolution table, and the extended universe project — Spencer Nitkey (Zoothesia), Sachin Benny (UE-T1 train series), Elizabeth Maher (T.R.O.(L.L.) universe), and Randy Lubin (Caduceus City) are building a shared off-Substack fiction universe, currently seeded by their Protocolized posts. Expected to become a new C3PO data source in Q4 2026.

- **Contraptions note:** The Substack API capabilities discovered here were written up as a note at `Publishing/Contraptions/substack-api.md` for the Contraptions/vgr_gramsci project. The note covers what the API provides that the export doesn't, and recommends the same API-first sync strategy for that project, including using `reaction_count` to replace the fragile pingback scraping currently used for lighthouse post scoring.

---

## Session 3: Devlog System, Session Rituals, and Org Admin Infrastructure

*2026-05-14 · 14:30–18:56 PT*

**Tracks:** operations

**Vectors upserted:** substack: 1,040

- **Devlog system:** Adopted the same devlog pattern used in the ribbonfarm_site project: `data/devlog.json` as source of truth, `devlog_session.py` to record session start/end timestamps to `/tmp/c3po_devlog_session_start.txt`, and `devlog_render.py` to render `DEVLOG.md` from JSON. The JSON-first approach keeps the record machine-readable and allows non-Markdown downstream uses (dashboard integration, cost aggregation).

- **Startup ritual (6 steps, added to CLAUDE.md):** (1) Record session start via `devlog_session.py start`. (2) Run `track.py status` to detect any concurrent PI project sessions and flag overlap. (3) Read `status.md` to review open questions and last session's state. (4) Check live Pinecone vector counts against the stored values in `status.md` — a fast sanity check that the index is intact. (5) Run `sync_substack.py --dry-run` to check for new or edited posts since last session. (6) Summarize delta to Venkat: vector counts, pending sync, open questions.

- **Wrap-up ritual (9 steps, added to CLAUDE.md):** Documentation (devlog entry, status.md, CLAUDE.md vector count update) → keys/env (update .env.template and ../admin/keys.md if new env vars were added) → repo (git add, commit, push) → expenses (track.py end, fill log-vgr.json, render.py) → memory (update Claude memory with non-obvious decisions). The checklist enforces that documentation and expense logging are never skipped at session end.

- **PI admin repo (Protocol-Institute/admin, private):** Created a new private GitHub repo to hold all Protocol Institute operational infrastructure: `expenses/` (expense tracker scripts, per-contributor log files, rendered EXPENSES.md), `keys.md` (key registry — owner, billing method, which projects use each key), and `security.md` (key storage policy, rotation procedures, incident response). This separates PI org admin from VGR's personal `Code/` directory, which tracks personal-scope keys only.

- **Expense model:** Per-contributor log files (`log-{id}.json`) — each contributor owns their file, eliminating merge conflicts. `track.py end` reads all active `/tmp/` session start files, takes the earliest start as the window start, and computes a single billable window — so if C3PO, website, and protocolized-website sessions all run simultaneously, the hours are not double-counted. A `key_ownership` map in each log file flags which API keys are personal (reimbursable) vs org (direct billing, not expensed). `render.py` aggregates all contributor logs into `EXPENSES.md` and `expenses.csv` with per-contributor sections and a grand total.

---

## Session 4: PDF Corpus Ingest and Canonical Ingest Pipeline

*2026-05-15 · 17:30–18:38 PT*

**Tracks:** corpus-ingestion, vector-architecture

**Session costs:** haiku_enrichment: $0.06 · voyage_embedding: $0.02

**Vectors upserted:** substack: 1,040 · pdfs: 766

- **Canonical ingest pipeline documented in ARCHITECTURE.md:** Formalized the three-layer pattern that every corpus source should follow. Layer 1 (Haiku enrichment): one API call per document produces summary, categories (from a shared 11-term vocabulary), and primary_author — saved to `sources/&lt;type&gt;/enriched_meta.json`, idempotent. Layer 2 (body chunks): text chunked at 512 tokens / 64 overlap, with a `Title / Authors / Type / Summary` prefix prepended to each chunk before embedding but not stored in metadata — so every chunk vector carries full document context. Layer 3 (doc_summary): one vector per document at ID `{id}__doc_summary`, embedding a compact structured representation for 'what is this document about' queries. The pattern was derived by comparing the Substack ingest (which had all three layers) against the original PDF ingest skeleton (which had none). Now the spec for all future sources.

- **PDF enrichment (82 PDFs, 0 errors, ~$0.06):** New script `ingest/enrich_pdfs.py`, structurally parallel to `enrich_substack.py`. Input per PDF: title + authors + type + tags from the protocolized-website resource markdowns (74 of 82 PDFs had matching entries), plus the first 1,500 characters of body text extracted by pdfplumber. Output: two-sentence summary, 2–4 categories from the shared vocabulary, and primary_author. 8 PDFs with no markdown entry (cover letters, title pages) were enriched from text alone — Haiku inferred institution-level authorship correctly. Checkpoint saves every 10 documents. All 82 enriched; saved to `sources/pdfs/enriched_meta.json`.

- **PDF ingest (766 vectors in Pinecone `pdfs` namespace):** `ingest/ingest_pdfs.py` rewritten from the original skeleton to match the canonical pattern: prefix-before-embedding, `namespace='pdfs'` throughout, `chunk_type` and `namespace` in every vector's metadata, and a new `ingest_doc_summaries()` function. Results: 689 body chunk vectors from 77 PDFs (5 PDFs were image-only — pdfplumber extracted no text, but all 5 still received doc_summary vectors from enrichment); 82 doc_summary vectors. 771 upserted, 766 landed in Pinecone — 5 deduplicated via SHA256 chunk-id collision (identical text passages appearing in multiple PDFs). Total index: 1,806 vectors across two namespaces.

- **Image-only PDFs (5, no body chunks):** `65-SCHROFF_GONG-Self-Ensured-cards.pdf`, `67-FERNANDEZ-Swarm-Protocol-Workshop.pdf`, `68-FERNANDEZ-Swarm-Games-pxlm.pdf`, `98-GONG-card-set-2024-03-28.pdf`, `SCHROFF-Protocol-Watching-HANDOUT.pdf`. These are card games and workshop materials typeset as images. They receive doc_summary vectors (from Haiku's reading of the opening text, which extracted enough from surrounding layout text to produce valid summaries) but no body chunks. A future improvement would be to run OCR (Tesseract or Claude Vision) on these five. Not blocking for Phase 2.

- **venv recreation note:** The `.venv` directory was absent at session start — Dropbox does not sync venvs (correct behavior, consistent with the node_modules policy). Recreated with `/opt/homebrew/bin/python3 -m venv .venv` and reinstalled deps. Also discovered that `pinecone-client` has been renamed to `pinecone` by Pinecone — the old package name raises an exception on import. Updated the install command in CLAUDE.md.

---

## Session 5: Phase 2A — Oracle Worker and Web UI

*2026-05-15 · ~19:00–19:12 PT*

**Tracks:** worker, ui, phase-2

**Vectors upserted:** substack: 1,040 · pdfs: 766

- **api/worker.js — Phase 2A Oracle Worker (~1,100 lines):** Adapted from vgr_zirp oracle/index.js for C3PO. Single Pinecone index with parallel namespace queries (`substack` + `pdfs`). Routes: `GET /` serves the embedded web UI, `POST /query` is the full RAG endpoint, `GET /search` is semantic-search-only (no LLM), `GET /stats` returns KV spend aggregates, `GET /health` checks Pinecone, `POST /share` stubs transcript sharing (503 until D1 provisioned in Phase 2C). All LLM calls use `claude-sonnet-4-6` — not Haiku — because the protocol research material is dense and requires strong synthesis.

- **System prompt derived from SOUL.md, cached with `cache_control: ephemeral`:** ~600-token structured prompt covering C3PO's identity (Protocol Institute research assistant), intellectual commitments (protocols as genuine analytical category, hardness, protocolization as civilizational force, context-tank mission), voice (scholarly, source-specific, honest about limits), and characteristic analytical moves (cross-domain comparison, hardness analysis, formalization ladder, stakeholder analysis). Cached on first call; subsequent calls pay the 10× cheaper cache-read price (~/bin/zsh.30/M vs .00/M input).

- **Secondary retrieval:** When Pinecone surfaces a `doc_summary` (PDF) or `post_summary` (Substack) vector as a top hit — these are excellent for title-query matching but contain only ~500-char abstracts — the worker immediately fires a follow-up filtered query to fetch 4 real body chunks from the same document. PDFs filtered by `url` field; Substack posts filtered by `slug`. Summary hits are then removed from the result set and replaced by their body-chunk siblings, giving the LLM actual prose to synthesize rather than an abstract.

- **Web UI embedded in worker at `GET /`:** Adapted from vgr_zirp.html. A/B testing removed entirely (no persona versions for C3PO). PI design tokens applied: primary `#0F6E56` (teal), Lora body font, Outfit UI font, Instrument Serif heading. Skull SVG replaced with robot/droid head SVG (antenna + head + eyes + grille + body — C-3PO-inspired). Source badges by document type: *Protocolized* (substack), Paper, Essay, Fiction, Game. SOUL_EXCERPT adapted for C3PO persona. Stats footer, action bar (copy/download/wrapup/clear), share section, and MCP panel all adapted. Offline notice points to protocolized.io resource library instead of Contraptions subscription.

- **Normalization and context block:** Two normalize functions — `normalizePdf()` maps `type` to a label (PAPER, WORKING PAPER, ESSAY, FICTION, GAME DESIGN, DATASET, TALK/LECTURE, WORKSHOP REPORT, TEMPLATE, INTERVIEW); `normalizeSubstack()` maps `section` to label (Fictions → FICTION, Articles → ESSAY, Obliquities → ESSAY, else PROTOCOLIZED). Context block assembles as `[LABEL — Title — Authors — Year]
chunk text` separated by `---` dividers. This matches Claude's trained affinity for structured retrieval context.

---

## Session 6: Security Filters, Transcript Loop, and Chat Index UX

*2026-05-16 · 16:36–19:28 PT*

**Tracks:** worker-api, ux, operations

**Vectors upserted:** substack: 1,040 · pdfs: 766 · transcripts: 4

- **Security pre-filters (3 regexes):** `INJECTION_RE` (jailbreak attempts), `SYSEXTRACT_RE` (system-prompt extraction attempts), `CREDENTIAL_RE` (API key fishing). All blocked queries return a canned redirect to C3PO's stated purpose rather than an error, which is less adversarial and less informative to attackers.

- **Transcript submission loop:** POST /share now generates a 6-character `chatId`, stores the full conversation as `submission:{ts}:{chatId}` in KV (90-day TTL), and writes a reverse-lookup key `chatid:{chatId}` for O(1) individual fetches. `autoModerate()` immediately classifies submissions as public/private/pending based on query length, answer length, and alphabetic content ratio — no 24-hour hold. Admin can PATCH status to override.

- **Chat index (`/chats`) and individual chat pages (`/chats/:chatId`):** Modeled on vgr_zirp's transcript browser. Public view shows status=public conversations; admin view (X-Admin-Key header, stored in sessionStorage) shows all with status dropdowns. Individual chat pages render full Q/A in Lora, with the C3PO droid SVG avatar, a private wall for non-admins accessing private chats, and source citations. `/admin` now redirects to `/chats`.

- **Session tracking in query logs:** The browser generates a random 8-character `session_id` on page load and includes it with every POST /query call. The backend stores it alongside `turnNumber` in the auto-log KV entry, enabling per-session analysis (conversation reconstruction, turn-distribution stats) without any user linkability.

- **Token and word limits:** Raised `MAX_ANSWER_TOKENS` 800→1200. Added explicit "350–500 words, complete every definition fully" instruction to system prompt. This addresses observed truncation mid-definition, a symptom of chunk-boundary splits in the corpus — the permanent fix is a protocol lexicon injected into the system prompt (deferred, tracked as TODO).

---

## Session 7: /chats Page Debugging and vgr_zirp Planning

*2026-05-16 · 22:13–23:02 PT*

**Tracks:** worker-api, operations

**Vectors upserted:** substack: 1,040 · pdfs: 766 · transcripts: 4

- **/chats page partial fixes:** Three confirmed bugs fixed and deployed: (1) `handleApiChats` was returning full conversation objects (27KB+ for admin view) — now returns slim objects with `firstQ` and `turnCount` only; (2) `GET /chats/` (trailing slash) now redirects 302 to `/chats`; (3) “&larr; All conversations” link in the individual chat page was using `href="/chats/"` (trailing slash, 404) — fixed to `/chats`; (4) `cardHTML` updated to read new slim field names. Deployed. Root bug — `getElementById('chats-list-container')` returning null on laptop — persists and is unresolved.

- **Issues tracking initialized:** Created `issues/` directory. First entry: `issues/chats-page-load-failure.md` documents the `getElementById` null mystery — element is in the DOM, IDs match, script is at end of body, yet the call returns null. Ruling out ID mismatch, wrong page served, and renderChats mutations. Open hypotheses: browser extension interference, or stale Cloudflare edge cache. Next steps: test in incognito, hard reload, check Network tab for `/api/chats` response shape.

- **vgr_zirp deep dive plan:** `plans/vgrzirp-reuse.md` completed from a full code + devlog audit of the ribbonfarm_site worker stack. Key findings: 7 features to copy directly (title-anchored embeddings, tier weighting, D1, AES-256-GCM encryption, strike/ban, self-notes, per-query logging); 4 to adapt (CORPUS_MAP, lexicon, MCP, search); 5 to skip (multiple indexes, A/B testing, site hooks, session compression, KBA probe). Primary architectural divergence: C3PO needs structural retrieval (numbered frameworks in academic PDFs) that vgr_zirp has no equivalent for — tracked separately in `plans/structural-navigation.md`. Build order: tier weighting → CORPUS_MAP → D1 → encryption → strike/ban → self-notes → per-query logging → title-anchored embeddings → lexicon → MCP → structural navigation.

---
