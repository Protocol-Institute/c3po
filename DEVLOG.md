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
