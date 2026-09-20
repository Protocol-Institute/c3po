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

## Session 7: Curly-Quote Root-Cause Fix, Shared Subnav, New Pages

*2026-05-17 · 14:55–15:45 PT*

**Tracks:** worker-api, ux

**Vectors upserted:** substack: 1,040 · pdfs: 766 · transcripts: 4

- **Root cause: curly quotes.** The `/chats` page had been broken for two sessions — `getElementById('chats-list-container')` returning null, CSS selectors not matching, the page stuck on 'Loading conversations…'. The root cause turned out to be 314 curly/smart quotes (U+201C `“` and U+201D `”`) throughout `worker.js` — introduced during initial AI-assisted code generation — instead of straight ASCII double-quotes (U+0022). The browser's HTML parser does not recognize curly quotes as attribute delimiters, so every `id=`, `class=`, and `href=` attribute silently failed to parse. The smoking-gun clue was the broken navigation URL: clicking 'Conversations' navigated to `https://c3po.vgr-702.workers.dev/%E2%80%9D/%E2%80%9D` — the URL-encoded form of the curly-quote characters being parsed as the href value itself. Fixed via Python: `text.replace('\u201c', '"').replace('\u201d', '"')`, 0 remaining after replacement.

- **Shared subnav.** Added a consistent navigation bar across all four C3PO pages, following the vgr_zirp pattern. Three helpers injected into every HTML template: `SUBNAV_SVG` (the C3PO droid icon in teal), `SUBNAV_CSS` (flexbox nav with PI color variables), and `subnav(current)` (function that renders the bar with the active page highlighted via `class="current"`). Nav links: Ask C3PO (`/`), Conversations (`/chats`), How It Works (`/how-it-works`), Terms (`/terms`). All four pages verified live with correct active-link highlighting.

- **New pages: How It Works + Terms.** Two new static routes added: `GET /how-it-works` and `GET /terms`. Both adapted from vgr_zirp equivalents with C3PO/PI branding — teal palette, Lora body font, C3PO droid avatar, PI name and corpus description. How It Works covers the RAG pipeline at a conceptual level for non-technical visitors. Terms covers appropriate use, limitations, and data handling.

---

## Session 8: YouTube Transcript Ingest and Bibliography Mining (Explicit Pass)

*2026-05-17 · 15:45–17:44 PT*

**Tracks:** corpus-ingestion, vector-architecture

**Session costs:** haiku_enrichment: $0.02 · voyage_embedding: $0.18

**Vectors upserted:** substack: 1,040 · pdfs: 766 · videos: 2,940 · transcripts: 4

- **YouTube channel survey.** The Protocol Institute YouTube channel at `@protocol-institute` has 91 unique videos across 10 playlists, organized into named series: Bridge Atlas (5 episodes), Guest Talk Series (~30), Researcher Salon Series (~10), Protocol Town Hall Podcasts (~26), Protocol School 2025 (13), 2024 Protocol Symposium (7), and several thematic cross-lists. Videos average 60 minutes, ranging from 43 minutes (Kyle Mathews Discord workshop) to 102 minutes (Beyond Consensus). All 91 have English auto-captions available. Notable speakers across the library: Emmett Shear, Nils Gilman, JoAnne Yates, Nathan Schneider, Kevin Kelly, Renee DiResta, Bryan Johnson, Geoff Manaugh, Chris Dixon, Sarah Perry, Keller Easterling-adjacent territory throughout.

- **Caption pipeline.** `fetch_youtube_meta.py` uses `yt-dlp --flat-playlist` to enumerate all playlists, deduplicates by video ID (videos appear in multiple playlists), and downloads English auto-captions as `.vtt` files. A custom VTT parser strips timestamp lines, cue numbers, inline timing tags (`&lt;00:01:23&gt;`), and deduplicated partial lines (VTT streams emit rolling partials then the completed sentence — the parser detects when a new line starts with the previous line and replaces it). Result: clean plain text per video. 91/91 succeeded, ranging from 6,200 to 15,700 words per video.

- **Haiku enrichment (~$0.02).** `enrich_youtube.py` sends the first 3,000 characters of each transcript plus the video title to Haiku. Output: a 2–3 sentence summary naming the speaker and specific argument, 2–4 categories from the shared vocabulary, a list of speakers, and 3–5 key protocol concepts. 91/91 succeeded, 0 errors. The enrichment is notably richer than PDF enrichment because transcript openings typically include speaker introductions and explicit framing statements.

- **Ingest: 2,940 new vectors.** `ingest_youtube.py` follows the same two-vector-type pattern as PDF ingest: **body chunks** (512-token windows, 64-token overlap, title/speakers/summary prefix for embedding but not stored) and **video_summary** (one per video, full enrichment text). 2,849 body chunks + 91 summary vectors → Pinecone namespace `videos`. The `videos` namespace is now the largest single corpus source, adding roughly 900,000 words of protocol-focused spoken content. Pinecone total: 4,750 vectors across four namespaces.

- **Bibliography mining: explicit pass.** `mine_bibliography.py --mode explicit` scanned all 82 PDFs for formal reference/bibliography sections (last standalone 'References', 'Bibliography', 'Notes', etc. header in the second half of the document). Six PDFs had parseable sections: Austin (architecture essay), Kei Kreutler (artificial memory), Saffron Huang (time and consciousness), two versions of Dispatches from Cascadia (URL footnotes), and Timber Schroff's Safe New World (coal mining safety). Haiku extracted 106 unique references after deduplication. A second Haiku pass scored every reference 0–3 for protocol relevance — nothing dropped, score stored as metadata for retrieval weighting. Distribution: core (3): 7 including *Extrastatecraft*, *A Pattern Language*, *Normal Accidents*; relevant (2): 24 including Safety-I/II, Technics and Civilization; adjacent (1): 30; incidental (0): 45 including White Noise, Proust. `fetch_refs.py` (Semantic Scholar abstract fetch + OA PDF download) written but not yet run — paused to end session.

---

## Session 8: Namespace Wiring, System Prompt Enrichment, and Full-Corpus Lexicon Extraction

*2026-05-18 · 14:55–16:10 PT*

**Tracks:** worker-api, corpus-ingestion, vector-architecture

**Session costs:** haiku_lexicon_extraction: $1.10 · voyage_embedding: $0.01

**Vectors upserted:** pdfs: 766 · substack: 1,040 · videos: 2,940 · bibliography: 278 · transcripts: 4

- **Videos + bibliography wired into live worker.** Both `GET /search` and `POST /query` now query all four Pinecone namespaces in parallel: `pdfs`, `substack`, `videos`, `bibliography`. Added `normalizeVideo()` and `normalizeBibliography()` normalizers. `mergeResults()` updated to four-array signature with tier weighting: pdfs/substack at 1.0×, videos at 0.9×, bibliography at 0.85× scaled by per-entry relevance_score (0–3). POST /query secondary retrieval extended to handle `video_summary` hits: when a summary vector matches, a follow-up query fetches the actual body chunks from the same video. Fixed a latent operator-precedence bug in `normalizeBibliography` where `m.url || m.doi ? ... : null` was binding as `(m.url || m.doi) ? ...` rather than the intended `m.url || (m.doi ? ...)`. Added Talk (orange) and Reference (grey) badge CSS classes and `badgeForSource()` cases. Total corpus now queryable: 5,028 vectors across five namespaces.

- **System prompt enriched with three new blocks.** (1) **ABOUT THE PROTOCOL INSTITUTE:** PI history, SoP provenance (EF-funded, 80+ researchers, 270+ works, 2023–2024), current programs (Protocolized magazine, Protocol School 2025, Bridge Atlas, Researcher Salons, Protocol Symposium), leadership (Rao as founder/director, Stinson-Schroff and Beiko as key figures), independence. (2) **INDEXED CORPUS:** Named listing of landmark papers by title/author, YouTube series with speaker names, Substack top contributors — preventing false denials on materials that are actually indexed. (3) **PROTOCOL LEXICON:** 40-term compact block with PI-specific one-line definitions for protocolization, hardness, Kafka/Bartleby/Whitehead protocols, ETTO, dynamic non-event, protocol archetypes, addressable space, and 32 other coined or redefined terms. The lexicon uses the PI meaning, not generic definitions — preventing Claude from defining 'hardness' as a material property or 'tension' as physical force.

- **Full-corpus lexicon extraction via Haiku.** `ingest/extract_lexicon.py` sends each PDF's full text to Haiku with a structured prompt asking for defined terms, technical vocabulary, and coined concepts. Initial pass covered 12 key theoretical papers (245 terms). Extended with `--all` flag to process all 82 PDFs, yielding **914 terms from 66 papers** (16 papers had insufficient text — mostly image-only or very short supplementary materials; one failure: Virtual Structures by Sinisterra, image-based PDF). Notable new contributions beyond the initial 12: Fangting's *Composable Life* (composable life, OALife, digital hardness, algorithmic traceology), Kreutler's *Artificial Memory* (living/latent memory, lore, memory images, associative arrangement), Huang's *Control and Consciousness of Time* (horological politics, device consciousness, device-mediated social protocol), Van Epps' *Capital Enclosure* (commons, anti-rival, commons-based peer production, extraction vs. apportionment), Lang's standards paper (de facto/consortium/disruptive standards-making, failure to launch, commercial diplomacy), Rajamohan's *Dispatches from Cascadia* (bioregional protocol, downward merge, flywheel economy). All 914 entries carry `term`, `definition`, `source` (paper title), `source_slug`, and `context` (verbatim excerpt). Saved to `sources/lexicon_draft.json`.

- **Protocol Lexicon published to protocolized-website.** `src/content/resources/protocol-lexicon.md` — a `framework`-type resource, marked `featured: true`. 41 entries organized into thematic sections: Core Protocol Theory, Archetypes and Failure Modes, Protocolization as Phenomenon, Protocol Lifecycle, Participants and Roles, Hardness Sources, Addressability, Knowledge and Epistemics. Each entry has a full 2–4 sentence definition and source attribution. Considerably richer than the compact system-prompt block. Definitions derived from Haiku extraction with editorial refinement. Will be updated as lexicon curation proceeds.

- **Architecture decision: full lexicon → Pinecone, not system prompt.** 914 terms × ~47 tokens/entry ≈ 43,000 tokens — far too large for static injection (would add ~$0.04/query at Sonnet pricing and likely degrade response quality). Decision: ingest `lexicon_draft.json` as vectors into a new `definitions` namespace, retrieved on demand like any other corpus material. The system prompt retains a 40-term *anchor block* for the most critical PI-specific terms (the ones most likely to trigger wrong generic definitions). Everything else — the remaining 874 terms including Fangting's composable life vocabulary, Huang's horological politics, Kreutler's memory framework — surfaces through semantic retrieval when relevant. Planned as next ingest step.

- **Magazine lexicon plan written, not executed.** `plans/magazine-lexicon.md` covers a two-pass extraction over the Protocolized archive (116 posts): a fiction pass (72 posts) tuned to capture fictional protocols, memetically resonant concepts, and design fictions — all clearly tagged `fictional: true` to prevent C3PO from treating speculative protocols as real coordination mechanisms — and a nonfiction pass (theory/editorial/protocol-watching) tuned for coined terms and protocol-watching observations. Full post HTML available locally at `data/substack/posts/`. Estimated cost: ~$0.70. Not yet executed — will follow lexicon ingest and definitions namespace work.

---

## Session 9: Discord Ingest Pipeline

*2026-05-19*

**Tracks:** corpus-ingestion, vector-architecture, operations

**Vectors upserted:** substack: 1,040 · pdfs: 766 · videos: 2,940 · bibliography: 278 · transcripts: 4 · discord: 2,390

- **sync_discord.py — REST-only batch harvester:** No Discord gateway, no persistent process, no privileged intents — pure REST. Polls the single general channel (`#🤔-idle-protocol-musings`) with cursor-based pagination. Filter: original messages ≥20 characters, replies ≥150 characters, thread starters always included. Thread starters fetch and bundle the entire thread as one conversation chunk. Pagination fix: backfill uses `before` (descending), incremental sync uses `after` (ascending) — the two directions require opposite cursor logic. State persisted in `data/discord_state.json`.

- **Full historical ingest of #🤔-idle-protocol-musings (4 years):** 2,877 messages fetched → 2,390 ingested (1,795 originals, 465 replies, 130 bundled thread chunks). 119 unique authors, 441 URLs mentioned, 104 starred messages. Pinecone namespace `discord`: 2,390 vectors. Metadata includes `guild_id`, `channel_id`, `message_id`, `author`, `star_count`, `url_count`, chunk_type (`message`, `reply`, `thread`).

- **Claude Haiku corpus analysis via ingest/analyze_discord.py:** Stratified sample of 200 messages passed to Haiku for qualitative analysis. Finding: ~85–90% signal — unusually high for a Discord channel. 10 topic clusters identified: protocol archetypes, hardness/softness, institutional memory, protocol failure modes, AI coordination, biological/evolutionary analogues, standards bodies, urban protocols, military/security protocols, protocol fiction. Analysis saved to `sources/discord_analysis.md`.

- **Guild ID metadata patch:** After ingest, realized guild_id was missing from all 2,390 already-upserted vectors (needed for constructing Discord message URLs). Patched via `index.update()` loop — Pinecone supports metadata-only updates without re-embedding. All 2,390 vectors updated.

---

## Session 10: SIG Channel Ingest

*2026-05-19*

**Tracks:** corpus-ingestion, vector-architecture

**Session costs:** haiku_sig_summaries: $0.15

**Vectors upserted:** substack: 1,040 · pdfs: 766 · videos: 2,940 · bibliography: 278 · transcripts: 4 · discord: 2,390 · sig: 4,583

- **sync_sig.py — two-level meeting ingest:** All 4 SIG channels ingested: SIGFPT (Future Protocol Thinking), MRG (Memory Research Group), SIGPfB (Protocols for Business), ProtFiSIG (Protocol Fiction SIG). Meeting threads detected via per-channel regex patterns — each SIG has different naming conventions. Meeting threads: Claude Haiku generates a meeting summary vector (`sig_meeting_summary`) plus chunked meeting body vectors (`sig_meeting_body`). Non-meeting threads bundled as `sig_discussion`. Main channel messages filtered same as sync_discord.py.

- **Per-channel ingest results:** SIGFPT: 29 meetings, 38 discussions, 564 main msgs → 757 vectors. MRG: 16 meetings, 9 discussions, 378 msgs → 433 vectors. SIGPfB: 22 meetings, 77 discussions, 2,001 msgs → 2,214 vectors. ProtFiSIG: 11 meetings, 43 discussions, 1,072 msgs → 1,179 vectors. Total `sig` namespace: 4,583 vectors. MRG pattern required special handling: one thread was named `202500807` (9-digit date typo) — extended digit pattern to `\d{6,9}` to capture it.

---

## Session 11: Security Hardening, Header Restyle, PI Website Update

*2026-05-19*

**Tracks:** worker-api, ux, operations

**Vectors upserted:** substack: 1,040 · pdfs: 766 · videos: 2,940 · bibliography: 278 · transcripts: 4 · discord: 2,390 · sig: 4,583

- **IP strike/ban system and expanded KBA/moderation filters:** Ported strike/ban tracking from vgr_zirp: 3 strikes within 1 hour triggers a 24-hour IP ban stored in KV. KBA (Known Bad Actor) filter expanded to include Timber Stinson-Schroff, Tim Beiko, and PI infrastructure alongside VGR — prevents the oracle from being used as a social engineering or impersonation vector. Two new regex filters: DARKBECOME_RE (requests to roleplay as an unrestricted AI) and WIELD_RE (requests to weaponize protocols against people or groups). SYSTEM_PROMPT SAFETY CONSTRAINTS block updated to cover all protected individuals and assets. MCP search rate limited to 100 queries/day.

- **Worker header restyle — minimal brand bar:** Replaced the subnav nav menu approach with a minimal brand bar: C3PO robot icon + wordmark + coral Beta badge + ← protocolized.io link. Cleaner; positions C3PO as a tool within the PI ecosystem rather than a standalone product.

- **PI website update (protocol-institute.org):** `projects.html`: C3PO status badge updated to "Live · Beta"; description rewritten to name the actual technology (RAG, 12k vectors, MCP, Claude Sonnet); direct "Open C3PO →" link added. `c3po.html`: Status and Technical sections rewritten present-tense; adds live URL, corpus size (12k+ vectors), MCP server paragraph, and direct "Try it →" CTA link.

---

## Session 12: Discord/SIG Wired into Worker; Corpus Description Rewrite

*2026-05-20*

**Tracks:** worker-api, ux

**Vectors upserted:** substack: 1,040 · pdfs: 766 · videos: 2,940 · bibliography: 278 · transcripts: 4 · discord: 3,301 · sig: 4,689

- **Discord + SIG namespaces wired into all 4 query callsites:** `runMcpSearch`, `runMcpAsk`, `GET /search`, and `POST /query` now all query `discord` and `sig` in parallel with the other namespaces. `mergeResults()` signature extended to 6 input arrays + maxSources. Discord and SIG tier weights wired. `normalizeDiscord()` builds Discord message URLs from guild_id + channel_id + message_id; detects thread chunks and links to the thread. `normalizeSig()` shows SIG name + chunk type (MEETING / MEETING TRANSCRIPT / DISCUSSION / MESSAGE) + title + date. Badge CSS added: `c3po-badge-discord` (blue) and `c3po-badge-sig` (teal). Context block and export markdown/clipboard both handle discord and sig cases. MCP `search_corpus` schema namespace enum updated to include discord and sig.

- **Corpus description rewrite across UI, system prompt, and How It Works:** UI intro blurb names Discord and all four SIG groups with session counts. System prompt INDEXED CORPUS block: two new entries (Discord channels with channel description, SIG sessions listing all four groups with leads). How It Works corpus table and Pinecone index table both updated with discord/sig rows, vector counts, and tier-weight documentation. SOUL_EXCERPT (export markdown) replaced vague "285+" citation with full accurate corpus inventory. Phase note bumped to 2C.

---

## Session 13: Discord Links Fetch and Enrichment Pipeline

*2026-05-20*

**Tracks:** corpus-ingestion, vector-architecture, worker-api

**Session costs:** haiku_enrichment: $0.08

**Vectors upserted:** substack: 1,040 · pdfs: 766 · videos: 2,940 · bibliography: 278 · transcripts: 4 · discord: 3,301 · sig: 4,689 · discord_links: 6,722

- **fetch_discord_links.py — link harvesting and web ingest:** Scanned all discord + sig Pinecone vectors for URLs in message text. Harvested 3,091 unique URLs. Fetched 1,412 (222 OK in first pass, 1,929 total vectors written to `discord_links` namespace); 942 failed (connection error/timeout/410); 373 skipped (already in registry); 355 deferred (161 YouTube, 194 Twitter/X — require separate pipelines). 9 rejected by prompt injection filter (11 regex patterns + invisible-character density guard >1%). Registry stored in `data/discord_links_registry.json`. Each fetched URL gets a `discord_link` chunk vector with the page title, description, and main text content (truncated at 2,500 chars).

- **enrich_discord_links.py — relevance scoring and pruning:** Scores each fetched URL 0–3 for protocol relevance via Claude Haiku. Score 0 = no connection; 1 = tangential; 2 = relevant; 3 = core. Score-0 vectors deleted from Pinecone — reduces index noise. Results: 1,412 scored; 485 deleted (score 0); 899 kept. Score distribution: 0→485, 1→480, 2→276, 3→143. Final `discord_links` namespace: **6,722 vectors**. Bug fixed mid-run: SCORE_PROMPT JSON example had unescaped curly braces (Python f-string), caught in dry-run before full execution.

- **Worker integration — relevance-score weighting:** `normalizeWebLink()` added; `discord_links` namespace queried in all 4 callsites. Retrieval weight uses base score by relevance tier (0.55/0.65/0.75) plus a small popularity bonus (url_count from the source messages). New `c3po-badge-web` badge. Total Pinecone: 19,634 vectors.

---

## Session 14: Exhibit Extraction Plan and SIG Meeting Recovery

*2026-05-20*

**Tracks:** corpus-ingestion, operations

**Vectors upserted:** substack: 1,040 · pdfs: 766 · videos: 2,940 · bibliography: 278 · transcripts: 4 · discord: 3,301 · sig: 4,689 · discord_links: 6,722

- **Structural navigation plan revised (plans/structural-navigation.md):** Reframed from section summaries + list extracts to four exhibit types: `section_summary`, `list_exhibit`, `figure_exhibit`, `table_exhibit`. Sampled 5 PDFs: SoP papers embed 20–30 full-page background images (design artifacts, filter by >80% page size); pdfplumber table extraction is unreliable (picks up typographic grids as tables). Strategy: two-pass — Haiku text pass for sections + lists; PyMuPDF render + Haiku Vision for figures + tables. Core essays (>12 text pages) get section summaries; all documents get list pass; visual pages (<50 words) get Vision pass. Estimated: ~450–550 new vectors, ~$1.30, new dep: pymupdf. Not yet built.

- **Discord bot architecture documented (DISCORD_BOT_DESIGN.md):** Two-bot architecture: `c3po_listener` (headless launchd batch scripts — no gateway, no persistent process) and `c3po_oracle` (interactive slash-command bot via Discord Interactions webhook → Cloudflare Worker deferred response). Listener: 3 launchd plists planned — daily Discord sync, biweekly SIG sync + website pipeline, weekly link enrichment. Oracle: /ask, /search, /help commands. Key constraints documented: Discord CDN attachment URLs expire in 24h (must download at sync time); SIG thread auto-archive cadences (SIGFPT/ProtFiSIG 3 days, SIGPfB 7 days) mean sync must run at minimum every 2–3 days or active threads will be missed. Phase 3A–3F roadmap.

---

## Session 15: Missed SIG Meetings Recovered; Active-Thread Fix

*2026-05-20*

**Tracks:** corpus-ingestion, operations

**Session costs:** haiku_sig_summaries: $0.05

**Vectors upserted:** substack: 1,040 · pdfs: 766 · videos: 2,940 · bibliography: 278 · transcripts: 4 · discord: 3,301 · sig: 4,795 · discord_links: 6,722

- **Root cause fix — active threads invisible to sync:** The channel-level `/threads/active` endpoint returns 404 for forum-style SIG channels — deprecated. Replaced with the guild-level `/guilds/{id}/threads/active` endpoint filtered by `parent_id`, which works for all channel types. SIGFPT and ProtFiSIG threads auto-archive after 3 days; SIGPfB after 7 days. Any meeting thread still active at sync time (e.g., a very recent meeting) was silently missed by the old code. Added `is_meeting_override` field to per-thread state in `data/sig_state.json` — allows manual classification without code changes.

- **SIGPfB pattern extensions:** Two patterns added to capture previously unmatched meeting threads. Extended `^Protocols for Business` to match `:` as well as `[` and space as delimiters. Added `^[\*=]+\s*(?:SIG\s+)?Protocols for Business` pattern for threads with decorative `====` or `**===` prefixes.

- **10 missed meetings recovered:** SIGFPT: 01May26 Stigmergy (38 msgs), 15May26 Stigmergy Part II (52 msgs). SIGPfB: 20Apr26 API Design, 04May26 Technology, 18May26 Manufacturing, 03Nov25 FDEs, 08Dec25 LLM Adoption, 12Jan26 AI Infrastructure. ProtFiSIG: 12Mar26 Protocol Fairy Tales, 23Apr26 Wile E. Coyote. sig namespace: 4,583 → 4,795 vectors. Website SIG pages updated: 80 → 88 meetings across 4 SIGs.

---

## Session 16: Channel Manifest, Onboarding Tool, Daily launchd Sync

*2026-05-20*

**Tracks:** operations

**Vectors upserted:** substack: 1,040 · pdfs: 766 · videos: 2,940 · bibliography: 278 · transcripts: 4 · discord: 5,518 · sig: 4,795 · discord_links: 6,722

- **data/channel_manifest.json — unified channel registry:** Single JSON file tracks all monitored channels with schema: type (general|sig|forum), namespace, status (active|archived), meeting_patterns, thresholds, onboarding_notes, added date. Tracked in git via `!data/channel_manifest.json` exception in .gitignore. `sync_discord.py` and `sync_sig.py` both read from manifest via `load_general_channels()` and `load_sig_channels()` respectively, with fallback to env vars / hardcoded dict if manifest is absent. Eliminates the need to edit script source to add or remove channels.

- **ingest/onboard_channel.py — semi-automated channel onboarding:** Given `--channel &lt;id&gt;`, fetches a sample of recent messages and threads via REST, passes them to Claude Sonnet with an analysis prompt asking for channel classification (type, theme, meeting patterns, relevance). Prints proposed manifest config for human approval. Supports `y/N/edit` interactive prompt plus `--yes` flag for scripted use. If approved, appends entry to manifest and optionally triggers `sync_discord.py --channel &lt;id&gt;` backfill via subprocess. Reduces time to onboard a new channel from ~30 minutes of manual config to ~2 minutes.

- **bin/daily_sync.sh and launchd plist installed:** Shell coordinator runs: sync_discord.py → sync_sig.py → rebuild_sig_summaries.py → generate_sig_pages.py → conditional website push (only if git status shows changes to sigs/ or sigs.html). Website push commits and pushes `sigs/` and `sigs.html` with a datestamped auto-commit message. Installed as `~/Library/LaunchAgents/org.protocol-institute.c3po.daily.plist`; `StartInterval: 86400` (fires 24h after last successful run, compatible with sleep — fires on next wake). Logs to `~/Library/Logs/c3po/daily.{log,err}`. Manual run tested successfully.

---

## Session 17: Archived Channel Sweep; Forum Channel Support

*2026-05-20*

**Tracks:** corpus-ingestion, operations

**Vectors upserted:** substack: 1,040 · pdfs: 766 · videos: 2,940 · bibliography: 278 · transcripts: 4 · discord: 5,518 · sig: 4,795 · discord_links: 8,864

- **7 archived general channels onboarded via onboard_channel.py:** #credit-protocols, #death-memory, #unconscious-protocols, #tech-standards, #built-environment, #organizational-protocols, #field-reports (all archived general text channels). Plus #affiliate-chat (archived SIG channel). All backfilled and ingested. URL registry grew from ~1,800 to 3,824 URLs across all channels. discord namespace grew from ~3,301 → 5,518 vectors.

- **Forum channel support (Discord type=15):** Forum channels have no main channel messages — all content lives in threads (forum posts). New `fetch_forum_threads()` queries guild-level active + archived public thread endpoints filtered by parent_id, then fetches each thread's messages. New `format_forum_post_chunk()` bundles a thread opener + all replies into one chunk with `chunk_type=forum_post`. State tracked in `last_thread_ids` dict (separate key from `last_message_ids` for text channels). #reading-room: Discord type=15, 81 posts ingested. `onboard_channel.py` updated to detect type=15 channels, propose `type=forum` config, and store `discord_type: 15` in the manifest entry.

- **Monitoring Dashboard design — silent listener pattern:** Rejected Discord heartbeat posting (listener bot should be invisible). Instead: all sync scripts write structured JSON entries to `data/sync_log.json` (90-day rolling). New `ingest/generate_monitoring_page.py` reads log + manifest + links registry and produces a static HTML page pushed to the PI website at `monitoring.html`. Wired into `bin/daily_sync.sh`. Produces a channel table (type, status, vector count, last sync), a 14-day run history (per-script stats), and a link registry status table.

---

## Session 18: Lexicon Definitions Namespace; Attachment Capture; YouTube Transcripts

*2026-05-20*

**Tracks:** corpus-ingestion, vector-architecture, operations

**Vectors upserted:** substack: 1,040 · pdfs: 766 · videos: 2,940 · bibliography: 278 · transcripts: 4 · discord: 5,518 · sig: 4,795 · discord_links: 8,864 · definitions: 560

- **Lexicon schema migration: flat dict → list-of-dicts.** `sources/lexicon_draft.json` migrated from `{term: entry_object}` to `{term: [entry_object, ...]}`. Motivation: terms like *protocol* have multiple competing definitions across 67 source documents — the flat schema silently dropped duplicates during extraction. The new schema supports multiple entries per term. All 914 existing entries wrapped in single-item lists (all currently variant_count=1). Future: a second extraction pass or editorial curation can append additional entries to any term's list.

- **ingest/sync_lexicon.py — definitions namespace ingest:** Ingests triage a+b entries only (239 + 321 = 560 entries; triage c excluded as standard terms without PI-specific usage). Vector ID scheme: `lexicon__{term_slug}__{source_slug}` — ensures uniqueness across both terms and sources, supports multiple definitions per term as distinct vectors. Metadata: `term`, `definition_index`, `variant_count`, `source`, `source_slug`, `triage`, `triage_reason`, `chunk_type=lexicon_definition`. Text format: bold term → Definition → Context → Source (markdown-structured for Claude synthesis). Non-ASCII slugify bug fixed: some term names contained Unicode characters (e.g., *poché*) — ASCII-stripped before slugification. 560 vectors upserted to `definitions` namespace.

- **Attachment capture in sync scripts:** New `ingest/attachments.py` downloads Discord CDN attachments at ingest time, before the 24-hour CDN URL expiry. Storage: `data/attachments/{channel_id}/{msg_id}/{filename}` (gitignored, local only; Dropbox-ignored via xattr). PDF attachments extracted via pdfplumber and embedded as separate `discord_attachment` / `sig_attachment` vectors. Plain-text and markdown attachments embedded directly. No backfill of historical attachments (deferred indefinitely — CDN URLs for past messages have long expired). Both `sync_discord.py` and `sync_sig.py` updated to call `process_attachments()` during ingest.

- **YouTube transcript pass (via youtube-transcript-api):** 195 deferred YouTube URLs in the discord_links registry. First attempt failed: 0/195 succeeded due to API version mismatch — newer `youtube-transcript-api` uses instance API `YouTubeTranscriptApi().fetch(video_id)` not the class method `get_transcript()`. Fixed, reset all 195 from `failed` back to `deferred`, re-ran. Second pass: 18/185 succeeded (170 remaining — mostly restricted/age-gated videos or videos with captions disabled). Successful transcripts embedded as `discord_link` vectors in the `discord_links` namespace.

---

## Session 19: c3po_oracle Discord Bot — Code Complete

*2026-05-26*

**Tracks:** discord-bot, api

**Vectors upserted:** substack: 1,040 · pdfs: 766 · videos: 2,940 · bibliography: 278 · transcripts: 4 · discord: 5,518 · sig: 4,795 · discord_links: 8,864 · definitions: 560

- **POST /interactions — Discord Interactions webhook endpoint:** Implements the full Discord slash-command webhook flow in `api/worker.js`. Verifies the Ed25519 request signature using `crypto.subtle` (SubtleCrypto native, no dependencies). Handles the Discord PING/PONG handshake required during webhook registration. Routes APPLICATION_COMMAND interactions to /ask, /search, and /help handlers. Channel gating via `ORACLE_ALLOWED_CHANNEL_IDS` env var (comma-separated; empty = all channels). Security filters (injection, system prompt extraction, credential fishing, KBA, etc.) applied to query text before queuing. Per-user rate limit: 5 queries/hour tracked by Discord user ID in KV. /ask and /search return `{type:5}` (DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE), showing a "thinking..." state in Discord while the queue processes the RAG job.

- **runRagQuery() — shared RAG core extracted from POST /query:** The embed → 7-namespace Pinecone query → secondary retrieval (doc_summary / post_summary / video_summary expansion) → normalize → merge → Claude call pipeline is now a standalone `async function runRagQuery(query, env, ctx, opts)`. Options: `includeAnswer` (skip Claude for search-only mode), `history` (conversation turns, preserved for the HTTP endpoint), `maxTokens` (Discord uses 800 to fit within 2000-char message limit; HTTP endpoint uses env.MAX_ANSWER_TOKENS). POST /query handler is now a thin wrapper: parse + validate + rate-limit + security check → call runRagQuery → logQuery + return. No behavioral change to existing routes.

- **Cloudflare Queue consumer — `async queue(batch, env, ctx)`:** Added to `export default` alongside `fetch` and `scheduled`. Processes one message at a time (`max_batch_size=1`). Each message carries `{commandName, query, applicationId, interactionToken}`. Consumer calls `runRagQuery`, formats the result, and POSTs to Discord's followup webhook: `DISCORD_API/webhooks/{applicationId}/{interactionToken}`. On error, posts a generic error message so the "thinking..." state doesn't hang. Queue name: `c3po-oracle` (needs `wrangler queues create` before first deploy).

- **scripts/register_discord_commands.py:** One-time setup script that bulk-registers the three Oracle slash commands with Discord via the REST API. Commands: `/ask &lt;question&gt;` (RAG with synthesis), `/search &lt;query&gt;` (semantic search, no LLM), `/help` (ephemeral info). Supports `--guild-id` for instant guild-scoped registration (testing) and `--global` for production. Full 8-step deploy guide in `plans/oracle-bot-setup.md`.

---

## Session 20: Definitions Namespace Live; Substack Sync

*2026-05-27*

**Tracks:** worker-api, corpus-ingestion

**Vectors upserted:** substack: 1,057 · definitions: 560 · discord: 5,533 · discord_links: 9,064 · sig: 4,905 · videos: 2,940 · pdfs: 766 · bibliography: 278 · transcripts: 4 · total: 25,184

- **Definitions namespace wired into query pipeline:** Added `normalizeDefinition()` normalize function and extended all four query paths — `runRagQuery()`, `runMcpSearch()`, `runMcpAsk()`, and the `POST /search_corpus` handler — to query the `definitions` namespace in parallel with the other 7. Results weighted at 1.0× (same as PDFs and Substack), labeled as `[LEXICON — 	erm\ — PI-coined | PI-specific]` in the context block. The MCP `search_corpus` tool now also accepts `"definitions"` as a filterable namespace value. Smoke test confirmed lexicon hits ("hard protocol", "hardness", "institution hardness") surfacing in top sources for relevant queries.

- **Substack sync — two posts ingested:** Ran `sync_substack.py` to pick up a new post (*the-overloaded-train*) and re-upsert an edited post (*introducing-the-protocol-institute*). 31 vectors upserted; substack namespace grew from 1,040 to 1,057.

- **Cloudflare Queue created (oracle bot prerequisite):** The `c3po-oracle` queue was blocking worker deployment (wrangler.toml references it as producer + consumer). Created it via `wrangler queues create c3po-oracle` — Step 2 of the oracle bot setup plan. Discord app creation and the remaining oracle bot steps (Steps 1, 3–8) are still pending.

---

## Session 21: Discord Bot: Thread Continuation + Introductions Monitoring

*2026-05-27 · ~14:30–15:00*

**Tracks:** discord-bot

**Vectors upserted:** substack: 1,057 · definitions: 560 · discord: 5,533 · discord_links: 9,064 · sig: 4,905 · videos: 2,940 · pdfs: 766 · bibliography: 278 · transcripts: 4 · total: 25,184

- **Thread continuation (up to 5 turns):** The bot now responds to any follow-up message posted in a thread it created, without needing a fresh @mention. It fetches the full thread history, recovers the original query from the parent channel message (Discord sets `thread.id == original_message.id`), builds a `history[]` array of `{role, content}` objects, and passes it to the Worker’s existing multi-turn RAG endpoint. After 5 bot turns it sends a graceful limit message with a redirect to the web UI.

- **Introductions channel monitoring:** The bot now watches `#introductions` (channel ID from the Discord URL). When a new top-level post appears, it sends the introduction text to the Worker along with a menu of the four active SIGs—SIGFPT, MRG, SIGPfB, ProtFiSIG—with their Discord `&lt;#ID&gt;` mention format. Claude picks the best-fit reading recommendation and SIG; the bot replies with a warm greeting, the answer, and the top source link. Replies within #introductions are skipped.

- **Bot process management:** Restarted the bot (PID 37015) after the update. Added a running-process block at the top of `status.md` recording the current PID, log path, and restart command—so future sessions can find and manage the bot process without hunting.

---

## Session 22: Pubsub Refactor Phase 1: Registry Layer + BaseSource ABC

*2026-05-28*

**Tracks:** operations, corpus-ingestion

**Vectors upserted:** substack: 1,057 · definitions: 560 · discord: 5,537 · discord_links: 9,108 · sig: 4,932 · videos: 2,940 · pdfs: 766 · bibliography: 278 · transcripts: 4 · humboldt: 94 · total: 25,276

- **Architecture pivot: c3po as knowledge broker.** Decided to refactor c3po from a tightly-coupled RAG endpoint into a general-purpose pubsub-style orchestrator for all Protocol Institute archival corpus needs. Key concepts: sources (publishers) feed a shared Pinecone index; sinks (consumers) query it. Three ownership tiers for all corpus resources: *owned* (c3po creates and maintains), *subscribed* (another PI project owns; c3po syncs read-only), and *aware* (external/federated; c3po knows it exists but has no pipeline). The existing ingest scripts and Worker API continue to function unchanged throughout the refactor.

- **config/source_registry.json — unified source manifest.** Machine-readable registry of all 11 corpus sources (10 owned, 1 aware). Fields per source: `source_id`, `ownership`, `namespace`, `source_type`, `schedule` (cadence + cron expression), `ingest_module`, `ingest_script` (legacy path), `state_file`, `access_level`, `status`. Supersedes the earlier `sources/REGISTRY.md` and per-source `registry.json` files as the authoritative manifest. The `humboldt` source is tagged `aware` with a note that it may be reclassified to `subscribed` when the Humboldt–c3po interface is formalized.

- **config/sink_registry.json — consumer manifest.** Registry of all 7 sinks (3 active, 4 planned). Each sink declares its allowed namespaces, ownership_filter, access_level, and sink_type. Active sinks: web_ui (public HTTP query), discord_bot (bin/c3po_bot.py), mcp (SSE tool server). Planned: oracle_bot (slash commands), listener_bot (Discord filter), sig_publisher (website publication), humboldt (agent direct-access).

- **config/corpus_map.json — namespace inventory with ownership.** Single-file inventory of all 10 Pinecone namespaces: ownership tier, source_id backlink, vector count, default_query_include flag, query weight. The `humboldt` namespace is explicitly tagged `aware` and `default_query_include: false`. The historical naming quirk (YouTube source uses namespace `videos`, not `youtube`) is documented. This file is the authoritative corpus snapshot; vector counts should be updated after each ingest session.

- **ingest/base.py — BaseSource ABC.** Defines the contract for all source modules: `source_id`, `namespace`, `ownership` class attributes; abstract `run(dry_run, **kwargs) → IngestResult`; optional `status() → dict` and `supports_incremental() → bool`. `IngestResult` is a dataclass with fields for vectors_upserted, vectors_deleted, items_processed, errors, and metadata.

- **ingest/sources/ — source module stubs.** 8 concrete source classes, each implementing BaseSource: SubstackSource, DiscordSource, SIGSource, YouTubeSource, PDFSource, BibliographySource, DefinitionsSource, DiscordLinksSource. Phase 1 implementations are subprocess wrappers that call the existing ingest scripts with the correct cwd and flags. No existing script modified. A `REGISTRY` dict in `__init__.py` maps source_id strings to classes — the entry point for the future orchestrator.

- **First new source ingested: Durable AI Adoption (ai.protocolized.dev).** SIGP4B's practical AI adoption guide (v0.5, May 2026) ingested as the first test of the new pipeline. PDF had full text layers (53 pages, 14MB); pdfplumber extracted cleanly. Added enriched_meta entry by hand with title, summary, categories, URL, authors, and tags. Patched `ingest_pdfs.py` to prefer enriched_meta fields (title, date, doc_type, tags, url) over protocolized-website markdown when present — enables ingesting external web resources without a resource markdown stub. 34 vectors upserted (33 body + 1 doc_summary); pdfs namespace: 766 → 800. URL in Pinecone metadata: `https://ai.protocolized.dev/`.

- **protocol-institute.org: Resources section added to SIGPfB page.** New "Resources" section added above the meeting archive on `sigs/sigpfb/index.html` — distinguishes published output artifacts from meeting transcripts. First entry: *Durable AI Adoption*, with a description, five topic tags (AI Adoption Maturity, Governed &amp; Cultivated Tracks, Organizational Protocols, Case Studies, Assessment Tool), and a C3PO chat link inviting visitors to explore the guide alongside the broader PI corpus.

---

## Session 23: PDF URL fix, answer length fix, SIG meeting page ingest

*2026-05-28*

**Tracks:** worker-api, corpus-ingestion, vector-architecture

**Session costs:** voyage_embedding: $0.03

**Vectors upserted:** sig: 5,027 · total: 25,406

---

## Session 24: Bot ecology Phase B: Discord conversation self-memory

*2026-05-28*

**Tracks:** operations, worker-api, vector-architecture

**Vectors upserted:** transcripts: 4 · total: 25,407

---

## Session 25: Discord Awareness System: channel registry, guide embeddings, intro handler, event sync

*2026-05-29*

**Tracks:** operations, vector-architecture, worker-api

**Vectors upserted:** discord_guide: 78 · total: 25,411

---

## Session 26: Phase D + welcome queue + intro recs overhaul + nav queries + GHA cron

*2026-05-30 · 08:30-11:02*

**Tracks:** operations, vector-architecture, worker-api

**Vectors upserted:** discord_links: 9,160 · discord: 5,548 · sig: 5,062 · videos: 2,940 · substack: 1,057 · definitions: 560 · pdfs: 800 · bibliography: 278 · discord_guide: 78 · transcripts: 9 · humboldt: 468 · total: 25,960

---

## Session 27: Full infrastructure migration to PI org — CF Worker, GitHub, Pinecone, Voyage AI

*2026-05-31 · 09:00-11:00*

**Tracks:** operations, worker-api

**Vectors upserted:** discord_links: 9,188 · discord: 5,552 · sig: 5,077 · videos: 2,940 · substack: 1,065 · definitions: 560 · pdfs: 800 · bibliography: 278 · discord_guide: 78 · transcripts: 9 · humboldt: 468 · total: 26,015

---

## Hotfix: Intro handler — forward-only watermark, new-member filter, mention passthrough

*2026-05-31 · 11:38*

**Tracks:** operations

---

## Session 28: PDF URL fix (files.protocolized.io) + cover-letter filter in intro recs

*2026-05-31 · 12:00*

**Tracks:** vector-architecture, worker-api, operations

**Vectors upserted:** discord_links: 9,188 · discord: 5,552 · sig: 5,077 · videos: 2,940 · substack: 1,065 · definitions: 560 · pdfs: 750 · bibliography: 278 · discord_guide: 78 · transcripts: 9 · humboldt: 468 · total: 25,965

---

## Session 29: Devlog Ingest Pipeline, Personality Split, and Architecture Doc

*2026-06-01 · 14:00–17:30 PT*

**Tracks:** worker-api, corpus-ingestion, vector-architecture, operations

**Session costs:** voyage_embedding: $0.02

**Vectors upserted:** meta: 29

- **Discord/web personality split** — Added `DISCORD_SYSTEM_PROMPT`: appends a DISCORD VOICE OVERRIDE block to the base system prompt, capping responses at 2–3 sentences with an office-manager tone and one named citation. The `context=&quot;discord&quot;` field in the request body selects the variant; the queue handler hardcodes it for slash commands. Both variants exceed the 1024-token prompt-cache threshold and cache independently.

- **Devlog ingest — Pinecone** — New `ingest/sync_devlog.py`: embeds one vector per devlog session into the new `meta` Pinecone namespace. Idempotent via SHA-256 content-hash state file. Anchor URLs point to `protocolized.io/resources/c3po-devlog#session-{id}` for deep-linking. 29 sessions upserted on first run.

- **Devlog ingest — D1 page** — New `ingest/generate_devlog_page.py`: renders `data/devlog.json` to markdown with `&lt;a id=&quot;session-{id}&quot;&gt;` anchors before each heading (marked.js does not support Pandoc-style IDs; raw HTML passthrough works). Upserts to D1 table `resources` as slug `c3po-devlog` via `npx wrangler d1 execute --remote --file`. Idempotent. 65,609-char body published to protocolized.io/resources/c3po-devlog.

- **Worker: meta namespace wired** — `normalizeDevlog()` normalizer added. `mergeResults` signature extended to 11 params (metaItems added as 9th). All 3 RAG paths (`runMcpSearch`, `runMcpAsk`, `runRagQuery`) now query the `meta` namespace (top 3 results). `buildContextBlock` gains a `devlog` case. Worker deployed: version `5a7fc01f`.

- **Daemon: 2 new steps** — `bin/daemon.py` updated with step 13 (`sync_devlog`) and step 14 (`generate_devlog_page`), keeping devlog vectors and the published page automatically current on every 30-minute cycle.

- **Bug fixes** — (1) `generate_devlog_page.py` CF token fallback path was `Code/.env.keys`; fixed to `protocol-institute/.env.keys`. (2) Wrangler 4.x defaults to `--local`; script now explicitly passes `--remote` for production D1 writes.

- **Architecture doc** — New `ARCHITECTURE.md` at c3po root: consolidated from stale `ARCHITECTURE.md` and `plans/bot-ecology.md`. Covers current 3-node topology, all 12 daemon steps, all namespaces with vector counts and weights, personality split, infrastructure table, design principles, session history table (sessions 1–29), and planned work queue.

- **.org website C3PO page** — Updated `website/c3po/index.html`: corpus count corrected (~26,000 across 10 namespaces), added Discord bot and MCP server sections, added lexicon use-case bullet, updated copyright year.

---

## Session 30: VPS Migration Plan

*2026-06-03 · 18:00–19:30 PT*

**Tracks:** operations

- **Gateway hosting assessment** — Evaluated options for moving the Discord bot off the laptop. Cloudflare Workers can't hold a persistent gateway connection (short-lived, no WebSocket keepalive for Discord's protocol). Railway and VPS are the viable options. Slash commands via the existing oracle bot already run on Cloudflare; @mention-based conversations require a real gateway process.

- **PI org process survey** — Surveyed all four Protocol Institute projects for persistent backend needs. Result: only c3po (daemon + gateway bot) and humboldt (research daemon + Discord bot) have always-on process requirements. The protocolized-website and protocol-institute.org are fully static/edge with no backend.

- **Hetzner VPS chosen over Railway** — A Hetzner CX22 (€3.29/mo, 2 vCPU, 4GB RAM) runs all 4 processes under systemd. Key reasons to prefer it over Railway: the bot conversation spool files are written by `c3po_bot.py` and read by `sync_bot_conversations.py` — colocating both on the same host means no transport redesign. Cross-repo git pushes for the website also work naturally on a VPS with a persistent filesystem. Railway would need separate services per process and is messier for git operations.

- **Migration plan written** — `plans/vps-migration.md`: 6 phases covering server setup (pyenv Python 3.14, deploy keys for 3 repos), c3po migration (2 systemd units, absorb GHA substack workflow into daemon step 0), humboldt migration (1 systemd unit), GHA retirement, and decommission of the remaining personal Cloudflare Worker and Pinecone index. Three open questions flagged: git identity for automated commits, humboldt notebook publish deploy key, and humboldt budget circuit breaker under systemd.

---

## Session 31: Discord Bot Fixes, SIGPSY + DRG Onboarded, Personal Account Cleanup

*2026-06-06 · 10:00–13:30*

**Tracks:** discord-bot, corpus-ingestion, operations

**Vectors upserted:** sig: 5,311 · discord_links: 9,640 · discord: 5,578 · substack: 1,080 · transcripts: 22 · meta: 31 · total: 26,268

- **Thread 5-turn cap: once-only** &mdash; The cap notice (“We’ve reached the 5-turn limit”) was being re-sent on every subsequent message in a capped thread because the cap message itself was being counted as a bot turn, keeping `bot_turns &ge; MAX_THREAD_TURNS` indefinitely. Fix: exclude the cap notice from the turn count via `_TURN_LIMIT_MARKER`, and check whether the cap notice already exists in thread history before sending it again. Subsequent messages are now silently ignored.

- **Side-conversation filtering in threads** &mdash; The bot was responding to every non-bot message in its own threads, including conversations between members that weren’t addressed to it. Fix: if the incoming message is a Discord reply (`message.reference is not None`) and the referenced message is from a human (not the bot), skip it — unless the bot is explicitly @mentioned. Standalone messages with no reply reference still trigger the bot normally. Also added mention-stripping from thread reply text, which was previously being passed verbatim to the Worker.

- **Intro suggested-reading coherence** &mdash; Root cause of a report where C3PO’s answer named “Unprotocolized Knowledge” as the best starting point but the Suggested reading link pointed somewhere else: the answer text and the `rec_sources` link were independently derived. Claude synthesized its answer from the full retrieved context and could name any source in the context block; `rec_sources` was just the highest-ranked non-VGR entry from the Pinecone merge results, which might be a Discord message or Substack post ranked above the PDF. Fix: scan `all_sources[:8]` for any source whose title (parenthetical suffixes stripped) appears in the answer text, and prefer that as the suggested reading link. Fall back to rank order only if no title match is found.

- **SIGPSY onboarded** &mdash; Added #⏳-psychohistory (ID 1508205168661893180) as a `sig`-type channel. Backfill: 65 vectors — 1 meeting (SIGPSY Kickoff, 4 Jun 2026, 6 topics, 4 insights), 13 discussion threads (World Machines book club: *Revolt of the Masses*, *Business of Enlightenment*; side quests; authentication flow brainstorm; book club list), and main channel messages. Calendar event matched: biweekly Thursdays 16:00 UTC, next 18 Jun.

- **DRG onboarded** &mdash; Added #🦸-distributed-robotics (ID 1508175637020676259) as a `sig`-type channel (onboard script initially classified it as `general` since no meeting threads existed yet; manually upgraded). Backfill: 43 vectors — 5 threads (NASA Space Roboticist Challenge, PI-DRG Network, GD&amp;T as tolerance protocol, reading queue, robot integrations) and main channel messages. First meeting next week. Calendar event matched: biweekly Thursdays 15:30 UTC.

- **sig_display auto-seeding fix** &mdash; New SIG channels were not matching their Discord calendar events because `sync_discord_channels.py` builds guide entries from live Discord API data, which has no `sig_display` field. The matcher’s first rule is a `sig_display` substring check, so SIGPSY’s event (“SIGPSY - Psychohistory”) went unmatched despite being an obvious hit. Fix: `sync_discord_channels.py` now loads `channel_manifest.json` at startup and seeds `sig_display` from there for any `type: sig` channel that doesn’t already have one set. The field then survives future sync cycles via `PRESERVED_FIELDS`.

- **generate_sig_pages.py dual-output format** &mdash; The script was writing only `sigs/{slug}.html` (relative-path format, served at `/sigs/sigfpt.html`). The website serves SIG archive pages at clean URLs (`/sigs/sigfpt/`) via `sigs/{slug}/index.html`. New SIG stubs (SIGPSY, DRG) existed only at the index path. Fix: the script now writes both formats on each run, converting relative to absolute asset paths (`../assets/` → `/assets/`) for the index version. All six SIG `index.html` files are now regenerated and kept in sync on every daemon cycle.

- **Personal account pre-deletion cleanup** &mdash; Two live code files still referenced the old personal CF Worker URL (`c3po.vgr-702.workers.dev`): `config/sink_registry.json` (endpoint field) and `ingest/sync_web_chats.py` (`WORKER_BASE`). Both updated to `c3po.protocolized.io`. `config/corpus_map.json` still had the old personal Pinecone index host (`c3po-bwo39z7`); updated to the PI org host (`c3po-1os2tli`). Full audit confirmed: CF Worker, Pinecone index, Voyage AI, and GitHub repo are all fully on PI org accounts. Personal CF Worker and personal Pinecone index are now safe to delete from c3po’s perspective.

---

## Session 32: Returning-Member Welcome, How-It-Works Rewrite

*2026-06-08 · 10:47–13:17*

**Tracks:** discord-bot, worker-api

**Vectors upserted:** discord_links: 9,674 · discord: 5,580 · sig: 5,346 · videos: 2,940 · substack: 1,080 · definitions: 560 · pdfs: 750 · bibliography: 278 · discord_guide: 78 · meta: 32 · transcripts: 23 · total: 26,341

- **Returning-member welcome path** &mdash; `NEW_MEMBER_DAYS` raised from 30 to 60 in both `bin/c3po_bot.py` and `bin/seed_welcome_queue.py`. Diagnosed via two missed intros: Shreeram (39 days old, slipped past old threshold) and Rob Knight (801 days, old member reintroducing). `handle_introduction(returning=True)` now handles members who joined &gt;60 days ago and post &ge;80 chars in #introductions: greeting is &ldquo;Hi @user &mdash; looks like you joined a while back and are getting more active. Welcome back!&rdquo; followed by the same corpus rec + channel suggestion. The returning-member path is *not* queued (welcome queue is for critical first-time new-member welcomes). Session log gains a `returning: bool` field.

- **/how-it-works full rewrite** &mdash; The live page at `c3po.protocolized.io/how-it-works` was substantially stale: all corpus counts from before session 22, SIG list missing SIGPSY and DRG, 8 namespaces instead of 11, wrong tier weights, stale SOUL.md description, old `vgururao/c3po` repo link, and &ldquo;Phase 2C&rdquo; dev status. Rewrote from 5 sections to 8. New sections: **Ingest pipeline pattern** (3-layer approach with JSON examples and a new-source checklist an agent can follow exactly); **Query pipeline step-by-step** (8 steps from embed to return); **Delivery interfaces** (web UI, Discord bot, MCP split into dedicated subsections). Discord bot section documents @mention/thread, nav queries, #introductions handler (both new-member and returning paths), slash commands, and the spool pattern. System prompt accurately described as a 7-section inline document in `worker.js` &mdash; not loaded from SOUL.md at runtime. All 11 namespaces with current vector counts and correct tier weights. Deployed: version `fb576704`.

---

## Session 33: Personal Infra Decommission, SIG Pages, Corpus Dashboard

*2026-06-13 · 11:05–13:10*

**Tracks:** infra, sig-pages, worker-api

**Vectors upserted:** discord_links: 9,889 · discord: 5,590 · sig: 5,504 · videos: 2,940 · substack: 1,101 · definitions: 560 · pdfs: 750 · bibliography: 278 · discord_guide: 78 · meta: 33 · transcripts: 25 · total: 26,748

- **Personal infra decommission** &mdash; Deleted the personal Cloudflare Worker (`c3po` on `vgr-702` account) and the personal Pinecone `c3po` index. The CF worker required removing the `c3po-oracle` queue consumer binding first via the CF API, then deleting the orphaned queue. Both PI org endpoints (`c3po.protocolized.io` and the PI Pinecone index) verified healthy post-deletion. Humboldt confirmed unaffected &mdash; it was already pointing at PI org keys.

- **SIG pages &mdash; new update protocol** &mdash; Familiarized with the website agent’s `CONVENTIONS.md` contract: c3po writes individual session detail pages (`sigs/&lt;slug&gt;/&lt;date-slug&gt;/index.html`) and patches the SIG index to link meeting titles; it never rewrites the full index. Wrote `ingest/update_sig_pages.py` as the canonical replacement for the unsafe `generate_sig_pages.py`. Added 5 new session detail pages (SIGPfB Jun 1, SIGPSY Jun 4, MRG Jun 4, ProtFiSIG Jun 11, SIGFPT Jun 12) plus the DRG Kickoff (Jun 11) after fixing the `DRG#\d+` meeting pattern that was missing from the channel manifest. All 6 new pages live on protocol-institute.org and ingested into Pinecone.

- **Corpus status dashboard** &mdash; `c3po.protocolized.io/status` is now live. The daemon assembles a JSON blob (Pinecone namespace vector counts, per-SIG meeting stats, patrol manifest) via `ingest/publish_dashboard.py` and PUTs it to the Worker via `PUT /api/admin/dashboard`; the Worker stores it in KV and serves it from `GET /status`. Three sections: Indexed Namespaces (live vector counts), SIG Meeting Archive (meeting count + last date + last sync), Patrol Manifest (all 12 ingest sources with cadence and last-run). Linked in the c3po subnav. Updates every 30 min as daemon step 15. The c3po page on protocol-institute.org was minimized to a one-liner + link.

---

## Session 34: Protocolized-Website Resource Pipeline

*2026-06-15*

**Tracks:** operations, corpus-ingestion

**Vectors upserted:** total: 26,881 · discord: 5,597 · discord_links: 9,943 · sig: 5,574 · substack: 1,101 · videos: 2,940 · pdfs: 750 · definitions: 560 · bibliography: 278 · meta: 34 · transcripts: 25 · discord_guide: 79

- Established c3po as the canonical enrichment source for all three content types in protocolized-website's resource library. PDF and YouTube enrichment is daemon-driven: `daemon.py` checks the mtime of `sources/{type}/enriched_meta.json` each cycle; if changed (i.e. a human ran an ingest script locally), it runs the corresponding sync script in protocolized-website and pushes the repo. Substack enrichment is GH Actions-driven: c3po's daily `sync-substack.yml` workflow now chains to check out protocolized-website, run `sync-substack-resources.py`, and push enriched resource Markdown. A new `sync-resources-d1.yml` workflow in protocolized-website fires on any push to `src/content/resources/` and runs `migrate-to-d1.py --remote`, closing the D1 update loop without requiring the daemon to hold CF credentials.

- protocolized-website's `sync-substack.py` was creating unenriched RSS-based resource Markdown stubs that conflicted with c3po's enriched stubs — both workflows ran at 08:00 UTC with no coordination, and whichever landed last won. The fix strips all Markdown creation from `sync-substack.py`, leaving it exclusively responsible for the D1 posts table and R2 body/image mirror (the magazine layer). State is now tracked in a committed `.substack-sync-state.json` keyed by slug, independent of whether any Markdown file exists. Resource Markdown for Substack posts is now exclusively written by c3po's GH Actions chain. 135 existing slugs bootstrapped into the state file on deploy.

- All three protocolized-website sync scripts (`sync-pdf-resources.py`, `sync-youtube-resources.py`, `sync-substack-resources.py`) previously hardcoded `C3PO_ROOT = REPO_ROOT.parent / 'c3po'`, which only works locally where the two repos are siblings on disk. Added `C3PO_ROOT = Path(os.environ.get('C3PO_ROOT', str(REPO_ROOT.parent / 'c3po')))` so GH Actions can pass the actual checkout path via env var, enabling the scripts to run in a cloud runner where c3po is checked out as a subdirectory.

- New script `ingest/draft_resource.py` writes resource Markdown stubs to protocolized-website for PDFs in `enriched_meta.json` that have no matching resource file yet. Pre-fills description (Haiku summary) and tags (mapped categories); leaves date, authors, and file URL for human completion. Closes the gap in the new-PDF intake flow: previously the resource Markdown had to be created manually after enrichment.

- Cross-repo push from c3po's workflow to protocolized-website requires a token with write access. Deploy keys are disabled at the Protocol-Institute org level; fine-grained PAT creation requires browser MFA and has no CLI path. Used the existing gh CLI OAuth token as a short-term solution, registered in `admin/keys.md` with a flag to rotate to a scoped fine-grained PAT. `CLOUDFLARE_API_TOKEN` (PI org CF token) also added to c3po GH Actions secrets for the D1 fallback query in `sync-substack-resources.py`.

---

## Session 35: GHA Fix, 6 Missing YouTube Videos, PR #4 D1 Migration

*2026-06-16*

**Tracks:** corpus-ingestion, operations

**Session costs:** haiku_enrichment: $0.03 · voyage_embedding: $0.02

**Vectors upserted:** videos: 3,127 · substack: 1,106 · total: 27,089

- **GHA Substack sync failure — fixed:** `sync-substack-resources.py` used a backslash-escaped quote inside an f-string expression (`'\"A post...\"'`). Python 3.12+ allows backslashes in f-string expressions; the CI runner (3.11) does not. Fix: replaced the literal with `yaml_str('A post from the Protocolized magazine.')` — identical output, no backslash in the expression. Manual run confirmed clean; `jamverse-jam` ingested.

- **Why 6 YouTube videos were missing:** `fetch_youtube_meta.py` discovers videos solely by walking the 10 hardcoded playlist IDs in `PLAYLISTS`. Videos not assigned to any of those playlists on YouTube are invisible to the ingest pipeline — regardless of how visible they are on the channel. The 6 missing videos were either pre-playlist SoP-era standalones (2023 office hours and town hall) or guest talks published to YouTube but never added to a tracked playlist. `protocolized-website` had them as manually-created resource stubs; c3po had never seen them.

- **6 missing videos ingested:** Fixed the `--video` flag to bootstrap a stub entry for playlist-orphaned videos, then fetched captions via yt-dlp, Haiku-enriched (summary, categories, speakers, key concepts), and upserted to the `videos` Pinecone namespace. Videos: *Atoms, Institutions, Blockchains* (Josh Stark); *Punk, Folk, Myth, Protocols* (Sam Chua); *Scaling Bitcoin: The Rise of the Lightning Network* (Lisa Nifut); *Seeing SCP as a Narrative Protocol* (Simon Dea Rir); *Summer of Protocols Office Hours 0*; *Summer of Protocols Town Hall*. videos: 2,940 → 3,127 (+187; 91 → 97 videos). Enriched Markdown synced to protocolized-website; R2 thumbnails uploaded; D1 re-migrated.

- **PR #4 merged + D1 column drop:** Merged the `enriched_categories` column removal from protocolized-website's D1 schema (was always written as `[]`, never read). Ran the live one-time migration: `ALTER TABLE posts DROP COLUMN enriched_categories;` against the remote `protocolized-resources` database. Clean — 0 errors.

---

## Session 36: Intro Quality System + SIG Meeting Capture Design

*2026-06-17 · 11:00–14:56*

**Tracks:** ux, operations, corpus-ingestion

**Vectors upserted:** total: 27,089

- **Root cause of intro response mismatches:** The intro handler's title-matching loop excluded VGR-authored sources before checking whether Claude had mentioned them. So when Claude's answer named a VGR paper (USoP was the most common case), the exclusion fired, the title match failed silently, and the fallback picked a random high-ranked source unrelated to what Claude actually said. Additionally, the Retrospectus — a yearly index document with no `is_cover_letter` flag — was appearing as a fallback source despite being unsuitable as a recommendation.

- **Intro handler rewrite:** Added `_is_excluded_from_intro()` — a single function that filters VGR-authored papers, cover letters, devlog sources, the Retrospectus by title, and definition entries without URLs. Added `_find_mentioned_source()` — scans all retrieved sources for the one whose title best matches the answer text, using exact substring then fuzzy word-overlap (≥60% of 4-char+ words). The corpus query now explicitly asks Claude to prefer non-VGR resources. The suggested reading list allows up to 3 links: the title-matched source first (always mentioned in the response text), plus up to 2 bonus sources from the valid pool.

- **Per-response quality monitoring:** Added `bin/intro_quality.py`: called by the intro handler after every response, before posting. Runs 6 checks — *title_mismatch* (primary link not found in answer text), *no_title_match* (source chosen by rank, not title), *usp_in_answer* (USoP over-referenced), *vgr_paper_mentioned* (VGR paper named in answer), *short_answer* (under 80 chars), *no_url_removed* (auto-fix: drops no-URL sources). Simple problems auto-fix before posting; others are logged. Log at `data/intro_quality_log.jsonl`, one entry per notable response. Added `bin/review_intro_quality.py` for session-start review. Updated `CLAUDE.md` startup ritual to include this step.

- **SIGFPT Roam graph inspection and ingest plan:** Inspected a 165-page Roam graph export of the SIGFPT working graph (1.7MB). Classified content: 14 rich meeting topic pages (AI summaries, vocabulary, participant discussions, reading lists), 3 raw transcripts (too noisy to ingest directly), 52 empty daily stubs, ~8 Protocol Foundations Workshop pages, ~6 standalone concept pages. The SIG master page maps each meeting date to a Roam page title and, for earlier sessions, a Discord thread ID. Plan written to `plans/roam-ingest.md`: ~56 vectors to the `sig` namespace via a new `ingest/sync_roam.py` script, plus `data/roam_enrichments.json` for website enrichment (reading lists, vocabulary, participants per meeting page).

- **Designed-for-ingestion SIG meeting capture protocol:** Decided to deprecate Roam as the SIGFPT capture format. The new design separates capture (human-provided, minimal) from enrichment (c3po-provided, AI-driven). Facilitators commit a YAML-frontmatter Markdown file per meeting to a `Protocol-Institute/sig-notes` repo — required fields: sig, date, topic; optional but valuable: participants, reading list, transcript file. A new `ingest/process_sig_meeting.py` pipeline takes the notes file plus raw transcript, queries Pinecone for related corpus context, and feeds everything to Claude to produce a structured enrichment record (summary, vocabulary, corpus links). The enriched version — not the raw transcript — goes to Pinecone. The same template and pipeline work for all six PI SIGs, with per-SIG namespace query weights as the only configuration difference. Plan at `plans/sig-meeting-capture.md`; pending discussion with SIG hosts.

---

## Session 37: Cost Tracking + New Nature Ingest

*2026-06-24*

**Tracks:** operations, corpus-ingestion

**Vectors upserted:** pdfs: 765 · total: 27,389

- **Anthropic API cost tracking:** Added `ingest/cost_logger.py` — a shared utility that appends one JSON record per Claude API call to `data/cost_log.jsonl`. Each record captures timestamp, script name, model, input/output tokens, and computed cost in USD. Pricing table covers Haiku 4.5 ($1.00/$5.00 per MTok), Sonnet 4.6 ($3.00/$15.00), and Opus 4.8 ($5.00/$25.00). Instrumented the four daemon scripts that call Claude: `enrich_discord_links` (dominant recurring cost — up to 200 Haiku calls per cycle), `rebuild_sig_summaries`, `sync_discord_channels`, and `sync_sig`. Added `bin/cost_report.py` which reads the log and prints last-N-days and all-time totals with per-script breakdown. Added the report as step 5 of the `CLAUDE.md` startup ritual. The log file will begin accumulating on the next daemon cycle.

- **New Nature special feature ingest:** Ingested two items from the June 17, 2026 New Nature talk into the `pdfs` Pinecone namespace. Source files were in `protocolized-website/inbox/.processed/new-nature/`: an HTML essay (`index.html`, 19KB of clean text) and a 28-page PDF slide deck (`slides.pdf`). Created `ingest/ingest_new_nature.py` — reads HTML via BeautifulSoup, reads PDF via pdfplumber, chunks both using the existing `chunk_text` utility, embeds via Voyage AI, and upserts body chunks + a doc_summary vector for each item. Enrichment records with hand-written summaries and categories added to `sources/pdfs/enriched_meta.json`. 15 vectors total: essay (9 body + 1 summary), slides (4 body + 1 summary). pdfs: 750 → 765.

---

## Session 38: Cost Monitoring Dashboard + Pinecone Read-Unit Limit

*2026-06-25*

**Tracks:** operations

**Vectors upserted:** total: 27,420

- **Discord request cost tracking in Worker:** Added `trackDiscordRequest()` to `api/worker.js` — accumulates Discord bot usage in two new KV keys: `stats:discord:day:YYYY-MM-DD` (daily rolling count) and `stats:discord:lifetime`. The `/stats` endpoint now returns `discord_day` and `discord_lifetime` alongside the existing MCP tracking fields, giving a parallel view of Discord vs. web vs. MCP usage.

- **Cost section in monitoring page:** Added `cost_section()` to `ingest/generate_monitoring_page.py`. Fetches live usage from `/stats`, reads `data/cost_log.jsonl` for instrumented Anthropic API calls, and reads the bot session log. Renders a four-row cost table: Web UI (c3po_web), Discord bot (c3po_bot via Worker), Ingest pipeline (c3po_listener), and Cloudflare infrastructure. Tracked actuals are shown alongside pre-tracking estimates for historical context. Now writes to both `c3po/monitoring.html` and `website/monitoring.html`.

- **Pinecone free-tier read-unit limit incident:** The PI org Pinecone account hit the 1M read-unit/month limit on the free tier. Symptoms: all Worker RAG queries silently returned empty sources, so the bot answered from system prompt only. Root cause was high daemon query volume (every 30-minute cycle across 11 namespaces) plus dev testing earlier in the month exhausting the budget. Fix: upgrade Pinecone plan. Limit resets monthly; the next cycle will restore normal retrieval.

---

## Session 39: /status Page — Artifact Counts and Origin Breakdown

*2026-06-28*

**Tracks:** operations, worker-api

**Vectors upserted:** total: 27,518

- **Artifact counts and origin breakdown on /status:** The corpus status page at `/status` previously showed only vector counts per namespace. Extended `ingest/publish_dashboard.py` with three new building blocks: `artifact_counts()` reads local state files to derive the number of discrete artifacts per namespace (97 talks, 123 posts, 74 papers, 914 terms, 1,252 links, 252 references, 103 meetings, 10 channels, 80 channels described); `NAMESPACE_TIERS` classifies each namespace as Protocol Institute, Community, Third-party, or System; and `build_breakdown()` aggregates those into four summary buckets with total vectors and artifact item lists. The Worker's `renderStatusPage()` was updated to render a new "By Origin" card grid at the top of the page — four cards each showing total vectors, a label, and a bulleted artifact summary — and the namespace table gained Artifacts and Origin columns with color-coded tier badges. PI-published content (5,573 vectors) is green, Community (11,539 vectors) is blue, Third-party (10,341 vectors) is amber, and System metadata (65 vectors) is gray.

---

## Session 40: Security Audit + /share Transcript Bug Fix

*2026-07-08*

**Tracks:** operations, worker-api

**Vectors upserted:** total: 28,193

- **Security audit — D1 input validation:** Audited `api/worker.js` for the beacon-endpoint SQL injection pattern found in the parent vgr_zirp project (probe strings accumulating in D1 tables due to missing categorical whitelists). Finding: c3po's Worker has no D1 binding at all — `[[d1_databases]]` is commented out in `wrangler.toml`; all state goes to KV. KV categorical fields are properly guarded: `shareMode` uses an `Array.includes()` whitelist, `status` is internally computed or whitelist-validated on admin PATCH, `rating` is range-clamped. No injection surface found.

- **Bug fix — `/share` Pinecone upsert:** The real-time Pinecone transcript upsert in `handleShare()` referenced `rand` (line 970), which is only defined inside `logQuery()`'s closure. Every `POST /share` call was silently throwing `ReferenceError: rand is not defined` inside the `try/catch`, so web-UI submissions were never indexed to the `transcripts` namespace in real time. Fixed by replacing `rand` with `chatId`, which is already defined in `handleShare` and serves as the unique submission identifier. KV writes were unaffected (they happen before the failing code), so no submissions were lost — `sync_web_chats.py` was still picking them up on the next daemon cycle. Deployed: `4ec05ced`.

- **Substack sync:** Ingested *A Visitor's Guide to the Disposition* — 14 vectors upserted to the `substack` namespace (1,135 total).

---

## Session 41: Audio Meeting Notes Pipeline

*2026-07-08*

**Tracks:** corpus-ingestion, operations, worker-api

**Vectors upserted:** total: 28,199 · sig: 5,991

- **New ingest source — #meeting-notes:** OpenRecapper-PI is a bot that records audio from PI voice channels and posts structured transcription output to `#meeting-notes`. Each session produces a header message with permanent R2 storage URLs (not ephemeral Discord CDN) linking to `summary.md`, `transcript.txt`, and `transcript.srt`, followed by AI-structured section messages. Sessions cover SIG calls (identified by sig code in title, e.g., "sigpsy 02July26") and ad hoc working sessions. Channel entry added to `config/discord_channels.json` as a Bot Feed source.

- **`ingest/sync_meeting_notes.py` (new):** Scans `#meeting-notes` via Discord REST API, identifies header messages by the 📝 prefix and R2 URL format, fetches `summary.md` from R2 (permanent URLs only — not Discord CDN), and parses structured sections (Overview, Key Points, Questions, Action Items, Context, Tools, Reading). Chunks into two vector types per meeting: `audio_meeting_summary` (overview + key points combined) and one `audio_meeting_section` per section. Embeds via Voyage AI and upserts to the `sig` namespace. For SIG meetings, also creates or enriches the meeting JSON in `data/sigs/meetings/` with `audio_*` fields (summary, key_points, participants, reading, questions, duration, r2_summary_url). For ad hoc meetings, upserts to Pinecone only. Initial run ingested 16 vectors: Jul 01 ad hoc (6 vectors), Jul 02 SIGPSY (5), Jul 08 ad hoc (5). The Jul 02 SIGPSY meeting had no existing Discord thread, so the script created `data/sigs/meetings/audio_1522286708635340840.json` from the audio summary alone.

- **Worker — `audio_meeting_summary` and `audio_meeting_section` support:** Extended `normalizeSig()` to recognize both new chunk types. Added `isAudioSummary`, `isAudioSection`, and `isAudio` boolean flags to the normalized result. Merge weights: audio summaries 0.92× (highest of all sig content, reflecting richer signal from structured AI synthesis of the actual audio), audio sections 0.85×. Context block type labels: `AUDIO SUMMARY` / `AUDIO RECORDING`. URL resolution uses the R2 URL from metadata. Title resolution reads `m.meeting_title` for audio chunks (different metadata key than Discord thread chunks). Added SIGPSY and DRG to the `SIG_NAMES` constant (previously missing). Deployed: `62263606`.

- **Website — meeting detail pages with audio content:** Extended `ingest/update_sig_pages.py`'s `render_detail_page()` to render a "Session Recording Summary" section when `audio_summary` or `audio_key_points` fields are present in the meeting JSON. The block shows the reading source, overview paragraphs, key points list, questions excerpt, participants, duration, and a link to the full R2 notes. This is additive alongside the existing Discord thread summary. Nine new meeting detail pages were created and pushed to the website repo: DRG (Jun 25), MRG (Jun 18, Jul 02), ProtFiSIG (Jun 25), SIGFPT (Jul 10), SIGPfB (Jun 15, Jun 22), SIGPSY (Jun 18, Jul 02 — the Jul 02 page includes the audio summary block). Six index pages updated with links to the new detail pages.

- **Daemon integration:** `bin/daemon.py` updated to include two new steps: `sync_meeting_notes` (after `sync_sig`, so audio summaries are ingested before summaries are rebuilt) and `update_sig_pages` (after `rebuild_sig_summaries`, before `generate_sig_pages`, so audio-enriched meetings get detail pages created before the index pages are regenerated and pushed). The daemon's website push step now picks up both new detail pages and enriched index pages.

---

## Session 42: SIG Meeting-Publishing Fixes + Website PR Deconfliction + exe.dev Plan

*2026-07-08 · 11:00–12:32*

**Tracks:** corpus-ingestion, operations, worker-api

**Vectors upserted:** total: 28,208 · sig: 5,998

- **Meeting-detection regex bug:** `data/channel_manifest.json`'s SIGPSY and DRG `meeting_patterns` required an exact 3-letter month abbreviation immediately followed by digits (`\d{1,2}[A-Za-z]{3}\d{2,4}`), so a thread titled “SIGPSY 2July26: Three Temporalities” (full month spelling) silently fell through to discussion classification instead of meeting — the reason that meeting never appeared on the site. Fixed to accept the month stem plus any trailing letters.

- **Meetings created ahead of the session were being published as if already complete:** `sync_sig.py` classified a thread as a finished meeting purely by name pattern, with no date awareness. A SIGFPT thread created for a July 10 session (still in the future) and a DRG thread for July 9 had already been auto-summarized with fabricated past-tense text describing sessions that hadn't happened yet. Added shared `meeting_ready()` / `MEETING_GRACE_DAYS = 7` (`ingest/utils.py`), applied at three layers — ingestion (`sync_sig.py`), JSON-building (`rebuild_sig_summaries.py`), and detail-page/link creation (`update_sig_pages.py`) — so a thread isn't treated as a completed meeting until 7 days past its date. A pending thread is rechecked every daemon cycle regardless of message-count changes, so it still promotes correctly even with no further Discord activity. Cleaned up the two premature records' Pinecone vectors and JSON files.

- **Full-page regeneration was clobbering detail-page links:** `update_sig_pages.py` adds a link from each meeting title to its detail page; `generate_sig_pages.py` runs immediately after (daemon step 10) to regenerate the whole SIG index page, but had no awareness those links existed and rebuilt every meeting title as plain text — undoing the previous step's work every single cycle. This had apparently been happening for a long time: nearly every meeting-title link across all 6 SIG pages was missing. Fixed by having the regenerator check for an existing detail-page directory and re-derive the link. Also fixed a related regression where the same regeneration reverted a manually-applied “Session livestream (YouTube)” link label back to a bare domain — both generator scripts now special-case YouTube links.

- **Website changes now go through a PR, not a direct push:** A daemon cycle's automated SIG-page regeneration collided with manual presentation/formatting work happening in parallel on the website side, and a direct push would have overwritten it with no review step. `bin/daemon.py`'s `push_website_if_changed()` now rebuilds a dedicated `c3po/auto-sig-pages` branch from `main` and opens (or silently updates) a PR via `gh` instead of committing straight to `main`. Filed [Protocol-Institute/website#5](https://github.com/Protocol-Institute/website/pull/5) as the one-off correction PR, with a full writeup of what it fixes and why. Also batched the check to run at most once every 7 days (new `data/website_push_state.json`) instead of every 30-minute cycle, so an open PR doesn't get bumped constantly.

- **Planned persistent-process migration to exe.dev:** Wrote `plans/exe-dev-migration.md`, moving `bin/daemon.py` and `bin/c3po_bot.py` to an existing exe.dev VM (root SSH, persistent Debian/Ubuntu, systemd + apt), replacing the never-executed Hetzner plan (`plans/vps-migration.md`, now marked superseded). Scope is deliberately narrow: daemon + Discord bot only, humboldt deferred; Claude Code installed for interactive SSH maintenance sessions only, no scheduled/autonomous jobs. Five open questions logged for VGR before execution: VM SSH access, root vs. non-root user, GitHub auth strategy for the VM, Claude Code auth mode, and directory layout. Also generated the repo's first `requirements.txt` (never existed — the laptop `.venv` had grown ad hoc across many sessions) via `pip freeze`, a prerequisite for cloning anywhere new.

---

## Session 43: Repo Made Public + Session-Start Audit

*2026-07-23 · 11:00–11:45*

**Tracks:** operations

**Vectors upserted:** total: 28,982 · sig: 6,281

- **Repo visibility flipped private → public:** Checked the full git history for accidentally committed secrets (`.env`, key/secret/credential/token filenames) first — clean, nothing ever committed. No code changes.

- **All 5 unreviewed intro-quality issues (Jul 12–18) share one failure mode:** the primary recommended link falls back to rank-order selection because `_find_mentioned_source()` can't find the chosen source's title in the answer text. Two of the five land on a low-signal resource as a result. Flagged for a dedicated look next session — not yet fixed or marked reviewed.

---

## Session 44: Worker Reconnect-Loop Fix + Pinecone Quota Root Cause

*2026-07-24 · 08:00–09:50*

**Tracks:** worker-api, operations

**Vectors upserted:** total: 28,982

- **Traced a sustained ~17,500 req/hr spike on the c3po Worker** to a single fixed IP in a tight reconnect loop against `GET /mcp` using the legacy SSE transport (`Accept: text/event-stream`) — confirmed via `wrangler tail` across 106 sampled requests, all identical, zero errors, never touching any cost-bearing endpoint. Not a distributed attack. Root cause: the endpoint returned `200` with a plain-text banner instead of `405`, so a spec-compliant-but-naive MCP client read the non-stream response as a dropped connection and retried with no backoff.

- `GET /mcp` now returns `405 Method Not Allowed` instead of `200`, telling SSE-transport clients not to retry. Deployed and verified live — the offending client's request rate dropped from ~2/sec to zero within a minute, confirmed silent in two follow-up `wrangler tail` checks. `POST /mcp` (the real JSON-RPC path) unaffected.

- Reviewed and merged c3po#1: `bin/daemon.py`'s website-push flow had a stale `sigs.html` pathspec that silently broke the weekly SIG-page PR flow on every attempt since 2026-07-09. Verified the log evidence and current website-repo state before merging; cleaned up 3 orphaned `git stash` entries the bug had left behind.

- c3po's own ingest scripts were confirmed already incremental and not the driver. The actual cause: humboldt's `ingest_all()` re-embeds and re-upserts its **entire** corpus (5,142 chunks) on every new notebook entry — roughly daily — with no content-hash check. `PINECONE_API_KEY` is shared account-wide between c3po and humboldt, so humboldt's write volume exhausted the shared monthly cap and blocked c3po's own (much smaller) writes too. Opened humboldt#1 with an incremental fix, verified locally against the live corpus, left for that project to merge.

---

## Session 45: Ingestion Pause/Resume + Live Retrieval-Failure Fix

*2026-07-24 · 10:00–10:50*

**Tracks:** operations, worker-api

**Vectors upserted:** total: 28,982

- Every write-touching ingest script already gets its Pinecone client from one shared helper (`utils.get_pinecone_index()`) — wrapped it in a guard that blocks writes/reads while paused and auto-pauses itself the moment Pinecone returns a write- or read-unit 429, so one script hitting the cap stops every other script from independently rediscovering (and paying Voyage/Anthropic cost for) the same doomed call. `bin/daemon.py` now skips invoking paused steps entirely — verified with a monkeypatched dry-run before restarting the live daemon. Resume needs no special backfill logic: every script's state file only updates on success, so nothing gets marked processed while paused — the next successful run just picks up everything deferred.

- While testing, `rebuild_sig_summaries.py` 429'd on a plain read — c3po's read-unit quota (1,000,000/month, same shared account as the write-unit issue) was independently exhausted too. Activated both a write and a read pause via the new `ingest/ingestion_control.py` CLI, until 2026-08-01.

- Confirmed via `wrangler tail` that every Pinecone query in the live `/query` pipeline was 429'ing — and the Worker was catching each failure, returning empty matches, and still generating a fluent, confident answer with `sources: []` and no indication anything was wrong. Fixed: all 4 query paths (`/query`, `/search`, MCP `search_corpus`, MCP `ask_c3po`) now detect a fully-failed retrieval and either prepend an honest degraded notice to the answer or return a `degraded` flag. Verified live on all 4 paths post-deploy.

- `GET /search` was throwing a 500 on every call — its `mergeResults()` call was missing one argument (10 passed, 11-param signature), silently shifting `MAX_SOURCES` into the transcript-items slot. This is humboldt's fallback retrieval path (`query_c3po_worker()`), so it had been silently broken there too. Fixed and verified.

---

## Session 46: Meeting-Notes Crash Fix, Intro-Quality Diagnostic Logging, exe.dev Migration + protocolized-website CVE Fix

*2026-08-01 · 11:00–14:35*

**Tracks:** operations, corpus-ingestion, ux, infra

**Vectors upserted:** total: 29,852

- Both write and read pauses set in session 45 lifted at the 2026-08-01 UTC reset (`ingest/ingestion_control.py status`). Vector count grew normally within the first hour, confirming writes are genuinely flowing again, not just the quota check passing.

- `sync_meeting_notes` was failing every cycle on a Pinecone `400` (`null` metadata for `sig_display`). Root cause: the meeting-bot’s title template quietly switched to a hyphenated `SIG-&lt;CODE&gt;` format (`SIG-FPT`, `SIG-MRG`, `SIG-P4B`, etc.), which didn’t match any prefix in `sync_meeting_notes.py`’s separate `SIG_TITLE_MAP` — an unmatched title left `sig_display=None`, and Pinecone rejects `null` metadata outright, killing the whole subprocess before later recordings in the same cycle could run. Fixed by normalizing hyphens out before matching (so future template drift degrades to a warning instead of a crash) and syncing the missing `SIGPfB` aliases already known to `channel_manifest.json`’s meeting-detection regexes. Verified live — the daemon picked up the fix mid-session and completed a clean 16/16 cycle.

- Sent a synthetic new-member introduction through the production `/query` endpoint using the intro handler’s exact prompt. Claude named the right resource but paraphrased its formal title into Discord voice (“**SIGFPT Stigmergy session** (May 2026)” for a source titled “SIGFPT: Stigmergy Part II - Real-World Applications and Framework Analysis”) — which legitimately falls below `_find_mentioned_source()`’s word-overlap threshold even though Claude clearly meant that source. The rank-order fallback happened to pick the same resource anyway in this repro, suggesting the impact is often cosmetic rather than a wrong link — but that couldn’t be confirmed for the historical flagged entries because the quality log never recorded the actual answer text, only the primary title and issue codes. That’s exactly why four sessions in a row could flag the pattern but never resolve it.

- `intro_quality.log_quality()` now stores the answer text (600 chars) and the primary source’s type/SIG, so the next occurrence can be diagnosed with evidence instead of re-guessed. Deliberately left the matching heuristic itself untouched — didn’t want to tune logic feeding a live public bot’s auto-fix behavior without real before/after data to validate against. Restarted `c3po_bot` to pick up the change; no user-facing behavior change.

- `bin/daemon.py` and `bin/c3po_bot.py` now run on `c3po-vm.exe.xyz` under systemd (`Restart=always`), not launchd on Venkat’s laptop — routine ingestion, Discord presence, and the website PR pipeline no longer depend on a laptop session ever running. Built two small pieces of prep first: a cost circuit breaker (`check_budget()` raises once today’s logged Anthropic spend hits a daily cap, mirroring Humboldt’s pattern) and a daemon self-commit step (pulls at the start of each cycle, commits+pushes its own routine state-file churn under a `[daemon]`-prefixed message at the end) — both tested locally before trusting them unsupervised, both verified live on the VM’s first real cycle.

- A fresh git clone doesn’t include anything `.gitignore`’d, which turned out to include `data/sigs/meetings/` — missing it made `rebuild_sig_summaries.py` silently re-call Haiku for all 117 meetings instead of reading its local cache, caught live via `cost_log.jsonl` growing every ~8s rather than any error message. And `sync_youtube_resources` (a pre-existing daemon step) turned out to need `wrangler` + a Cloudflare token for a D1 fallback lookup — same dependency shape as the Substack workflow’s resource sync, which is why that workflow was deliberately left on GitHub Actions rather than absorbed into the daemon as originally planned: it already runs independent of the laptop, so absorbing it would have added a real dependency chain (Node, npm, a new secret) for no laptop-unblocking benefit.

- Installing `protocolized-website/worker`’s dependencies fresh on the VM surfaced 8 `npm audit` findings the laptop’s older `node_modules` hadn’t re-flagged recently. Read each advisory rather than blanket-patching: all 8 traced to `hono` (the only flagged package actually in `dependencies`), and of those, the two that could plausibly apply to a live Cloudflare Worker — CORS middleware credential/wildcard reflection, and `hono/jsx` cross-request context leakage — don’t, confirmed by grepping the actual source for the specific APIs each bug requires (no CORS middleware import; no `createContext`/`useContext`/`jsxRenderer`/`useRequestContext` anywhere). Bumped `hono` to `4.12.33` anyway since it was free — already within the declared semver range, verified with a clean `tsc --noEmit` and `wrangler deploy --dry-run`. The remaining 7 findings are wrangler/tailwindcss dev-toolchain only, never shipped to the deployed Worker; fixing those needs a breaking `wrangler` bump, left for a separate PR. Opened [protocolized-website#5](https://github.com/Protocol-Institute/protocolized-website/pull/5) rather than pushing to main.

---

## Session 47: Discord Bot Bug Fixes + discord_guide Embedding Scope Redesign

*2026-08-04 · 11:15–14:00*

**Tracks:** discord-bot, operations, corpus-ingestion

**Vectors upserted:** total: 30,095

- `NEW_MEMBER_DAYS` was 60, so members who’d joined up to two months earlier still got the “Welcome, new member!” treatment if they posted in `#introductions` — narrowed to 7 days. Separately, normal `@mention` query replies could show a source with a rendered-empty link (e.g. a lexicon entry with no URL), because `send_answer()` lacked the `url` guard `handle_introduction()` already had — added the same guard so linkless sources are filtered out before the top-3 slice. Verified live same day: the empty-links fix confirmed working immediately; the intro-window fix needs real member activity over a few weeks to verify naturally, logged as an open-monitoring item rather than a closed TODO.

- `journalctl -u c3po-daemon` since 2026-08-01 showed 134 consecutive sync cycles, all 16/16 steps OK, zero errors, over ~3 days unattended. `exe.dev billing usage` showed the VM comfortably within its shared-pool allowance (~0.0/2 vCPU avg, 10.8/100GB disk).

- The `discord_guide` Pinecone namespace held 80 entries because the embed step only checked Discord-guild presence, not whether a channel was actually useful for long-term conversational memory. Wrote `plans/discord-guide-scope.md` first — an explicit embed/do-not-embed policy plus a guiding principle for anything auto-discovered later (embed if useful for long-term discourse memory, exclude if it’s transient administrative noise) — then implemented it: a new `should_embed()` excludes MOD, Server Link Feed, and `#introductions`/`#bugs-and-tests`/`#announcements`; archived-read-only channels (49 of the 80) now embed once and then freeze permanently instead of being re-checked every cycle. Found and fixed a latent bug along the way: `last_embedded_hash` was never carried forward between cycles, so the daemon had been silently re-embedding all 80 channels on *every single cycle* since the script existed. Purged 7 vectors embedded under the old scope that are now excluded — `discord_guide`: 80 → 73.

- The new-member intro check had zero content gate and no per-user dedup, so it fired on every top-level message from a member inside the new-member window — including a new member’s casual reply to someone else’s greeting, mistaken for a fresh self-introduction. Added a per-user dedup (`wq.is_welcomed()`/`mark_welcomed()`, persisted in `data/welcome_queue.json`) so a member gets the welcome flow at most once, plus a 20-character content floor so one-word replies can’t trigger it in the first place. Both skip conditions now return “handled, don’t retry” rather than “failed,” which also fixed a related bug where a gate-rejected queued message would’ve retried on every future drain cycle indefinitely.

---

## Session 48: Pinecone Egress Quota Exhaustion — Diagnosis + Incremental-Read Fixes

*2026-08-13 · 11:30–14:20*

**Tracks:** operations, corpus-ingestion

**Vectors upserted:** total: 30,616

- Started from a report that the deployed web bot “seemed to have obsolete rate-limit messaging and was capping responses.” Reproduced live: every `/query` on `c3po.protocolized.io` returned `degraded: true`, empty sources, and a generic quota disclaimer prepended to an ungrounded answer. Traced it to a genuine, current Pinecone 429 via a raw REST call that bypassed all of c3po's own code &mdash; `“You’ve reached your egress limit for the current month (1000000000 bytes)”`. Confirmed with the user (who’d checked Pinecone’s console and saw read units, write units, and storage all under 65%, nothing breached) that **egress is a fourth, independent monthly quota** &mdash; 1GB/month on the Free plan, not surfaced next to RU/WU/storage on the main quotas page. First hit 2026-08-10, invisible for three days because no session had logged in since 2026-08-04.

- `ingest/fetch_discord_links.py`'s `harvest_urls_from_namespace()` was doing a full `idx.list()` + `idx.fetch()` &mdash; complete embedding values plus metadata &mdash; over every vector in the `discord`+`sig` namespaces (12,873 combined) on every cycle, with no caching, just to read a small `urls` metadata field. The same shape as the humboldt full-re-embed incident from July, except this time a full-corpus *read* in c3po's own code. Fixed to persist already-harvested vector IDs per namespace and fetch only newly-added ones.

- `_GuardedIndex` in `ingest/utils.py` (built in July for the write-unit/read-unit incidents) auto-pauses ingestion when a Pinecone 429 matches a known regex &mdash; but it only knew `“write unit limit”` and `“read unit limit”`, not `“egress limit”`, so the daemon retried blind for three-plus days instead of self-pausing like it had before. Generalized to one regex matching Pinecone's general `“reached your &lt;name&gt; limit”` phrasing, so any not-yet-seen quota dimension self-pauses correctly going forward.

- After estimating that even the fixed `fetch_discord_links.py`'s remaining `idx.list()` call &mdash; ID-only, but still run against the full discord+sig namespaces every cycle &mdash; could on its own approach the entire 1GB/month egress budget, added a `describe_index_stats()` precheck (a control-plane call, confirmed exempt from the egress quota) to skip listing a namespace entirely when its vector count hasn't moved since last run. Separately, `rebuild_sig_summaries.py` was fetching full vector data for all ~124 meeting-summary vectors unconditionally, before its own already-built-locally check was even applied &mdash; since the meeting `thread_id` is embedded directly in the vector ID, it can now check the local JSON cache first and only fetch meetings it hasn't built yet. Swept the rest of the repo for the same pattern; everything else turned out to be one-off manual scripts or normal per-interaction query traffic, not recurring full-scans.

- Found direct log evidence that `c3po_bot.py`'s own separate direct-Pinecone query (for Discord onboarding/nav help) is hitting the same exhausted egress quota &mdash; `Guide task failed: [429] ...egress limit...` logged on both 2026-08-11 and 2026-08-12, caught and swallowed so the bot continues without that recommendation instead of crashing. Same underlying mechanism as the web Worker's `degraded: true` fallback built in July: both channels keep talking, just without corpus grounding, while the quota stays exhausted.

---

## Session 49: Merged Daemon-Diagnosed PRs, Root-Caused the Query Fan-Out, Trimmed Egress Cost

*2026-08-17 · 11:30–13:10*

**Tracks:** operations, worker-api

**Vectors upserted:** total: 30,698

- Went looking for PRs against the website repo and found the real ones sitting against `Protocol-Institute/c3po` instead: **#2** dropped a gitignored `monitoring.html` from the website-push pathspec &mdash; `git add` on an explicitly-named ignored path is a hard error, not a silent skip, so every website-push attempt since 2026-08-07 had been aborting before it ever reached the SIG content that mattered. **#3** fixed a zero-width-lookahead regex bug in `_patch_meeting_archive` that had been leaking two blank lines into every SIG page on every daemon cycle since session 39, compounding to roughly 800 stray lines per page by the time it was caught. Verified both independently (traced the regex by hand, confirmed the gitignore state against the live website repo) before merging, then fast-forwarded both the laptop and VM clones and restarted the VM daemon.

- The read pause's `resume_at` is a code-side guess &mdash; “1st of next UTC month” &mdash; extrapolated from the write/read-unit quotas' confirmed reset behavior, but egress itself had never actually been observed resetting since it was only discovered as a separate quota in session 48. A raw REST call straight to Pinecone, bypassing all of c3po's own code, confirmed it's still exhausted: the same `“egress limit for the current month (1000000000 bytes)”` 429.

- Prompted by the observation that Discord/webchat traffic didn't look heavy enough to explain a blown 1GB/month allowance. Every web `/query`, MCP call, and Discord @mention or slash command turned out to fan out to **9–11 separate namespace queries** with full metadata &mdash; one user question is really about ten Pinecone reads. On top of that, humboldt (previously understood as just a quota-sharing peer with its own separate index) turned out to **query c3po's live index directly** as its primary retrieval path, fanning out across six of c3po's namespaces on every Discord reply it composes. Corrected the humboldt memory note, which had understated this.

- Cut the Worker's shared `TOP_K_EACH` constant from 8 to 5 across all four query-fan-out call sites and deployed live. Narrowed humboldt's per-reply namespace set from seven down to five (dropping `bibliography` and `discord_links`, rarely relevant to a live reply, while leaving its deliberate CLI research commands at full breadth), pushed, and restarted its daemon. A larger fix &mdash; short-TTL result caching for repeat queries &mdash; was scoped but deferred as a likely follow-up if this trim isn't enough on its own.

---

## Session 50: Closed the Egress Caching/Accounting Gap, Documented the Pattern for Other Bots, Fixed Stale Bibliography Links

*2026-09-01 · 11:30–12:40*

**Tracks:** operations, worker-api, corpus-ingestion, discord-bot

**Vectors upserted:** total: 31,724

- Session-start check verified the September reset directly against Pinecone's REST API (bypassing our own code), and confirmed the session 49 fan-out trims (`TOP_K_EACH` 8&rarr;5, humboldt's narrower namespace breadth) had held for the full 15-day window without re-exhausting.

- Both projects independently hit the same shared-account Pinecone quota problem. Found humboldt had already built &mdash; better &mdash; the two things c3po's own sessions 48&ndash;49 left as deferred TODOs: byte-level egress accounting and disk-based result caching, documented in `humboldt/plans/read-outage-2026-08.md`. Decided against extracting a shared package (two languages per project, no shared-package infrastructure between the repos, only two consumers today) in favor of a documented pattern: `admin/sop-pinecone-quota-management.md` (applies to any future bot on this account) and `plans/pinecone-quota-management.md` (c3po's own history and status against it).

- `queryNamespace()` now caches results in Cloudflare KV (6h TTL, keyed on a hash of namespace + top_k + filter + embedding vector) and every fan-out call site reports estimated egress bytes, aggregated once per request &mdash; not per namespace &mdash; to avoid a KV read-modify-write race across the 9&ndash;11 parallel namespace queries in a single interaction. Surfaced in `GET /stats` as `pinecone_egress`. Deployed (version `f14011da`) and verified against the live bot: normal answers, a real cache hit on a repeated question, and correct byte accumulation across a spaced-out test sequence.

- The intro flow's Reader fallback (`bin/c3po_bot.py`) pointed at a genuinely dead page &mdash; replaced with the live `protocolized.io/resources/protocol-reader-2025`. Investigating turned up a broader bug: `ingest/mine_bibliography.py` extracts a citing paper's own footnote URL with no check for whether the citation actually names a document c3po already hosts, so three bibliography entries (Capital Enclosure for Software Commons; Protocol Foundations 002 and 003) carried stale/foreign URLs for papers already sitting in our own 85-PDF corpus with correct `files.protocolized.io` links. Fixed all three source-file entries by hand (none was live in the `bibliography` namespace yet, so no re-ingest was needed) and closed the root cause &mdash; `merge_into_registry()` now cross-checks every extracted citation against the corpus we already own and rewrites self-citations to our own canonical URL.

---

## Session 51: SIG Attribution Repaired End to End: Audio Recordings, the Wedged Website Push, MCP Analytics, and a Stale Snapshot That Outranked the Truth

*2026-09-08 · 13:10–18:05*

**Tracks:** corpus-ingestion, operations, worker-api, website-integration

**Vectors upserted:** total: 32,151

- `SIG_TITLE_MAP` in `sync_meeting_notes.py` mixed bare keys (`drg`, `mrg`) with sig-prefixed ones (`sigfpt`, `sigpsy`), while the recorder posts every title as `SIG-&lt;GROUP&gt;`. `SIG-DRG` normalized to `sigdrg` and matched nothing, so those recordings were embedded with an empty `sig_display` and never attached to a meeting record. `match_sig()` now reduces a title to bare alphanumerics, tries it with and without a leading `sig`, and matches longest key first so a short key cannot shadow a longer one. 16 recordings re-embedded (96 vectors) with correct metadata.

- A recording arrives the day of the meeting; its Discord thread is summarised seven days later. Each wrote its own record, and the SIG page rendered the session twice (four such pairs existed). `attach_meeting_json()` now defers inside the grace window, and `rebuild_sig_summaries` absorbs a matching audio record when it writes the thread record. Deferring alone was not enough &mdash; it only moved the collision to day seven, which is why the absorb hook exists.

- A conflicted `git stash pop` left unmerged paths, and the old recovery (`git checkout main`) cannot run while those exist &mdash; so the clone sat parked on the auto branch and every later cycle failed identically. The caller stamped `last_push_check` regardless, hiding the failure for a week at a time. Now: `_recover_website_checkout()` aborts the merge and hard-resets, the flow returns `None` on failure so the clock is not advanced, and a pre-flight heals a clone wedged by an earlier run. Recovery must defer publishing to the next cycle: the reset also discards that cycle's regenerated pages, and the first live run shipped a PR with 12 detail pages and no index pages before that was fixed.

- The step that reads published meeting pages back off protocol-institute.org into the `sig` namespace as `chunk_type=sig_meeting_page` had never been wired in, though CLAUDE.md listed it among scripts that run automatically. Its state file was frozen at the last manual run: 96 pages, none for DRG. Added as step 7b; the backfill ingested 27 new pages and refreshed 91 stale ones. Note for future work: `daemon.py`'s own step list is loaded at process start, so changing it needs `systemctl restart c3po-daemon`, not just the per-cycle self-pull.

- Asked what meeting records exist for DRG, the bot answered &ldquo;0 archived sessions.&rdquo; The text was real: a community-shared link to `/sigs/drg/` had been fetched into `discord_links` on 2026-06-06 while that page was still a stub reading &ldquo;Meeting Archive &mdash; 0 sessions.&rdquo; Link snapshots are never refreshed. Six genuine DRG thread summaries were already indexed and lost to it, which is the more troubling half: the failure was not missing data but one authoritative-sounding stale chunk. `fetch_discord_links.py` now skips our own domains &mdash; a scraped copy of our own content goes stale by construction and duplicates the canonical ingestion. 43 stale own-domain vectors deleted.

- `trackMcpRequest()` fired only for an `ask_c3po` that reached Claude and returned, so `/stats` read &ldquo;1 lifetime request&rdquo; while the open `search_corpus` tool served ~130 calls a month &mdash; visible only as a side effect of the rate limiter's per-IP keys, mostly from Anthropic's egress range. `trackMcpCall()` now counts every request by method and tool:outcome, surfaced as `mcp_calls`, and `logQuery()` is wired into both tools. Counters are KV read-modify-write and undercount concurrent bursts; per-request event keys were rejected deliberately, since a reconnect-looping client once drove 17.5k req/hr and would blow the KV write quota.

- The grace window was enforced at four stages but not in `generate_sig_pages.py`, which rendered every record on disk &mdash; so sessions 1 and 4 days old reached the SIG index while their detail pages were correctly withheld. Separately, a meeting date is whatever the summarising model returns: a dropped digit published SIGPSY's 2026-08-13 session as 2023-08-13, which the grace window waved through because a 2023 date is comfortably old. `sanitize_meeting_date()` validates the model's date against the Discord thread's own snowflake. The first cut of the renderer gate would have been worse than the bug &mdash; `meeting_ready("unknown")` is False, so it held three long-published undated records and would have removed four live cards.

- New SIG hosted by Sarah Friend, reaching c3po only through audio recordings &mdash; it has no Discord channel. Added across the seven duplicated SIG registries in `ingest/*.py`; the duplication itself is unaddressed. Stub page merged as website PR #8.

---

## Session 52: Three Copies of “The” GitHub Token, All Different: Replacing the VM's Credential and Taking the API Off the Box

*2026-09-09 · 13:45–14:20*

**Tracks:** operations, infra, website-integration

**Vectors upserted:** total: 32,213

- **Filed as an incident the same morning, from a different project.** While designing Humboldt's VM credential model, its session found that `c3po-vm.exe.xyz` was authenticated with a GitHub token belonging to the account owner, carrying **admin and push on every repository it could reach**, readable by `exedev` — the user both c3po services run as, which has passwordless sudo. Surveying it here widened the finding: the token also had `admin:true, push:true` on `vgururao/venkateshrao.com`, so it was an *account*-wide token, personal repos included. It is a fine-grained PAT, which is exactly why it went unnoticed since 2026-08-01 — the token type reads as least-privilege; the scope selection is where the privilege actually lives. No evidence of misuse: this was exposure, not a breach. Two cheap questions closed early — `notes-ingest.exe.xyz` is clean (it already pushes with a per-repo deploy key, no `gh auth` at all — the good pattern predated the bad one on this account by a month), and the laptop authenticates with a separate keyring credential, so revocation could not disturb local work.

- **Deploy keys are disabled org-wide on Protocol-Institute.** The incident's remediation — replace the account token with per-repo deploy keys, the pattern Humboldt's plan uses — fails here: `POST /repos/{repo}/keys` returns `422 Deploy keys are disabled for this repository` for all three PI repos, while a personal repo still accepts them. Chosen instead: **one fine-grained PAT scoped to exactly `c3po`, `website` and `protocolized-website`, `Contents: write` and nothing else** — no admin, no other repos, no personal repos. Worth stating the trade honestly rather than glossing it: this is weaker than a deploy key in two specific ways — it is still an account-identity credential, and its scope can be widened later in the UI with no signal, which is precisely how the original exposure happened. That makes the standing assertions (phase 3) the control that keeps the choice safe over time, not optional garnish.

- **One API call was the entire justification for a GitHub credential on that box.** `bin/daemon.py` pushed three checkouts — all genuinely needing write — but also called `gh pr list` / `gh pr create` to open the weekly SIG-pages PR against the website repo. A contents-write token can push a branch; it cannot open a pull request. Rather than grant `Pull requests: write` back onto a machine whose two services exist to consume untrusted Discord input, the capability moved off the VM entirely: the website repo now opens its own PR from `.github/workflows/c3po-auto-pr.yml`, triggered by a push to `c3po/auto-sig-pages` and using that workflow's own `GITHUB_TOKEN`. Pushing to an already-open PR's branch updates it unaided, so the workflow only acts on the first push after each merge — the same condition the removed `gh pr list` was testing. **The VM now has no GitHub API access at all.**

- **The `GH_PAT` repo secret held the same account-wide token**, set 2026-08-01T20:33Z, one minute after the VM's `hosts.yml` was written. Revoking it failed `sync-substack.yml` on `Bad credentials` at the cross-repo checkout step. The error worth recording is the inference, not the outage: the value stored under `GH_PAT` in the key store hashes *differently* from the VM's token, and that was taken as establishing that the *secret* of the same name was also different. It was a third copy. Fixed narrower than what broke — that checkout and its push are the token's only use, so `PROTOCOLIZED_PUSH_TOKEN` is scoped to `protocolized-website` alone. The rename was deliberate: a key-store entry and a repo secret both called `GH_PAT`, holding different values, is what made them look interchangeable. Old secret deleted, workflow re-triggered and green.

- **The generalisable finding, and the one most likely to recur elsewhere.** The key store, a GitHub Actions repo secret, and the VM each claimed to hold this project's GitHub credential, under two names — and all three held *different values*, none matching how the registry described any of them. `admin/keys.md` had recorded a narrow fine-grained PAT for five weeks while an account-wide admin token was actually deployed; the key store's own entry turned out to be a *classic* token with `repo, workflow, delete_repo` across everything the account can reach, in plaintext. Documented at the `Code/` level rather than in this project, as **`security-policy.md` Rule 8 — “The Registry Is Not Evidence”**, with a companion section in `warnings-keys.md`. It carries the probes that actually discriminate, because the obvious ones mislead: `GET /repos/{owner}/{repo}` succeeds for any *public* repo with any valid token and reports the *account's* role rather than the token's grant, and every fine-grained PAT an account issues shares its leading `github_pat_11&lt;account-id&gt;` chunk — so both a scope probe and a prefix comparison read “still over-scoped” against a correctly narrow token. What discriminates: a *private* out-of-scope repo must 404, an admin-only endpoint must 403, the token-expiration response header identifies *which* token is in use, and a real ref push proves write. **This is the same silent-drift class as last session's daemon step that was never wired in** — documented intent diverging from deployed reality, with nothing erroring in between.

- The exposure is removed and the old token revoked, but the box is not yet split: both services still run as `exedev` with passwordless sudo, so the Discord bot can read the daemon's GitHub token. c3po makes that split unusually clean — `bin/c3po_bot.py` needs only four environment variables and reaches answers through the public `/query` endpoint, so the service most exposed to untrusted input needs no Anthropic key, no Cloudflare credential and no GitHub credential whatsoever. The runbook is `plans/vm-credential-hardening.md`. Both new tokens expire 2026-12-08, and an expiring PAT fails silently — the daemon simply stops pushing — so expiry checking folds into the pipeline-consistency job already on the list.

---

## Session 53: Two Bugs Stacked on Top of Each Other Kept a New PR Workflow From Ever Working

*2026-09-12 · 15:30–16:20*

**Tracks:** operations, infra, website-integration

**Vectors upserted:** total: 32,592

- **146 failures over two days, all silent.** The daemon's website-push step ran `git fetch origin main` before a `push --force-with-lease` — a refspec that only refreshes the `main` tracking ref. Once the last SIG-pages PR merged and GitHub deleted `c3po/auto-sig-pages`, the VM's cached `origin/c3po/auto-sig-pages` ref kept pointing at the branch's old SHA forever, and force-with-lease compared every subsequent push against that phantom value. Reproduced the failure in an isolated repo before touching production code, and along the way disproved the first fix attempted: `fetch origin main --prune` does *not* prune refs outside the explicit refspec it's given, only a full `fetch origin --prune` does. That distinction only showed up under an actual repro — the git documentation doesn't make it obvious. Fixed, deployed, and confirmed live: the next daemon cycle logged a clean push with no rejection.

- **The push had never actually succeeded before, so the step after it had never actually run.** Session 52 moved PR-opening off the VM entirely, onto a website-repo workflow using the default Actions token — specifically so the VM's own credential wouldn't need `Pull requests` scope. With today's push finally landing, that workflow failed too: `gh pr create` returned *“GitHub Actions is not permitted to create or approve pull requests.”* A direct API probe confirmed this is a **Protocol-Institute org-wide policy** — not something a repo's own `permissions:` block can override, and not fixable without org-admin access. The session-52 redesign, in other words, had never worked even once since it shipped: the branch-push side only started succeeding today, which is the only reason the PR-open side got exercised at all. Four SIG meeting pages had been sitting pushed-but-orphaned the entire time, with no error visible anywhere in the daemon's own logs — from the daemon's point of view, the push itself was the last thing it could see.

- Presented with the choice — relax the org's Actions policy, or add one more narrowly-scoped credential — the owner chose the credential. `WEBSITE_PR_TOKEN` is a fine-grained PAT restricted to the website repo alone, `Pull requests: write` plus `Contents: read`, expiring alongside the two PATs session 52 issued so all three rotate together. The workflow now authenticates with that instead of the default token. Today's already-generated content was opened by hand as a PR in the meantime, since the automation couldn't yet; the unattended *create-a-new-PR* path itself still hasn't been exercised end to end and is the thing to watch on the next weekly cycle.

- While checking whether an existing PAT could cover the new need instead of minting one, a `grep -v` filter meant to exclude two token values from the output didn't, and both were printed. Flagged immediately as exposure under the project's own incident-response posture; the owner judged it low-severity and deferred rotation rather than acting on it in the moment. Recorded here so that deferral is a decision on the record, not a gap nobody chose.

---

## Session 54: A Programme Oracle, and the Discovery That C3PO Never Knew What Day It Was

*2026-09-16 · 11:00-12:15*

**Tracks:** corpus-ingestion, worker-api, website-integration, vector-architecture

**Vectors upserted:** total: 33,193 · symposium: 273

- **New `symposium` namespace — 71 vectors.** Ahead of the September 21-25 symposium, C3PO now carries the full programme: an event overview, the four special-session blocks (Inventing Psychohistory, Worldbuilding in New Nature, Southeast Asia x Protocols, The Art of Memory), all 61 talks, workshops and interactive sessions, and a separate chunk per workshop carrying its audience, takeaways and activities — fields that never appear in full on the programme page and are the real substance for talking about what a workshop is *about*.The source is the site's D1-backed public JSON API, not the rendered page. Scraping our own HTML is how C3PO once ended up insisting "DRG has 0 archived sessions" from a stale snapshot of a stub page, and that lesson applies with more force to a programme still changing daily — one session was added to it the day before this work.The intent is narrow and deliberate: a programme *content* oracle, not a scheduling app. It should connect a talk to the SIG threads and papers behind it, not do agenda arithmetic.

- Building the "don't recommend talks that already happened" behaviour surfaced something broader: **no date reached the model at all.** The system prompt is a static template and the user message was the question plus retrieved excerpts. Web, Discord and MCP alike — C3PO had been answering every question for months with no idea when "now" was.It also corrected an assumption about where answers come from. The exe.dev VM runs the ingest daemon and the Discord *gateway*, but the gateway forwards to the Worker, so every answer is generated in one place. One fix site rather than three.The timestamp goes in the **user message**, never the system prompt. That block is sent with `cache_control: ephemeral`; a value that changes per request would miss the prompt cache on every single query C3PO serves, symposium-related or not. The instruction is static and cached; only the clock reading moves.

- C3PO recommends material and offers insight about it. It does not tell people to register, sign up, apply, join, subscribe or "get involved" — now written into the voice rules corpus-wide rather than as a symposium special case. Facts about an event (dates, status, what happened) are reported as facts, not turned into invitations.It matters immediately here: symposium registration closed at capacity, so a helpful-sounding "you can sign up at..." would have been both a call to action and wrong. Asked point-blank how to register and whether to join the Discord, the deployed bot reports that live slots are full, notes the public livestream, and stops.

- **A silent signature break.** Adding a `symposiumItems` parameter to `mergeResults()` left one of its four call sites passing `limit` where an array belonged — no syntax error, a runtime failure on the MCP search path only.**An invented acronym.** The first live test confidently expanded DRG as "Drama Research Group" and SIGPSY as "Psychology SIG". The cause was the normalisation step: collapsing the programme's two spellings per SIG onto one canonical key (`SIGP4B (Protocols for Business)` and `SIGPfB` are the same track) had also stripped the expansion, leaving the model to guess. The embedded text now carries the full name while metadata keeps the bare key for filtering. A reminder that tidying data for machines can quietly remove what the machine was relying on.

- Orienting on the programme meant reading the API behind it, which turned out to return **51 distinct speaker, organiser and host email addresses** to any anonymous request — along with each submitter's private notes to the organisers and the member vote tallies from shortlisting. Three symposium endpoints and, once swept for the same shape, one on the projects API.Cause: `SELECT *` passed straight to the response. The addresses were genuinely used — but only so the page could decide whether to show *you* an edit button. That decision moved to the server as a boolean; nine separate pages had their own copy of the comparison.The fix projects through an explicit allowlist rather than excluding known-bad fields. Three host-email columns had joined the public payload automatically when a migration added them, which is precisely the failure an allowlist prevents. The ingest applies its own allowlist independently, and refuses outright to embed any chunk containing an email address.

- Twenty-five of the thirty files in the symposium's public slide-deck folder are now in the corpus, so C3PO can answer from what speakers will actually present rather than only from their abstracts.Extraction was the easy half. **Matching a deck to a talk is where this breaks**, because filenames carry neither the talk's slug nor, often, its title. `TMDTS-Speaker-Notes.pdf` is Florian Lohse's “Time Moves Down the Stack” — the initials are the only clue. `The House that Governs Itself.pdf` matches no programme title at all; it turns out to be Daniel Kronovet's companion paper to “Chores as Complex Coordination.” Neither is reachable by name similarity, and both were resolved by reading the file's own text.So the rule is that ambiguity escalates instead of resolving. A deck filed against the wrong talk is worse than an absent one, because the bot then answers confidently from material that belongs to someone else. One deck turned out to cover *two* talks and was quietly resolving to just one of them — the long filename dilutes the similarity score enough that only one candidate clears the threshold, and the other talk's material would have been silently dropped.

- Two decks were slides exported as images: fifteen and fourteen pages, one full-page picture each, not a single character of text. A conventional pipeline records these as failures and moves on, and the talks behind them stay invisible.Claude reads a PDF directly as a document block, doing vision over the pages, so recovering them needed no rasteriser, no OCR engine and no page-splitting — a PDF that yields almost nothing from its text layer simply falls through to a transcription pass. Twenty thousand characters recovered for sixteen cents. The prompt asks for the slide text verbatim plus a single `Visual:` line on diagram slides, and forbids commentary, so what lands in the corpus is the deck rather than an interpretation of it.A related detail worth stating plainly: **speaker decks carry contact slides**. Seven of them held email addresses. The allowlist that protects the programme API is irrelevant here — different provenance, different surface — so deck text is redacted before embedding, and the embedded chunks were swept afterwards to confirm it held.

- While verifying the session's own work, one step in the ingest cycle was found dead: `sync_discord_events` had taken a single HTTP 500 from Discord and exited. It recovered by itself on the next cycle, so nothing was stuck — but the step is how SIG meeting cadence reaches the corpus, and a transient upstream error is not a reason to lose it.The function already waited out rate limits properly; it just raised on everything else. Server errors and network failures now get a bounded backoff, on a budget kept separate from rate limiting — a 429 is Discord telling you exactly how long to wait, which is different in kind from a blip to retry. A 4xx that isn't a rate limit still fails loudly, because that class of error means *we* are wrong, and retrying it just converts a clear failure into a slow one.

---
