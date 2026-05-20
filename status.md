# C3PO — Status Log

## 2026-05-20 — Missed SIG meetings recovered; Discord bot design doc (session 15)

**Root cause fix — active threads invisible to sync — COMPLETE**
- `sync_sig.py`: replaced deprecated channel-level `/threads/active` (returns 404) with guild-level `/guilds/{id}/threads/active` filtered by `parent_id`
- SIGFPT/ProtFiSIG threads auto-archive after 3 days; SIGPfB after 7 days — any meeting thread still active at sync time was silently missed
- Added `is_meeting_override` field in state for manual thread classification without code changes

**SIGPfB pattern fixes — COMPLETE**
- Extended `^Protocols for Business` to match `:` delimiter as well as `[` and space
- Added `^[\*=]+\s*(?:SIG\s+)?Protocols for Business` pattern for threads with `====` / `**===` decorative prefixes

**10 missed meetings recovered — COMPLETE**
- SIGFPT: 01May26 Stigmergy (38 msgs), 15May26 Stigmergy Part II (52 msgs)
- SIGPfB: 20Apr26 API Design, 04May26 Technology, 18May26 Manufacturing, 03Nov25 FDEs, 08Dec25 LLM Adoption, 12Jan26 AI Infrastructure
- ProtFiSIG: 12Mar26 Protocol Fairy Tales, 23Apr26 Wile E. Coyote
- sig namespace: 4,583 → 4,689 vectors; website pages updated (80 → 88 meetings)

**DISCORD_BOT_DESIGN.md — COMPLETE**
- Two-bot architecture: `c3po_listener` (headless launchd batch scripts) vs. `c3po_oracle` (interactive slash-command bot)
- Listener: 3 launchd plists — daily Discord sync, biweekly SIG sync + website pipeline, weekly link enrichment
- Oracle: Discord Interactions webhook → Cloudflare Worker deferred response; `/ask`, `/search`, `/help` commands
- Attachment CDN expiry (24h) and active-thread archival cadence noted as key constraints
- Phase 3A–3F roadmap; 5 open questions documented

**Open TODOs (priority order):**
1. Build launchd plists for daily_sync + sig_sync (+ auto website push) + weekly_links (Phase 3A/3B)
2. YouTube transcript pass — 161 deferred URLs (Phase 3D)
3. Attachment capture in sync scripts — download at ingest time before CDN expiry (Phase 3C)
4. c3po_oracle bot — Discord Interactions webhook (Phase 3E/3F)
5. Add definitions namespace (`lexicon_draft.json`)
6. GitHub Actions cron for `sync_substack.py`

## 2026-05-20 — Exhibit extraction plan revised (session 14)

**Plan: structural-navigation.md revised — COMPLETE**
- Reframed from section summaries + list extracts → four exhibit types: `section_summary`, `list_exhibit`, `figure_exhibit`, `table_exhibit`
- Sampled 5 PDFs: SoP papers embed 20–30 full-page background images (design artifact, filter by >80% page size); pdfplumber table extraction unreliable (picks up typographic grids)
- Strategy: two-pass — Haiku text pass for sections + lists; PyMuPDF render + Haiku vision for figures + tables
- Core essays (>12 text pages) get section summaries; all get list pass; visual pages (<50 words) get vision pass
- Estimated: ~450–550 new vectors, ~$1.30, new dep: pymupdf
- Not yet built

**Open TODOs (priority order):**
1. Build `ingest/extract_structure.py` + `ingest/ingest_structure.py` (exhibit extraction)
2. YouTube transcript pass (161 deferred URLs)
3. Attachment capture — extend sync scripts to read `msg["attachments"]`
4. Set up launchd: daily `sync_discord.py` + weekly `sync_sig.py` + `fetch_discord_links.py`
5. Add more Discord channels; set `DISCORD_SUMMARY_CHANNEL_ID`
6. Add definitions namespace (`lexicon_draft.json`)
7. GitHub Actions cron for `sync_substack.py`

## 2026-05-20 — Discord links fetch + enrichment pipeline (session 13)

**Enrichment results (bb2tle25s) — COMPLETE**
- Scored 1,412 fetched URLs; 899 kept (score 1–3), 485 deleted from Pinecone (score 0), 28 errors (no text in Pinecone)
- Score distribution: 0→485, 1→480, 2→276, 3→143
- Final `discord_links` namespace: **6,722 vectors** (down from ~11,012 pre-enrich)
- Total Pinecone: **19,634 vectors** across 8 namespaces

**discord_links ingest — COMPLETE**
- `fetch_discord_links.py` harvested 3,091 unique URLs (discord + sig namespaces); fetched 1,412; failed 942; skipped 373 (already seen); deferred 355 (161 YouTube, 194 Twitter/X); rejected 9 (injection filter)
- ~11,012 vectors written to `discord_links` Pinecone namespace
- Prompt injection filter: 11 regex patterns + invisible-char density guard (>1%); catches override/jailbreak/token-boundary attacks; false-positive-safe (tightened after rescan found 6 false positives with old patterns)
- `enrich_discord_links.py`: scores each fetched URL 0–3 for protocol relevance via Claude Haiku; deletes score-0 vectors from Pinecone; stores score + reason in registry; resumable, dry-run + limit flags
- Worker integration complete: `normalizeWebLink()`, `discord_links` namespace in all 4 query callsites, relevance-score weighting (0.55/0.65/0.75 base × popularity bonus), `c3po-badge-web` badge
- Enrichment run kicked off in background after session end (bb2tle25s)
- Bug fixed: `SCORE_PROMPT` JSON example had un-escaped braces (`{{"score":...}}`); caught in dry-run before full run

**Open TODOs (priority order):**
1. Check enrichment results (bb2tle25s) — verify score distribution; delete score-0 entries confirmed; update CLAUDE.md with final discord_links vector count
2. YouTube transcript pass — 161 deferred URLs; use `youtube-transcript-api` (no API key)
3. Twitter/X paid API pass — 194 deferred URLs; needs Twitter API v2 credentials
4. Attachment capture — extend sync scripts to read `msg["attachments"]`, download at sync time before CDN URLs expire (24h)
5. Set up launchd: daily `sync_discord.py` + weekly `sync_sig.py` + `fetch_discord_links.py`
6. Add more Discord channels; set `DISCORD_SUMMARY_CHANNEL_ID`
7. Add definitions namespace (`lexicon_draft.json`, deferred)
8. GitHub Actions cron for `sync_substack.py`

## 2026-05-20 — Wire discord/sig into worker, corpus description rewrite (session 12)

**Worker wiring — COMPLETE**
- Badge CSS: `.c3po-badge-discord` (blue), `.c3po-badge-sig` (teal) added to worker CSS
- `badgeForSource()`: discord → "Discord"; sig → SIG display name (SIGFPT, MRG, etc.)
- `buildContextBlock()`: discord label shows channel + date + participants; sig label shows SIG name + chunk type (MEETING / MEETING TRANSCRIPT / DISCUSSION / MESSAGE) + title + date
- `srcLine()`: discord and sig cases for clipboard text export
- `buildExportMarkdown()`: discord and sig cases for .md download
- All 4 query callsites updated to fetch discord + sig namespaces in parallel: `runMcpSearch`, `runMcpAsk`, `GET /search`, `POST /query`
- `mergeResults()` signature: 6 item lists + maxSources (was 5); discord/sig weights wired
- MCP `search_corpus` schema: namespace enum now includes `discord` and `sig`; description updated
- **Deployed:** Phase 2C live at c3po.vgr-702.workers.dev

**Corpus descriptions — COMPLETE**
- UI intro blurb: names Discord + all four SIG groups with session counts
- System prompt `INDEXED CORPUS`: two new entries (Discord channels, SIG sessions + leads)
- How It Works "corpus" table: Discord and SIG rows added with vector counts
- How It Works "Pinecone index" table: discord/sig rows; full tier-weight documentation
- `SOUL_EXCERPT` (export md): replaced vague "285+" with accurate full corpus inventory
- Phase note bumped to 2C

**Open TODOs (priority order):**
1. Attachment capture — extend `sync_sig.py` + `sync_discord.py` to read `msg["attachments"]`, store in registry, build `fetch_discord_attachments.py` (download at sync time before CDN URLs expire)
2. Run `fetch_discord_links.py` (1,042+ URLs in registry) — ingest linked articles
3. Add more general Discord channels to `DISCORD_CHANNEL_IDS`; set `DISCORD_SUMMARY_CHANNEL_ID`
4. Set up launchd: daily `sync_discord.py` + weekly `sync_sig.py`
5. Add definitions namespace (`lexicon_draft.json`, deferred from session 8)
6. GitHub Actions cron for `sync_substack.py`

## 2026-05-19 — Security hardening, header restyle, PI website update (session 11)

**Security & UI — COMPLETE**
- IP strike/ban system (3 strikes/1h → 24h ban), history smuggling detection, MCP search rate limit (100/day)
- KBA filter expanded: Timber Stinson-Schroff, Tim Beiko, PI infrastructure added alongside Venkatesh Rao
- DARKBECOME_RE (roleplay-as-unrestricted) and WIELD_RE (weaponize protocols) filters added
- SYSTEM_PROMPT SAFETY CONSTRAINTS block updated with all protected individuals/assets
- How It Works page: added MCP section (tool table, Claude Code commands, Claude Desktop JSON)
- Worker header: replaced subnav nav menu with minimal brand bar — robot icon + C3PO + coral Beta badge + ← protocolized.io link
- **Deployed:** c3po.vgr-702.workers.dev (Phase 2B)

**PI website (protocol-institute.org) — COMPLETE**
- `projects.html`: C3PO status → "Live · Beta"; description updated to RAG/12k vectors/MCP/Claude Sonnet; direct "Open C3PO →" link added
- `c3po.html`: Status and Technical sections rewritten present-tense; adds live URL, corpus size (12k+ vectors), MCP server paragraph, direct "Try it →" link

**Open TODOs (priority order):**
1. Add `discord` + `sig` namespaces to worker.js (normalizeDiscord, normalizeSig, Discord badges, link gen) — *other agent working on this*
2. Wire discord/sig into mergeResults(): starred 1.0×, unstarred 0.70×
3. Run fetch_discord_links.py once all channels done (1,042+ URLs in registry)
4. Add more general Discord channels to DISCORD_CHANNEL_IDS + set DISCORD_SUMMARY_CHANNEL_ID
5. Set up launchd for daily sync_discord.py + weekly sync_sig.py
6. Add definitions namespace (lexicon_draft.json, deferred session 8)
7. GitHub Actions cron for sync_substack.py

## 2026-05-19 — SIG channel ingest, sync_sig.py (session 10)

**All 4 SIG channels ingested — COMPLETE**
- `ingest/sync_sig.py` — batch SIG harvester, all 4 channels, two-level meeting ingestion
- Meeting detection: per-channel regex patterns; SIGFPT, MRG, SIGPfB, ProtFiSIG each have tuned patterns
- MRG pattern fix: `\d{6,9}` (one thread named `202500807` — 9-digit typo)
- Meeting threads: Claude Haiku summary vector (sig_meeting_summary) + chunked body (sig_meeting_body)
- Non-meeting threads: bundled as sig_discussion; main channel filtered same as sync_discord.py
- SIGFPT: 29 meetings, 38 discussions, 564 msgs → 757 vectors
- MRG: 16 meetings, 9 discussions, 378 msgs → 433 vectors
- SIGPfB: 22 meetings, 77 discussions, 2,001 msgs → 2,214 vectors
- ProtFiSIG: 11 meetings, 43 discussions, 1,072 msgs → 1,179 vectors
- **Total sig: 4,583 vectors** | 77 meeting summaries, 169 discussions across all SIGs
- **Pinecone:** discord: 3,301 · sig: 4,583 · substack: 1,040 · videos: 2,940 · pdfs: 766 · bibliography: 278 · transcripts: 4 · **Total: 12,912**

**Open TODOs (priority order):**
1. Add `discord` + `sig` namespaces to worker.js (normalizeDiscord, normalizeSig, Discord badges, link gen)
2. Wire discord/sig into mergeResults(): starred 1.0×, unstarred 0.70×
3. Run fetch_discord_links.py once all channels done (1,042+ URLs in registry)
4. Add more general Discord channels to DISCORD_CHANNEL_IDS + set DISCORD_SUMMARY_CHANNEL_ID
5. Set up launchd for daily sync_discord.py + weekly sync_sig.py
6. Add definitions namespace (lexicon_draft.json, deferred session 8)
7. GitHub Actions cron for sync_substack.py

## 2026-05-19 — Discord ingest pipeline (session 9)

**Discord harvester built and run — COMPLETE**
- `ingest/sync_discord.py` — REST-only batch poll, no gateway, no persistent process
- Filter: originals ≥20 chars, replies ≥150 chars, thread starters always included
- Thread starters fetch and bundle full thread as one conversation chunk
- Pagination bug fixed: backfill uses `before` (backward), incremental uses `after` (forward)
- Guild ID added to metadata in both format functions; 2,390 existing vectors patched via `index.update`
- `ingest/analyze_discord.py` — one-shot analysis: stats + stratified Claude Haiku pass
- Full historical ingest of `#🤔-idle-protocol-musings` (4 years): 2,877 fetched → 2,390 ingested
- Discord corpus: 1,795 originals · 465 replies · 130 bundled threads · 119 authors · 441 URLs · 104 starred
- Claude analysis: ~85-90% signal, 10 topic clusters identified; saved to `sources/discord_analysis.md`
- **Pinecone:** discord: 2,390 · substack: 1,040 · videos: 2,940 · pdfs: 766 · bibliography: 278 · transcripts: 4 · **Total: 7,418**

**Open TODOs (priority order):**
1. Add `discord` namespace to worker.js (`normalizeDiscord()`, Discord badge, link generation using guild_id+channel_id+message_id)
2. Wire discord namespace into `mergeResults()`: starred 1.0×, unstarred 0.70×
3. Add more Discord channels to DISCORD_CHANNEL_IDS + set DISCORD_SUMMARY_CHANNEL_ID
4. Set up launchd plist for daily sync_discord.py run
5. Ingest lexicon_draft.json as `definitions` namespace (deferred from session 8)
6. Wire `definitions` namespace into query handler (5th active namespace)
7. Magazine lexicon pass — `plans/magazine-lexicon.md`
8. GitHub Actions cron for `sync_substack.py`
9. Phase 2B: MCP Worker at `/mcp`

## 2026-05-18 — Namespace wiring, system prompt enrichment, lexicon extraction (session 8, complete)

**Videos + bibliography wired into live worker — COMPLETE**
- `normalizeVideo()` and `normalizeBibliography()` added to worker.js
- `mergeResults()` updated: pdfs/substack 1.0×, videos 0.9×, bibliography 0.85×relevance_scale
- Both GET /search and POST /query now query all 4 namespaces in parallel
- POST /query secondary retrieval extended to handle video_summary → body chunk expansion
- Talk (orange) and Reference (grey) badge CSS added; URL operator precedence bug fixed
- Chat page blurb updated to enumerate all 4 source types; input placeholder updated
- **Pinecone:** pdfs: 766 · substack: 1,040 · videos: 2,940 · bibliography: 278 · transcripts: 4 · **Total: 5,028**

**System prompt enriched — COMPLETE**
- Added ABOUT THE PROTOCOL INSTITUTE block (SoP history, current programs, leadership, independence)
- Added INDEXED CORPUS block listing named papers, series, speakers — prevents false denials
- Added 40-term PROTOCOL LEXICON with PI-specific compact definitions (injected inline)

**Bibliography — all 136 currently sourced refs ingested**
- fetch_refs.py still running (PID 96761) for remaining 116 inline-pass refs
- ingest_bibliography.py run on all 136 sourced → 278 vectors (136 ref_summary + 142 body)

**Lexicon extraction — COMPLETE**
- `extract_lexicon.py` written with --all flag for full-corpus pass
- First pass: 12 key papers → 245 terms
- Full pass: 82 PDFs → 914 terms from 66 papers (16 skipped: too short/image-only)
- All entries have: term, definition, source (paper title), source_slug, context (verbatim)
- `sources/lexicon_draft.json` — full 914-term draft for future curation
- `sources/lexicon_prompt_block.txt` — compact 40-term system prompt block
- `protocol-lexicon.md` resource published to protocolized-website repo
- Plan: `plans/magazine-lexicon.md` — fiction + nonfiction magazine pass (not yet executed)

**Open TODOs (priority order):**
1. Ingest lexicon_draft.json as vectors → new `definitions` namespace in Pinecone
2. Wire `definitions` namespace into query handler (5th namespace)
3. Magazine lexicon pass — see `plans/magazine-lexicon.md`
4. Curate lexicon_draft.json → update protocolized.io resource page
5. Structural navigation — section_summary + list_extract for PDFs (see `plans/structural-navigation.md`)
6. D1 setup → encryption → strike/ban → self-notes → per-query logging
7. GitHub Actions cron for `sync_substack.py`
8. Phase 2B: MCP Worker at `/mcp`

---

## 2026-05-17 — Curly-quote root-cause fix, subnav, How It Works + Terms pages

- **Root cause found and fixed:** 314 curly/smart quotes (U+201C/201D) throughout `worker.js` caused ALL HTML attribute parsing to fail — `getElementById`, CSS selectors, every `href`. Browser URL clue: "Conversations" link navigated to `%E2%80%9D/%E2%80%9D` (URL-encoded curly quotes). Fixed via Python unicode replacement; confirmed 0 remaining.
- **Shared subnav** added to all pages: `SUBNAV_SVG`, `SUBNAV_CSS`, `subnav(current)` helper — brand robot icon + `/` separator + 4 nav links, active-link highlighting per page.
- **New pages:** `GET /how-it-works` and `GET /terms` — adapted from vgr_zirp equivalents with PI/C3PO branding.
- `/chats` bug resolved — issue documented and closed in `issues/chats-page-load-failure.md`.
- Deployed: `1a10748`; all 4 routes return 200.
- **Pinecone:** substack: 1,040 · pdfs: 766 · transcripts: 4 · Total: ~1,810 (unchanged)

**Open TODOs (priority order):**
1. Tier weighting in `mergeResults()` — first vgr_zirp item, small change
2. CORPUS_MAP in system prompt — prevents false denials
3. Protocol lexicon — ML extraction + hand curation (~40 terms)
4. D1 setup → encryption → strike/ban → self-notes → per-query logging
5. GitHub Actions cron for `sync_substack.py`
6. Phase 2B: MCP Worker

---

## 2026-05-16 — /chats debugging, issues tracking, vgr_zirp plan (22:13–23:02 PT)

- Deployed 4 fixes to /chats page: slim API response, /chats/ trailing-slash redirect, back-link fix, cardHTML field names
- Root bug (getElementById null) persists — documented in `issues/chats-page-load-failure.md` with ruling-out analysis; next: test incognito + hard reload + Network tab
- `plans/vgrzirp-reuse.md` complete — 7 copy, 4 adapt, 5 skip; build order established
- `plans/structural-navigation.md` — unchanged, waiting for implementation slot
- **Pinecone:** substack: 1,040 · pdfs: 766 · transcripts: 4 · Total: ~1,810 (unchanged)

**Open TODOs (priority order):**
1. Resolve `/chats` getElementById null bug (see `issues/chats-page-load-failure.md`)
2. Tier weighting in `mergeResults()` — first vgr_zirp item, small change
3. CORPUS_MAP in system prompt — prevents false denials
4. Protocol lexicon — ML extraction + hand curation (~40 terms)
5. D1 setup → encryption → strike/ban → self-notes → per-query logging
6. GitHub Actions cron for `sync_substack.py`
7. Phase 2B: MCP Worker

## 2026-05-14 — Project initialized

- Repo created at `vgururao/c3po` (personal account; to migrate to Protocol-Institute org at Phase 6)
- README.md with full 6-phase plan
- SOUL.md — bot persona, voice, intellectual commitments
- CLAUDE.md — dev environment and conventions
- `.env.template` — env var inventory
- Directory structure: `ingest/`, `api/`, `submissions/`, `data/`
- Skeleton scripts created for Phase 1 ingest pipeline:
  - `ingest/utils.py` — chunking, Voyage embedding, Pinecone upsert helpers
  - `ingest/ingest_pdfs.py` — PDF ingest from protocolized-website corpus
  - `ingest/ingest_substack.py` — Substack HTML export + RSS sync
  - `ingest/ingest_discord.py` — Phase 4 stub
- Description page (`c3po.html`) published on protocol-institute.org
- Listed as "In Development" initiative on protocol-institute.org/projects.html

**Next:** Create Pinecone index `c3po`, copy PDF corpus to `data/pdfs/`, run `ingest_pdfs.py`.

## 2026-05-14 — Substack ingestion pipeline complete

**Corpus fetched:** 115 posts from Protocolized Substack API + HTML export (116 ingested, 3 pages + 113 newsletters).

**Files created:**
- `sources/substack/api_metadata.json` — full API metadata for all 115 posts (tags, bylines, section, updated_at, reaction_count)
- `sources/substack/collections.json` — authoritative collection/series/SIG membership (13 collections)
- `sources/substack/enriched_meta.json` — Haiku enrichment: summary + categories + author for 129 posts (~$0.05)
- `sources/substack/registry.json` — updated with last_sync state, vector counts, api_endpoint
- `sources/substack/fetch_api_metadata.py` — API fetch script
- `sources/CORPUS_MAP.md` — structural map: sections, authors, extended universe plans, Timber's co-editor role
- `ingest/enrich_substack.py` — Haiku enrichment script (idempotent, checkpoint saves)
- `ingest/ingest_substack.py` — full 4-vector-type ingest (body chunks, post_summary, collection_card, author_profile)
- `ingest/sync_substack.py` — daily API sync (new post detection, edit detection via updated_at, tag change detection)

**Pinecone namespace `substack`:** 1,040 vectors
- ~873 body chunks (title/author/collection/summary prefix for retrieval)
- 116 post_summary vectors (slug__post_summary IDs)
- 13 collection_card vectors (fiction contests, series, SIGs, editorial)
- 38 author_profile vectors (all 38 authors, with regular/guest contributor framing)

**Substack sections discovered:** Fictions (333105, 58 posts), Articles (333110, 47 posts), Obliquities (333103, 5 posts), Protocolized catch-all.

**Contraptions note:** `Publishing/Contraptions/substack-api.md` updated with API-first sync recommendation for that project.

**Next for Substack:** Set up daily cron for `sync_substack.py`. Consider GitHub Actions workflow.
**Next overall:** PDF corpus ingest (Phase 1 completion), then Cloudflare Worker API (Phase 2).

## 2026-05-14 14:30–18:56 PT — Devlog system, session rituals, org admin infrastructure

Added devlog infrastructure (data/devlog.json, devlog_session.py, devlog_render.py) and backfilled Sessions 1–2 from today's ingest work. Added startup (6-step) and wrap-up (9-step) rituals to CLAUDE.md. Keys section updated to point to ../admin/keys.md and ../admin/security.md instead of Code/.env.keys. PI admin repo (Protocol-Institute/admin, private) created with expense tracker, key registry, and security policy.

**Pinecone:** substack: 1,040 vectors (unchanged)
**Next:** GitHub Actions cron for sync_substack.py; PDF corpus ingest.

## 2026-05-15 17:30 PT — PDF corpus ingest + canonical ingest pattern

Completed Phase 1 PDF ingest. Recreated venv (Dropbox doesn't sync it); fixed `pinecone-client` → `pinecone` package rename.

**Canonical ingest pipeline documented** in ARCHITECTURE.md (Layer 1: Haiku enrichment, Layer 2: body chunks with prefix, Layer 3: doc_summary vector). Applies to all corpus sources.

**New scripts:**
- `ingest/enrich_pdfs.py` — Haiku enrichment for PDFs (parallel to enrich_substack.py)
- `ingest/ingest_pdfs.py` — rewritten with prefix, namespace, chunk_type, and doc_summary vectors

**PDF ingest results:**
- 82 PDFs enriched via Haiku (0 errors); saved to `sources/pdfs/enriched_meta.json`
- 771 vectors upserted (766 in Pinecone after dedup by chunk_id)
  - 689 body chunk vectors (5 PDFs image-only, no extractable text)
  - 82 doc_summary vectors (all 82 PDFs)
- 5 image-only PDFs (no body chunks): 65-SCHROFF_GONG-Self-Ensured-cards, 67-FERNANDEZ-Swarm-Protocol-Workshop, 68-FERNANDEZ-Swarm-Games-pxlm, 98-GONG-card-set-2024-03-28, SCHROFF-Protocol-Watching-HANDOUT

**Pinecone:** substack: 1,040 · pdfs: 766 · Total: 1,806
**Next:** GitHub Actions cron for sync_substack.py; Cloudflare Worker query API (Phase 2).

## 2026-05-15 ~19:00–19:12 PT — Phase 2A: Oracle Worker and web UI

Built `api/worker.js` — full C3PO Oracle Worker (~1,100 lines) serving both the API and the embedded web UI.

**Routes:** `GET /` (web UI), `POST /query` (RAG), `GET /search` (no-LLM semantic search), `GET /stats`, `GET /health`, `POST /share` (stub — 503 until D1 Phase 2C).

**Key decisions:**
- **Sonnet not Haiku** throughout — user direction: protocol research material is dense and requires strong synthesis
- **Prompt caching** on SOUL.md-derived system prompt (`cache_control: ephemeral`) — 10× cheaper on subsequent calls
- **Secondary retrieval**: doc_summary/post_summary hits trigger follow-up body-chunk queries (same pattern as vgr_zirp)
- **A/B testing removed** — not applicable to C3PO (no persona versions)
- **PI branding**: teal `#0F6E56`, Lora body font, robot/droid SVG avatar, type-based source badges (Paper/Essay/Fiction/Game/Protocolized)

**Updated files:** `api/worker.js` (new), `api/README.md`
**Pinecone:** substack: 1,040 · pdfs: 766 · Total: 1,806 (unchanged)

**Next:** Deploy to Cloudflare — `wrangler kv namespace create RATE_LIMIT`, set secrets, `wrangler deploy`. Then Phase 2B (MCP Worker). Also still pending: GitHub Actions cron for `sync_substack.py`.

## 2026-05-17 — Phase 2A deployment, security filters, transcript loop, token limits

**Deployed** Phase 2A Oracle Worker to https://c3po.vgr-702.workers.dev. Fixed critical `String.raw` bug (template literal `\n` escape processing killed embedded JS). Fixed dedicated KV namespace segregation (C3PO_KV, `54276a38...`). Fixed iOS form submission conflict.

**Security pre-filters** (3 regexes): INJECTION_RE (existing), SYSEXTRACT_RE (system prompt extraction), CREDENTIAL_RE (API key extraction). All return canned redirect. Deferred: strike/ban tracking (vgr_zirp has this — 3 strikes → 24h IP ban).

**Transcript loop:**
- Auto-log every answered query to KV: `log:{ts}:{rand}` (7-day TTL) via `ctx.waitUntil`
- POST /share implemented: stores `submission:{ts}:{rand}` (90-day TTL) + embeds last Q+A into Pinecone `transcripts` namespace
- GET /admin/transcripts?key=ADMIN_KEY&type=logs|submissions — submission browser
- Fixed: UI sends `{turns:[{q,answer},...]}` not `{query,answer}` — backend now accepts turns array

**Token limits:** MAX_ANSWER_TOKENS 800→1200; system prompt now specifies 350–500 words and "complete every definition fully."

**Pinecone:** substack: 1,040 · pdfs: 766 · transcripts: 2 (test submissions) · Total: ~1,808

**Open TODOs:**
- [ ] **Protocol lexicon in system prompt** — vgr_zirp injects `LEXICON_MD` (~40 terms, ~4k tokens) directly into the system prompt. For C3PO: ML candidate extraction from corpus (similar to vgr_zirp's `api_tagger.py` pass), then hand-curate definitions, then inject as `PROTOCOL_LEXICON` block. Solves chunk-boundary definition splits permanently.
- [ ] **Deep vgr_zirp review** — before building anything new, audit the full oracle+mcp+search worker stack to avoid reinventing completed work. Key files: `workers/oracle/index.js`, `build-prompt.js`, `persona.js`, `workers/mcp/index.js`. Areas to check: strike/ban tracking, D1 query logging schema, moderation filter, self-notes, share/transcript CRUD, RSS feed, /search endpoint design.
- [ ] **GitHub Actions cron** for `sync_substack.py` (pending since Phase 1)
- [ ] **Phase 2B: MCP Worker** at `/mcp` endpoint

## 2026-05-16 — Chat index + individual chat pages, session tracking

**Redesigned transcript UX** based on vgr_zirp pattern:
- `/chats` — public chat index (no key required); admin view (with X-Admin-Key) shows all including private, with status dropdowns to flip public/private/pending
- `/chats/:chatId` — individual chat page (full Q/A, Lora font, C3PO droid avatar, private wall for non-admin)
- `/admin` now redirects to `/chats`
- `/api/chats`, `GET /api/chat/:id`, `PATCH /api/chat/:id` — REST endpoints backing the UI
- PATCH fallback scan added for legacy entries lacking reverse-lookup `chatid:` key
- CORS expanded to include PATCH

**Session tracking:** Each browser session generates a `session_id` (8-char random); passed in POST /query body and stored in auto-logs alongside `turnNumber`. Enables per-session analysis without linkability.

**Header link:** "conversations" link in main chatbot header → `/chats`

**Pinecone:** substack: 1,040 · pdfs: 766 · transcripts: 4 (2 public, 2 private) · Total: ~1,810
