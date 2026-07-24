# C3PO — Status Log

## 2026-07-24 10:00–10:50 PT — Ingestion pause/resume + live retrieval-failure fix (session 45)

**Built a pause/resume mechanism for the ingest pipeline** (`ingest/utils.py`, `bin/daemon.py`, new `ingest/ingestion_control.py`). Every write-touching ingest script already gets its Pinecone client from `get_pinecone_index()` — the single choke point — so it's wrapped in a `_GuardedIndex` that blocks `upsert`/`update`/`delete` while a write pause is active and `query`/`fetch`/`list` while a read pause is active (independent channels — Pinecone tracks write-unit and read-unit as separate monthly quotas), and auto-pauses itself the instant Pinecone returns a write- or read-unit 429. `daemon.py` checks pause state before each cycle and skips invoking the 10 write-touching + 1 read-only (`rebuild_sig_summaries.py`) subprocess steps entirely while paused — zero Discord/Voyage/Anthropic/Pinecone cost, not just avoided Pinecone writes. Verified with a monkeypatched dry-run (confirmed exactly the intended steps get skipped, nothing else touched) before restarting the live daemon via `launchctl kickstart`. `sync_substack.py` (GitHub-Actions-only, not covered by the daemon's skip logic) got an explicit early check. State lives in `data/ingestion_pause_state.json`, deliberately **not** gitignored so the GHA runner sees the same pause state after checkout. Resume needs no special backfill logic — every script's state file only updates on success, so a paused cycle just means a bigger delta next successful run, same as any missed cycle.

**Discovered c3po's own read-unit quota was also exhausted** — confirmed live, `rebuild_sig_summaries.py`'s `idx.list()` 429'd with "read unit limit for the current month (1000000)" even with only a write pause active. Same shared account as humboldt's earlier read-unit exhaustion (session 44). Activated both a write and a read pause via `ingest/ingestion_control.py`, both until **2026-08-01T00:00:00Z**.

**Found and fixed a live production issue while testing:** the public `/query` endpoint was silently answering questions with zero retrieval grounding. Every Pinecone namespace query was 429ing on the read-unit cap; `queryNamespace()` caught each failure and returned `[]`, indistinguishable from "no matches" — so the Worker still called Claude and returned a fluent, confident, fully unsourced answer (`sources: []`) with no indication anything had failed. Confirmed live via `wrangler tail` (11/11 namespace queries 429ing on a single test call). Fixed: `queryNamespace()` now tags a failed call's return array with `_pineconeFailed = true` rather than changing its signature, so all ~30 call sites across the 4 query paths (`runRagQuery`, `runMcpSearch`, `runMcpAsk`, `GET /search`) can check `anyPineconeFailed(...)` without a refactor. Degraded responses now either prepend an honest notice to the answer text or return a `degraded` flag. Verified live on all 4 paths post-deploy.

**Fixed a second, unrelated pre-existing bug found along the way:** `GET /search` was throwing a 500 on every single call — its `mergeResults()` call was missing the `metaItems` argument (11-param signature, only 10 passed), silently shifting `MAX_SOURCES` into the `transcriptItems` slot and calling `.map()` on a number. This is humboldt's `query_c3po_worker()` fallback retrieval path (`agent/retrieval.py`), so it's been silently broken there too — worth flagging to humboldt if their worker-mode retrieval has seemed dead.

**Pinecone:** 28,982 vectors — unchanged (both write and read paused deliberately). No `describe_index_stats()` impact — confirmed that call keeps working through a read-unit 429 (it's control-plane, not a per-vector read), so `/status`, `/health`, and `publish_dashboard.py` are all unaffected.

**Open TODOs (priority order):**
1. **2026-08-01:** confirm both write and read pauses clear naturally (`python3 ingest/ingestion_control.py status` — should show "not paused" for both after 08-01), then confirm a full daemon cycle completes 16/16 (not just that steps stop being skipped — verify they actually succeed)
2. Consider whether humboldt's `agent/retrieval.py` `query_c3po_worker()` mode is actually used anywhere — if so, tell them `/search` was broken and is now fixed
3. Identify the owner of the MCP SSE reconnect-loop client (107.201.136.15, AT&T, La Cañada Flintridge CA) — currently silent post-fix, but posted to Discord unidentified
4. Execute exe.dev migration — `plans/exe-dev-migration.md`, pending VGR's answers to the 5 open questions
5. Implement `ingest/sync_roam.py` (plan: `plans/roam-ingest.md`) — confirm still wanted given Roam deprecation decision
6. Create `Protocol-Institute/sig-notes` repo + `_template.md`; discuss with SIG hosts
7. Starter page — 28 recs across 20 resources in tally (threshold reached); build "good first reads" page + wire into intro handler
8. Exhibit extraction — `ingest/extract_structure.py`
9. Anthropic key rotation to PI org account
10. Rotate `GH_PAT` to fine-grained PAT scoped to protocolized-website only
11. Fix discord guide eligibility: only embed active channels; currently 80 described channels which is too many
12. Investigate intro-quality title-match regression (session 43 finding, still open)

---

## 2026-07-24 08:00–09:50 PT — Worker load incident + Pinecone quota root-cause (session 44)

**Cloudflare Worker load alert investigated.** Account-wide `workersInvocationsAdaptive` (GraphQL Analytics) showed `c3po` at ~17,500 req/hr sustained, up from ~5K/day (07-17) to ~215K/day (07-24) — `protocolized-website` unaffected (~1-2K/day, flat). `wrangler tail` (two windows, 106 sampled requests, all identical) traced it to a single fixed IP (107.201.136.15, AT&T, La Cañada Flintridge CA) in a tight reconnect loop against `GET /mcp` with `Accept: text/event-stream` — not a distributed attack: single static IP, zero errors, never touched `/query`/`ask_c3po`/anything cost-bearing (`/stats` confirmed 0 tracked requests that hour). Root cause: `GET /mcp` (`api/worker.js:3221`) returned `200` with a plain-text banner instead of `405`, so a client using the legacy MCP SSE transport read the non-stream response as a dropped connection and reconnected with no backoff.

**Fix:** `GET /mcp` now returns `405` (worker.js, deployed — version `dcf0b6e7`). Verified live: the offending client's request rate dropped from ~2/sec to zero within a minute of deploy, with no further reconnect attempts in two follow-up `wrangler tail` checks. `POST /mcp` (the real JSON-RPC path) unaffected. Posted a short Discord note with the IP/location so the (still unidentified) owner can reconfigure their client with `--transport http`.

**Merged [Protocol-Institute/c3po#1](https://github.com/Protocol-Institute/c3po/pull/1)** (opened by another agent): `bin/daemon.py`'s `WEBSITE_PATHS` still listed `sigs.html`, removed from the website repo since its restructure to `sigs/index.html`. That stale pathspec broke `git stash push -u -- sigs/ sigs.html monitoring.html` on every attempt since 2026-07-09 (confirmed in `daemon.log`: 07-09, 07-16, 07-23), silently aborting the weekly SIG-page PR flow — no automated website PR has actually landed since PR #5 (07-08). Verified `sigs.html` really doesn't exist and the log evidence matched before merging. Also dropped 3 orphaned `git stash` entries this had left in the local `website` repo clone (all against the same stale base commit, all auto-generated SIG-page regen diffs — confirmed via `git stash show` before dropping).

**Pinecone write-unit quota root cause identified (see [[project_pinecone_quota]]).** c3po's own ingest scripts were confirmed properly incremental (state-file/hash-based skip logic; daemon log shows `0 new`/`1 to embed` on nearly every cycle) — not the driver. Root cause is `humboldt/agent/ingest.py`'s `ingest_all()`: it re-embeds and re-upserts humboldt's **entire** corpus (5,142 chunks) on every call, with no content-hash check, and `daemon/discord_client.py` calls it on every new notebook entry (~daily, confirmed 36 occurrences since 05-28, 25 in July alone before the cap hit). `PINECONE_API_KEY` is shared account-wide between `c3po` and `humboldt` (separate indexes, same account/quota) — humboldt's write-unit burn exhausted the account's monthly cap by 07-21, blocking c3po's writes too; humboldt's own separate read-unit cap (1M/month) was also exhausted by 07-22 from its own retrieval query volume. Opened [Protocol-Institute/humboldt#1](https://github.com/Protocol-Institute/humboldt/pull/1) (not merged — humboldt's repo, left for that project to review) adding a content-hash state file (`data/ingest_state.json`) so `ingest_all()` only touches chunks that actually changed; verified locally (mocked Pinecone/Voyage) against the live corpus — first run upserts all 5,142 chunks (one-time state backfill), second run moments later upserted only 8 genuinely-changed chunks. Built the fix in an isolated `git worktree` rather than humboldt's live working directory, which had substantial unrelated uncommitted in-progress work from the running daemon itself.

**Two reusable lessons promoted to `Code/` level** (see `Code/warnings-keys.md` "Shared Keys = Shared Quotas" and `Code/warnings.md` "Git: Don't Operate Directly on a Live Autonomous Agent's Working Directory").

**Pinecone:** 28,982 vectors — unchanged. **Update, same session:** [Protocol-Institute/humboldt#1](https://github.com/Protocol-Institute/humboldt/pull/1) merged (2026-07-24, confirmed via `gh pr view`). VGR's decision: no plan upgrade — ingestion is deliberately paused until the monthly quota resets 2026-08-01, rather than paying to lift the cap early. Daemon cycles will keep running and hitting 429 on write steps until then (harmless — Pinecone rejects before charging); this is expected, not a new fault. Both prerequisites from the earlier TODO are now resolved: the humboldt-side leak is fixed, and the plan is to let the cap reset naturally.

**Open TODOs (priority order):**
1. **2026-08-01:** confirm Pinecone write-unit quota actually reset and a full daemon cycle completes 16/16 (currently 9/16, 7 write-dependent steps failing on 429) — with humboldt#1 merged, a fresh reset should not immediately re-exhaust
2. Identify the owner of the MCP SSE reconnect-loop client (107.201.136.15, AT&T, La Cañada Flintridge CA) — currently silent post-fix, but posted to Discord unidentified
3. Execute exe.dev migration — `plans/exe-dev-migration.md`, pending VGR's answers to the 5 open questions
4. Implement `ingest/sync_roam.py` (plan: `plans/roam-ingest.md`) — confirm still wanted given Roam deprecation decision
5. Create `Protocol-Institute/sig-notes` repo + `_template.md`; discuss with SIG hosts
6. Starter page — 28 recs across 20 resources in tally (threshold reached); build "good first reads" page + wire into intro handler
7. Exhibit extraction — `ingest/extract_structure.py`
8. Anthropic key rotation to PI org account
9. Rotate `GH_PAT` to fine-grained PAT scoped to protocolized-website only
10. Fix discord guide eligibility: only embed active channels; currently 80 described channels which is too many
11. Investigate intro-quality title-match regression (session 43 finding, still open)

---

## 2026-07-23 11:00–11:45 PT — Repo made public; session-start audit; ingestion pipeline review (session 43)

**Session-start checklist — no code changes.**
- Pinecone: 28,208 → 28,982 (+774) since session 42, all from ongoing daemon activity: `discord_links` +423 (10,949), `sig` +283 (6,281), `discord` +54 (5,712), `substack` +13 (1,148), `meta` +1 (43).
- Substack dry-run: 2 new posts pending (`the-crooked-timber-of-ai`, `thanatosis-on-the-central-mast-liveness`) plus 54 posts flagged "edited" — likely a bulk metadata change upstream rather than 54 real edits; not investigated this session, flagged for next sync.
- Intro quality: 5 unreviewed issues (Jul 12–18), **not marked reviewed** — all 5 share the same pattern (`title_mismatch` + `no_title_match` together: the primary link falls back to rank-order because the answer never names its source verbatim), and 2 of the 5 land on the low-signal "durable ai adoption" resource. One (Paul Staples, Jul 16) also surfaced a VGR-authored paper (USoP) in the answer text. Worth a dedicated look at whether the answer generator or the fuzzy title-match (`_find_mentioned_source()`, ≥0.6 threshold) regressed — see `[[feedback_intro_handler]]`.
- Cost: $1.37 last 7 days (339 calls, ~98% `sync_sig`), $6.61 all-time since 2026-06-25.

**Repo visibility: Protocol-Institute/c3po flipped private → public** (user request). Checked full git history for committed secrets first (`.env`, `*_key`, `*secret*`, `*credential*`, `*token*` filenames) — clean, none ever committed. No code or config changes.

**Discussed, no action taken:**
- `c3po_inbox/ProtocolTheory-2026-06-17-14-32-07.json` (SIGFPT Roam export, triaged in session 39) is still the only pending inbox item — `ingest/sync_roam.py` from `plans/roam-ingest.md` still not built (TODO #2 since session 39). Possibly superseded by the session-39 decision to deprecate Roam as the SIG capture format going forward — flagged to confirm before building a one-off script for a format being phased out.
- Recapped the routine ingestion pipeline (GitHub Actions vs. daemon vs. live-session-only work) for reference — no changes.

**⚠️ Pinecone monthly write-unit quota exhausted.** `sync_devlog.py` failed with `429: You've reached your write unit limit for the current month (2000000)`. This is an account-level cap, not a bug — it likely means the daemon's ongoing upserts (`sync_sig`, `sync_discord`, `fetch_discord_links`, etc.) have been silently failing too since whenever the cap was hit this month, which would explain why the daemon-driven vector-count deltas logged above look plausible but haven't been cross-checked against what should have landed. Devlog page itself still published fine (`generate_devlog_page.py` writes to D1, independent of Pinecone) — only the `meta` namespace session vector is missing for session 43. **Needs Pinecone plan upgrade or quota reset before any further ingestion will land.**

**Pinecone:** 28,982 vectors (unchanged by this session; see counts above — session-43 devlog vector NOT written, see quota note)

**Open TODOs (priority order, unchanged from session 42):**
1. Execute exe.dev migration — `plans/exe-dev-migration.md`, pending VGR's answers to the 5 open questions
2. Implement `ingest/sync_roam.py` (plan: `plans/roam-ingest.md`) — **confirm still wanted given Roam deprecation decision**
3. Create `Protocol-Institute/sig-notes` repo + `_template.md`; discuss with SIG hosts
4. Starter page — 28 recs across 20 resources in tally (threshold reached); build "good first reads" page + wire into intro handler
5. Exhibit extraction — `ingest/extract_structure.py`
6. Anthropic key rotation to PI org account
7. Rotate `GH_PAT` to fine-grained PAT scoped to protocolized-website only
8. Fix discord guide eligibility: only embed active channels; currently 80 described channels which is too many
9. **New:** investigate intro-quality title-match regression (5/5 recent flagged issues share the same failure pattern — see above)
10. **New, urgent:** Pinecone monthly write-unit quota exhausted (2,000,000 cap hit) — upgrade plan or wait for reset; daemon ingestion may be silently failing until resolved

---

## 2026-07-08 11:00–12:32 PT — SIG meeting-publishing bug fixes + website PR deconfliction + exe.dev migration plan (session 42)

**Bug fixes — SIG meeting pipeline:**
- Fixed SIGPSY/DRG meeting-detection regex in `data/channel_manifest.json`: it required an exact 3-letter month abbreviation immediately followed by digits, so full month spellings ("2July26") were silently misclassified as discussion, not meeting — this was why the SIGPSY "Three Temporalities" meeting was missing from the site.
- Added shared `meeting_ready()` / `MEETING_GRACE_DAYS = 7` (`ingest/utils.py`), applied at three layers — `sync_sig.py` (ingestion), `rebuild_sig_summaries.py` (JSON building), `update_sig_pages.py` (detail-page/link creation) — so a meeting thread created ahead of the actual session (agenda/reading-list post only) isn't treated as complete and published until 7 days after its date. A thread flagged as a pending meeting gets rechecked every cycle regardless of message-count changes. Two already-premature meetings (SIGFPT "Summer Deep Dive" dated Jul 10, DRG "EIP-8126" dated Jul 9) had already been auto-summarized with fabricated past-tense text describing sessions that hadn't happened; cleaned up their Pinecone vectors and JSON records.
- Fixed `generate_sig_pages.py`: it runs after `update_sig_pages.py` (which links each meeting title to its detail page) but had no awareness those links existed, so every full-page regeneration silently stripped them back to plain text — this had apparently been happening for a while, across nearly every meeting on all 6 SIG pages. Now checks for an existing detail-page directory and preserves the link.
- Fixed a related regression: the same regeneration was reverting a manually-applied "Session livestream (YouTube)" link label back to a bare domain. Both `update_sig_pages.py` and `generate_sig_pages.py` now special-case YouTube links so this won't recur.

**Website deconfliction protocol:**
- `bin/daemon.py`'s `push_website_if_changed()` no longer pushes straight to `main` on the website repo — it rebuilds a dedicated branch (`c3po/auto-sig-pages`) from `main` and opens/updates a PR via `gh`, so the website project's own presentation/formatting edits aren't silently clobbered by an automated regeneration. Root cause this session: a daemon cycle's full-page regeneration collided with manual website-side formatting work. Filed [Protocol-Institute/website#5](https://github.com/Protocol-Institute/website/pull/5) as the one-off correction, with a full writeup for the website side.
- Batched the PR check to run at most once every 7 days (`WEBSITE_PUSH_INTERVAL_DAYS`, new `data/website_push_state.json`) instead of every 30-minute daemon cycle, so an open PR isn't bumped every cycle.

**exe.dev migration planning:**
- Wrote `plans/exe-dev-migration.md` — moves `bin/daemon.py` + `bin/c3po_bot.py` to VGR's existing exe.dev VM (root SSH, persistent Debian/Ubuntu, systemd+apt), replacing the unexecuted Hetzner plan (`plans/vps-migration.md`, now marked superseded/do-not-execute). Scope: daemon + Discord bot only (humboldt deferred to later); Claude Code installed for interactive SSH maintenance sessions only, no scheduled/autonomous jobs. Five open questions logged (VM SSH access, root vs non-root user, GitHub auth strategy, Claude Code auth mode, directory layout) — pending VGR's input before execution.
- Generated `requirements.txt` (didn't exist before — laptop `.venv` had grown ad hoc over many sessions) via `pip freeze`, needed before any clone-elsewhere step in the migration.

**Pinecone:** sig: 5,991 → 5,998 (net of deleting the 2 premature meetings' 4 vectors, offset by ongoing Discord activity) · Total: 28,199 → 28,208

**Open TODOs (priority order):**
1. Execute exe.dev migration — `plans/exe-dev-migration.md`, pending VGR's answers to the 5 open questions
2. Implement `ingest/sync_roam.py` (plan: `plans/roam-ingest.md`)
3. Create `Protocol-Institute/sig-notes` repo + `_template.md`; discuss with SIG hosts
4. Starter page — 28 recs across 20 resources in tally (threshold reached); build "good first reads" page + wire into intro handler
5. Exhibit extraction — `ingest/extract_structure.py`
6. Anthropic key rotation to PI org account
7. Rotate `GH_PAT` to fine-grained PAT scoped to protocolized-website only (consider bundling with the exe.dev GitHub-auth setup — same underlying need)
8. Fix discord guide eligibility: only embed active channels; currently 80 described channels which is too many

---

## 2026-07-08 — #meeting-notes pipeline: corpus ingest + website detail pages (session 41)

**`#meeting-notes` pipeline — COMPLETE**
- New channel: OpenRecapper-PI bot posts SIG audio call summaries + ad hoc sessions. R2 permanent URLs, not Discord CDN.
- `ingest/sync_meeting_notes.py` (new): scans #meeting-notes for header messages, fetches `summary.md` from R2, chunks by section, embeds via Voyage, upserts to Pinecone `sig` namespace. Handles both SIG and ad hoc meetings. 16 vectors ingested: Jul 01 ad hoc (6), Jul 02 SIGPSY (5), Jul 08 ad hoc (5). State file: `data/meeting_notes_state.json`.
- `ingest/update_sig_pages.py`: extended `render_detail_page()` to add "Session Recording Summary" block from `audio_*` fields in meeting JSON — overview, key points, questions excerpt, participants, duration, link to R2 notes.
- `data/sigs/meetings/audio_1522286708635340840.json` (new): SIGPSY 2026-07-02 meeting; no Discord thread existed so created from audio summary alone. 10 participants.
- `config/discord_channels.json`: added `#meeting-notes` entry (type: Bot Feed, source: manual).
- `api/worker.js`: `normalizeSig()` extended to handle `audio_meeting_summary` and `audio_meeting_section` chunk types — `isAudioSummary` / `isAudioSection` booleans, merge weights 0.92× / 0.85×, context block type labels "AUDIO SUMMARY" / "AUDIO RECORDING", R2 URL from metadata, title from `meeting_title`. Added SIGPSY and DRG to SIG_NAMES. Deployed: `62263606`.
- `plans/meeting-notes-ingest.md` (new): documents source format, both actions, implementation.
- Website: 9 new SIG meeting detail pages created and pushed (DRG, MRG ×2, ProtFiSIG, SIGFPT, SIGPfB ×2, SIGPSY ×2 including the audio-enriched Jul 02 page).
- `bin/daemon.py`: added `sync_meeting_notes` (after `sync_sig`) and `update_sig_pages` (after `rebuild_sig_summaries`) to the sync cycle.

**Pinecone state: 28,199 vectors** (sig +170 since session 39: Discord SIG activity + 16 new audio vectors)

**Open TODOs (priority order):**
1. Implement `ingest/sync_roam.py` (plan: `plans/roam-ingest.md`)
2. Create `Protocol-Institute/sig-notes` repo + `_template.md`; discuss with SIG hosts
3. Starter page — 28 recs across 20 resources in tally (threshold reached); build "good first reads" page + wire into intro handler
4. Execute VPS migration — `plans/vps-migration.md`
5. Exhibit extraction — `ingest/extract_structure.py`
6. Anthropic key rotation to PI org account
7. Rotate `GH_PAT` to fine-grained PAT scoped to protocolized-website only
8. Fix discord guide eligibility: only embed active channels (SIG channels + general + idle-protocol-musings + protocol-watch); currently 80 described channels which is too many

---

## 2026-07-08 — Security audit + /share transcript bug fix (session 40)

**Security audit: D1 input validation — COMPLETE (no issues found)**
- Audited `api/worker.js` for the beacon-endpoint pattern from vgr_zirp (probe strings accumulating in D1 due to missing categorical whitelists).
- Finding: **no D1 binding** — `[[d1_databases]]` is commented out in `wrangler.toml`; all state uses KV only. The vgr_zirp SQL injection pattern doesn't apply here.
- KV categorical fields are properly guarded: `shareMode` uses `Array.includes()` whitelist, `status` is computed internally or whitelist-validated on admin PATCH, `rating` is range-clamped. Clean.

**Bug fix: `/share` Pinecone upsert — FIXED**
- `handleShare()` at line 970 referenced `rand`, which is only in scope inside `logQuery()`. Every `/share` call was silently throwing `ReferenceError` and skipping the real-time Pinecone transcript upsert.
- Fixed: replaced `rand` with `chatId` (already in scope, the correct unique identifier).
- No submissions were lost — KV writes succeed before the failing code, and `sync_web_chats.py` picks them up on the next daemon cycle.
- Deployed: `4ec05ced`.

**Substack sync: 1 new post**
- `a-visitors-guide-to-the-disposition` — 14 vectors; substack: 1,121 → 1,135.

**Pinecone state: 28,193 vectors** (daemon activity since session 39: discord_links +463, sig +151, discord +19, substack +14 this session, transcripts +12, meta +2)

**Open TODOs (priority order):**
1. Implement `ingest/sync_roam.py` (plan: `plans/roam-ingest.md`)
2. Create `Protocol-Institute/sig-notes` repo + `_template.md`; discuss with SIG hosts
3. Starter page — 28 recs across 20 resources in tally (threshold reached); build "good first reads" page + wire into intro handler
4. Execute VPS migration — `plans/vps-migration.md`
5. Exhibit extraction — `ingest/extract_structure.py`
6. `sync_sig_pages.py` + `update_sig_pages.py` — add to daemon (needs VPS first)
7. Anthropic key rotation to PI org account
8. Rotate `GH_PAT` to fine-grained PAT scoped to protocolized-website only

---

## 2026-06-28 — /status page artifact counts + origin breakdown (session 39)

**/status page improvements — COMPLETE**
- Extended `ingest/publish_dashboard.py`: added `artifact_counts()` (reads local state files to derive per-namespace artifact counts), `NAMESPACE_TIERS` (PI / Community / Third-party / System), `ARTIFACT_UNITS`, and `build_breakdown()` (aggregates into four summary buckets).
- Updated `renderStatusPage()` in `api/worker.js`: new "By Origin" card grid at top of page; namespace table gains Artifacts and Origin (tier badge) columns.
- Live at `c3po.protocolized.io/status`. Breakdown: PI 5,573 vectors (97 talks · 123 posts · 74 papers · 914 terms), Community 11,539 (103 meetings · 10 channels · 80 described), Third-party 10,341 (1,252 links · 252 refs), System 65 (38 sessions · 27 conversations).
- Added devlog entries for sessions 38 and 39 (both were missing).

**Pinecone state: 27,518 vectors** (daemon activity since session 38: sig +233, discord_links +119, discord +41, substack +15, meta +3, discord_guide +1, transcripts +2)

**Open TODOs (priority order):**
1. Upgrade Pinecone plan to resolve read-unit limit — BLOCKER (should be resolved with monthly reset)
1a. **Security audit: D1 input validation** — vgr_zirp (the parent project this is based on) was found to have SQL injection probe strings accumulating in `sponsor_events` due to missing input whitelisting on beacon endpoints. All D1 writes used parameterized queries so no execution risk, but garbage data polluted stats tables. Audit `api/worker.js` for any endpoints that accept user-supplied strings and write them to D1 without whitelisting (beacon-style endpoints, event tracking, telemetry fields). Pattern to fix: replace `.slice(0,N)` length caps with `Set.has()` whitelist checks for categorical fields. See ribbonfarm_site session 69 for the full fix.
2. Implement `ingest/sync_roam.py` (plan: `plans/roam-ingest.md`)
3. Create `Protocol-Institute/sig-notes` repo + `_template.md`; discuss with SIG hosts
4. Starter page — 28 recs across 20 resources in tally (threshold reached); build "good first reads" page + wire into intro handler
5. Execute VPS migration — `plans/vps-migration.md`
6. Exhibit extraction — `ingest/extract_structure.py`
7. `sync_sig_pages.py` + `update_sig_pages.py` — add to daemon (needs VPS first)
8. Anthropic key rotation to PI org account
9. Rotate `GH_PAT` to fine-grained PAT scoped to protocolized-website only

---

## 2026-06-25 — Cost dashboard + Pinecone read-unit limit hit (session 38)

**Cost monitoring dashboard — COMPLETE**
- Worker (`api/worker.js`): Added `trackDiscordRequest()` — new `stats:discord:day:*` and `stats:discord:lifetime` KV keys accumulate Discord-specific usage (parallel to existing MCP tracking). `/stats` endpoint now returns `discord_day` and `discord_lifetime`. Deployed: version `40763a0f`.
- Monitoring page (`ingest/generate_monitoring_page.py`): Added `cost_section()` — fetches `/stats` live, reads `data/cost_log.jsonl`, reads bot session log. Shows 4-row table: Web UI (c3po_web), Discord bot (c3po_bot via Worker), Ingest pipeline (c3po_listener), Cloudflare infrastructure. Includes tracked actuals + pre-tracking estimates. Now writes to both `c3po/monitoring.html` and `website/monitoring.html`.
- Current totals: Web $3.61 (142 req), Discord $0.00 tracked + $0.76 est. (42 pre-tracking events), Ingest $0.02 tracked + $2.51 hist. est., CF $5.00. Grand tracked: $8.63 + ~$3.27 estimated pre-tracking.

**Pinecone read unit limit hit — BLOCKER**
- PI org Pinecone account hit 1M read units/month (free tier limit).
- All Worker queries return empty sources silently — bot has been returning context-free answers.
- Root cause: Pinecone free plan 1M RU/month exhausted; likely from high daemon query volume + dev testing.
- Fix: upgrade Pinecone plan (user acknowledged). Limit resets monthly.

**Pinecone state: 27,420 vectors** (daemon activity since session 37: sig +152, discord_links +119, discord +32, substack +9, meta +3, discord_guide +1)

**Open TODOs (priority order):**
1. Upgrade Pinecone plan to resolve read-unit limit — BLOCKER
2. Implement `ingest/sync_roam.py` (plan: `plans/roam-ingest.md`)
3. Create `Protocol-Institute/sig-notes` repo + `_template.md`; discuss with SIG hosts
4. Starter page — 28 recs across 20 resources in tally (threshold reached); build "good first reads" page + wire into intro handler
5. Execute VPS migration — `plans/vps-migration.md`
6. Exhibit extraction — `ingest/extract_structure.py`
7. `sync_sig_pages.py` + `update_sig_pages.py` — add to daemon (needs VPS first)
8. Anthropic key rotation to PI org account
9. Rotate `GH_PAT` to fine-grained PAT scoped to protocolized-website only

---

## 2026-06-24 — Cost tracking + New Nature ingest (session 37)

**Anthropic API cost tracking — COMPLETE**
- Created `ingest/cost_logger.py`: shared utility that appends one JSON line per Claude API call to `data/cost_log.jsonl` (pricing table: Haiku 4.5, Sonnet 4.6, Opus 4.8).
- Instrumented 4 daemon scripts: `enrich_discord_links`, `rebuild_sig_summaries`, `sync_discord_channels`, `sync_sig`.
- Created `bin/cost_report.py`: reports last-7-days + all-time spend with per-script breakdown.
- Added cost report as step 5 in `CLAUDE.md` startup ritual.
- Note: `data/cost_log.jsonl` doesn't exist yet — tracking starts on next daemon cycle.

**New Nature special feature ingest — COMPLETE**
- Ingested essay HTML + slides PDF from `protocolized-website/inbox/.processed/new-nature/`.
- Created `ingest/ingest_new_nature.py` — extracts text from HTML (BeautifulSoup) and PDF (pdfplumber), chunks, embeds into `pdfs` namespace.
- 15 vectors total: essay (9 body + 1 summary), slides (4 body + 1 summary).
- Enrichment records added to `sources/pdfs/enriched_meta.json`.
- pdfs: 750 → 765; total: ~27,089 → ~27,389.

**Pinecone state: ~27,389 vectors** (pdfs: +15)

**Open TODOs (priority order):**
1. Implement `ingest/sync_roam.py` (plan: `plans/roam-ingest.md`)
2. Create `Protocol-Institute/sig-notes` repo + `_template.md`; discuss with SIG hosts
3. Starter page — 28 recs across 20 resources in tally (threshold reached); build "good first reads" page + wire into intro handler
4. Execute VPS migration — `plans/vps-migration.md`
5. Exhibit extraction — `ingest/extract_structure.py`
6. `sync_sig_pages.py` + `update_sig_pages.py` — add to daemon (needs VPS first)
7. Anthropic key rotation to PI org account
8. Rotate `GH_PAT` to fine-grained PAT scoped to protocolized-website only
9. Phase E: multi-node swarm planning

---

## 2026-06-17 — Intro quality system + SIG meeting capture design (session 36, PT 11:00–14:56)

**Intro response quality fixes — COMPLETE**
- Root cause: VGR-authored papers (especially USoP) were being mentioned in Claude's answer but excluded from title-matching, so the fallback picked an unrelated source. Also: Retrospectus was appearing as a fallback (no metadata flag to exclude it).
- Fixed `c3po_bot.py`: added `_is_excluded_from_intro()` (excludes VGR-authored, cover letters, devlog, retrospectus, no-url definitions), `_find_mentioned_source()` (fuzzy word-overlap ≥0.6 threshold), updated corpus query to prefer non-VGR resources, up to 3 suggested reading links.
- Added `bin/intro_quality.py`: per-response quality checker — 6 check types (title_mismatch, no_title_match, usp_in_answer, vgr_paper_mentioned, short_answer, no_url_removed); auto-fixes no-URL sources; logs to `data/intro_quality_log.jsonl`.
- Added `bin/review_intro_quality.py`: session-start review tool — shows unreviewed issues with severity summary, `--mark-reviewed` to clear.
- Updated `CLAUDE.md` startup ritual to include quality review as step 4.

**Roam ingest plan — COMPLETE (plan only)**
- Inspected `c3po_inbox/ProtocolTheory-2026-06-17-14-32-07.json` (165 pages, 1.7MB SIGFPT Roam graph export).
- 14 rich meeting topic pages, 3 raw transcripts (skip), 52 empty daily stubs (skip), ~8 workshop pages, ~6 concept pages.
- Plan: `plans/roam-ingest.md` — ~56 vectors to `sig` namespace, new `ingest/sync_roam.py`, `data/roam_enrichments.json` for website enrichment.

**SIG meeting capture protocol — COMPLETE (design only)**
- Decided to deprecate Roam as capture format.
- Designed `plans/sig-meeting-capture.md`: YAML frontmatter + markdown per meeting in `Protocol-Institute/sig-notes` repo; c3po transcript processing pipeline (raw transcript + corpus context → Claude → enriched JSON → Pinecone); generalizes to all 6 SIGs.
- Pending: discussion with SIG hosts before implementation.

**Pinecone state: ~27,089 vectors** (no index changes this session)

**Open TODOs (priority order):**
1. Implement `ingest/sync_roam.py` (plan: `plans/roam-ingest.md`)
2. Create `Protocol-Institute/sig-notes` repo + `_template.md`; discuss with SIG hosts
3. Starter page — 28 recs across 20 resources in tally (threshold reached); build "good first reads" page + wire into intro handler
4. Execute VPS migration — `plans/vps-migration.md`
5. Exhibit extraction — `ingest/extract_structure.py`
6. `sync_sig_pages.py` + `update_sig_pages.py` — add to daemon (needs VPS first)
7. Anthropic key rotation to PI org account
8. Rotate `GH_PAT` to fine-grained PAT scoped to protocolized-website only
9. Phase E: multi-node swarm planning

---

## 2026-06-16 — GHA fix, 6 missing YouTube videos, PR #4 D1 migration (session 35, PT)

**GHA Substack sync fix — COMPLETE**
- `sync-substack-resources.py` line 261: backslash-escaped quote inside f-string expression — valid Python 3.12+ but breaks on 3.11 (CI runner). Fixed: `'\"A post...\"'` → `yaml_str('A post...')`, equivalent output.
- Triggered manual run; `jamverse-jam` ingested. substack: 1,101 → 1,106 (+5 vectors incl. american-skyway tag change).

**6 missing YouTube videos ingested — COMPLETE**
- Root cause: `fetch_youtube_meta.py` discovers videos only by walking the 10 tracked playlists; these 6 were either pre-playlist SoP-era standalones or guest talks never assigned to a playlist on YouTube. protocolized-website had them as manually-created resource stubs but c3po had never ingested them.
- Fixed `--video` flag to bootstrap a stub entry for playlist-orphaned videos, then fetched captions, Haiku-enriched, upserted.
- Videos: Atoms/Institutions/Blockchains, Punk/Folk/Myth/Protocols, Scaling Bitcoin (Lightning), Seeing SCP as Narrative Protocol, SoP Office Hours 0, SoP Town Hall.
- videos: 2,940 → 3,127 (+187; 91 → 97 videos)
- Synced enriched Markdown to protocolized-website; R2 thumbnails uploaded; D1 re-migrated (311 resources).

**PR #4 merged + D1 migration — COMPLETE**
- Merged `enriched_categories` column drop from protocolized-website D1 schema.
- Ran live migration: `ALTER TABLE posts DROP COLUMN enriched_categories;` — success.

**Pinecone state: ~27,089 vectors** (session start: 26,883; +206)

**Open TODOs (priority order):**
1. Starter page — 28 recs across 20 resources in tally (threshold reached); build "good first reads" page + wire into intro handler
2. SIG call transcript ingestion — plan ingest pipeline for meeting transcripts
3. Execute VPS migration — `plans/vps-migration.md`
4. Exhibit extraction — `ingest/extract_structure.py`
5. `sync_sig_pages.py` + `update_sig_pages.py` — add to daemon (needs VPS first)
6. Anthropic key rotation to PI org account
7. Rotate `GH_PAT` to fine-grained PAT scoped to protocolized-website only
8. Phase E: multi-node swarm planning

---

## 2026-06-15 — protocolized-website resource pipeline (session 34, PT)

**Resource pipeline — COMPLETE**
- Implemented c3po → protocolized-website enrichment pipeline across all three content types
- PDF + YouTube: daemon-driven, mtime-gated; detects when `enriched_meta.json` changes and runs sync scripts in protocolized-website, then pushes; GH Actions `sync-resources-d1.yml` handles D1 migration on push
- Substack: GH Actions-driven; c3po's `sync-substack.yml` now chains to protocolized-website after daily Substack sync, runs `sync-substack-resources.py`, pushes enriched resource Markdown
- Sync scripts (`sync-pdf-resources.py`, `sync-youtube-resources.py`, `sync-substack-resources.py`) updated to accept `C3PO_ROOT` env var override for use in GH Actions
- `sync-resources-d1.yml` (new GH Actions workflow in protocolized-website): auto-migrates D1 on any push to `src/content/resources/`
- `draft_resource.py` (new): intake helper that writes resource Markdown stubs for new PDFs not yet in protocolized-website
- `enrichment_sync_state.json` (new, gitignored): tracks mtime of enriched_meta files for daemon gating

**Race condition fixed — COMPLETE**
- protocolized-website's `sync-substack.py` was creating unenriched RSS-based resource stubs that conflicted with c3po's enriched stubs; both ran at 08:00 UTC with no coordination
- Fix: stripped all Markdown creation from `sync-substack.py`; it now only handles D1 posts table + R2 (its actual job); state tracked in `.substack-sync-state.json` keyed by slug, independent of Markdown file existence
- 135 existing slugs bootstrapped into state file on first run

**enriched_categories PR — OPEN**
- PR #4 in protocolized-website: removes vestigial `enriched_categories` column from D1 posts table (always `[]`, never read); includes one-time migration command

**Keys**
- `CLOUDFLARE_API_TOKEN` added to c3po GH Actions secrets
- `GH_PAT` added to c3po GH Actions secrets (currently using CLI OAuth token — flag for rotation to fine-grained PAT scoped to protocolized-website)
- `GH_PAT` registered in `admin/keys.md`
- `.env.keys` Dropbox ignore attribute was lost — re-applied

**Pinecone state: ~26,881 vectors** (daemon activity since session 33; no manual ingest this session)

**Open TODOs (priority order):**
1. **SIG call transcript ingestion** — plan ingest pipeline for meeting transcripts from SIG calls (next session)
2. Execute VPS migration — `plans/vps-migration.md`
3. Starter page — tally at 25 recs, threshold reached (~20)
4. Exhibit extraction — `ingest/extract_structure.py`
5. `sync_sig_pages.py` + `update_sig_pages.py` — add to daemon (needs VPS first)
6. Anthropic key rotation to PI org account
7. Rotate `GH_PAT` to fine-grained PAT scoped to protocolized-website only
8. Merge PR #4 (enriched_categories drop) + run D1 migration
9. Phase E: multi-node swarm planning

---

## 2026-06-13 — Personal infra decommission (session 33, PT)

**Personal CF Worker deleted — COMPLETE**
- Removed `c3po` worker from personal CF account (`Vgr@ribbonfarm.com`, ID `7026b5d7c1ad16cb808987576bb07ab2`)
- Had to first remove the queue consumer binding on `c3po-oracle` queue via API (wrangler delete blocked otherwise)
- Also deleted the orphaned `c3po-oracle` queue on personal account
- Used wrangler OAuth token at `~/Library/Preferences/.wrangler/config/default.toml` with `CLOUDFLARE_ACCOUNT_ID=7026b5d7c1ad16cb808987576bb07ab2`

**Personal Pinecone `c3po` index deleted — COMPLETE**
- Deleted `c3po` index from personal Pinecone account using `PINECONE_PERSONAL_API_KEY`
- Remaining indexes on personal account: `contraptions`, `ribbonfarm`, `vgr-books`, `vgr-twitter` (untouched)

**Pinecone state: ~26,748 vectors** (daemon + 6 new SIG meeting pages this session)

**Open TODOs (priority order):**
1. Execute VPS migration — `plans/vps-migration.md` (blocks daemon→website push + `update_sig_pages.py` + `sync_sig_pages.py` daemon wiring)
2. Starter page — tally at 25 recs, threshold reached (~20)
3. Exhibit extraction — `ingest/extract_structure.py`
4. `sync_sig_pages.py` + `update_sig_pages.py` — add to daemon (needs VPS first)
5. Anthropic key rotation to PI org account
6. Phase E: multi-node swarm planning

---

## 2026-06-08 — Returning-member welcome, /how-it-works rewrite (session 32, ~10:47–13:17 PT)

**Returning-member welcome path — COMPLETE**
- `NEW_MEMBER_DAYS` raised 30→60 in `bin/c3po_bot.py` and `bin/seed_welcome_queue.py`. Diagnosed via two missed intros: Shreeram (39 days, slipped past old threshold) and Rob Knight (801 days, old member reintroducing). Maxwell's intro from Jun 7 was confirmed sent via session log (`sent: true`, 5.7s latency).
- New `handle_introduction(returning=True)` path: members who joined >60 days ago and post ≥80 chars in #introductions get "Hi @user — looks like you joined a while back and are getting more active. Welcome back!" followed by the same corpus rec + channel rec.
- Returning-member path not queued (welcome queue is for critical first-time welcomes only).
- Restarted bot; seeder queued and successfully sent Shreeram's welcome (DRG rec, latency 7s).
- Rob Knight's already-posted intro left for manual follow-up.

**ARCHITECTURE.md updated — COMPLETE**
- Fixed inverted/stale threshold description in introductions section (30→60 days, new returning path documented)
- Added missing daemon steps 13 (sync_devlog) + 14 (generate_devlog_page)
- Updated Pinecone counts to current (26,341)
- Added session 32 to build history

**/how-it-works page — FULL REWRITE — COMPLETE**
- Rewrote from 5 sections to 8: added ingest pipeline pattern (3-layer + new-source checklist), query pipeline step-by-step (8 steps), delivery interfaces (web/Discord/MCP split out)
- All corpus counts current; all 11 namespaces documented with correct weights
- Discord bot section added: @mention, nav queries, introductions handler (both paths), slash commands, spool pattern
- System prompt accurately described as inline 7-section document; SOUL.md stale-reference removed
- Repo link fixed (vgururao → Protocol-Institute); dev status links to public devlog
- Deployed: version `fb576704`

**Pinecone state: ~26,341 vectors** (daemon activity: +73 since session 31; no manual ingest this session)

**Open TODOs (priority order):**
1. Delete personal CF Worker (`c3po` on `vgr-702`) — overdue
2. Delete personal Pinecone index — c3po confirmed safe
3. Execute VPS migration — `plans/vps-migration.md`
4. Build c3po tracking dashboard
5. Starter page — ~18 welcome events logged; needs ~20
6. Exhibit extraction — `ingest/extract_structure.py`
7. `sync_sig_pages.py` — add to daemon
8. Anthropic key rotation to PI org account
9. Phase E: multi-node swarm planning

---

## 2026-06-06 — Discord bot fixes, SIGPSY+DRG onboarded, pre-deletion cleanup (session 31, ~10:00–13:30 PT)

**Discord bot fixes — COMPLETE**
- 5-turn thread cap: cap notice now sent exactly once; subsequent messages silently ignored (was re-sending the cap message on every new message)
- Side-conversation filtering: messages that are replies to another human (not the bot) are now ignored unless bot is explicitly @mentioned — prevents bot from responding to conversations between members in its own threads
- Intro suggested-reading coherence: `rec_sources` now uses title-match scan against Claude's answer text to find the source Claude actually recommended, rather than picking independently from rank order. Fallback to rank order if no title match found. (Root cause: answer and suggested-reading link were independently derived — Claude would say "Unprotocolized Knowledge" but the link would show a different resource)

**SIGPSY + DRG onboarded — COMPLETE**
- SIGPSY (#⏳-psychohistory, 1508205168661893180): 65 vectors (1 meeting "Kickoff 4 Jun 2026" + 13 discussions including World Machines book club + main channel); biweekly Thursdays 16:00 UTC
- DRG (#🦾-distributed-robotics, 1508175637020676259): 43 vectors (5 threads + main channel, 0 meetings yet); biweekly Thursdays 15:30 UTC; first meeting next week
- Both added to `channel_manifest.json`, `generate_sig_pages.py`, `rebuild_sig_summaries.py`, `sync_sig_pages.py`
- `sync_discord_channels.py` fixed to auto-seed `sig_display` from manifest for sig-type channels (was causing SIGPSY event to be unmatched in calendar sync)
- `generate_sig_pages.py` now writes both `sigs/{slug}.html` and `sigs/{slug}/index.html` (absolute-path clean-URL format); fixes all existing SIG pages to stay in sync
- Website pages published for both; SIGPSY shows kickoff meeting card; DRG shows "no meetings yet"

**Personal account pre-deletion cleanup — COMPLETE**
- `config/sink_registry.json`, `ingest/sync_web_chats.py`: updated from `c3po.vgr-702.workers.dev` → `c3po.protocolized.io`
- `config/corpus_map.json`: updated index host from personal (`c3po-bwo39z7`) to PI org (`c3po-1os2tli`)
- Confirmed: personal CF Worker safe to delete (all code + secrets + KV + queue fully on PI org)
- Confirmed: personal Pinecone index safe to delete from c3po's perspective (all data on PI org index); humboldt's dependency noted but treated as out of scope for this project
- ARCHITECTURE.md, CLAUDE.md updated for session 31

**Pinecone state: ~26,268 vectors** (sig: 5,311 +218; discord_links: 9,640 +383 from daemon; transcripts: 22 +10; meta: 31 +2)

**Open TODOs (priority order):**
1. Delete personal CF Worker (`c3po` on `vgr-702`) — overdue
2. Delete personal Pinecone index — c3po confirmed safe
3. Execute VPS migration — `plans/vps-migration.md`
4. Build c3po tracking dashboard — what SIGs, channels, namespaces, sources are being tracked; vector counts; daemon health; last-sync timestamps
5. Starter page — tally at 18 events, needs ~20
6. Exhibit extraction — `ingest/extract_structure.py`
7. `sync_sig_pages.py` — add to daemon
8. Anthropic key rotation to PI org account
9. Phase E: multi-node swarm planning

---

## 2026-06-03 — VPS migration plan (session 30, ~18:00–19:30 PT)

**Infrastructure planning — COMPLETE**
- Assessed options for moving Discord bot gateway off laptop: Cloudflare Workers can't hold a persistent gateway connection; Railway and VPS are the viable options
- Surveyed all PI org projects: only c3po and humboldt have persistent process needs; protocolized-website and website are fully static/edge
- Decided Hetzner CX22 VPS (€3.29/mo) over Railway: colocation of bot + daemon means spool files remain local (no transport redesign), cross-repo git pushes work naturally, cheaper for 4 always-on processes
- Removed Humboldt dependency items from c3po TODO list — those are humboldt's responsibility
- Wrote `plans/vps-migration.md`: 6-phase plan covering server setup, deploy keys, c3po migration (absorbing GHA substack workflow into daemon), humboldt migration, GHA retirement, and personal infra decommission

**Pinecone state: ~25,772 vectors** (unchanged this session)

**Open TODOs (priority order):**
1. Delete personal Cloudflare Worker (`c3po` on `vgr-702`) — window passed 2026-06-07
2. Delete personal Pinecone index
3. Execute VPS migration — see `plans/vps-migration.md`
4. Anthropic key rotation to PI org account — deferred
5. Starter page — tally needs ~20 welcome events before building
6. Exhibit extraction — `ingest/extract_structure.py` (plan in `plans/structural-navigation.md`)
7. `sync_sig_pages.py` — add to GitHub Actions cron (or VPS daemon step once migrated)
8. Phase E: multi-node swarm planning

---

## 2026-06-01 — Devlog ingest pipeline + personality split + architecture doc (session 29, ~14:00–17:30 PT)

**Discord/web personality split — SHIPPED**
- `DISCORD_SYSTEM_PROMPT`: appends `DISCORD VOICE OVERRIDE` block to base prompt — 2–3 sentence max, office-manager tone, one named resource
- `runRagQuery` reads `context` opt; `/query` handler passes `context="discord"` from request body; queue handler hardcodes `"discord"` for slash commands
- Both prompts exceed 1024-token cache threshold — cache independently

**Devlog ingest pipeline — SHIPPED**
- `ingest/sync_devlog.py`: 29 session vectors → Pinecone `meta` namespace; idempotent, content-hash state; `--dry-run`/`--force`
- `ingest/generate_devlog_page.py`: renders devlog JSON → markdown with `<a id="session-{id}">` anchors; upserts to D1 slug `c3po-devlog` (protocolized.io/resources/c3po-devlog); idempotent; `--dry-run`/`--local`/`--force`
- `worker.js`: `normalizeDevlog()` added; `meta` namespace queried (top 3) in all 3 RAG paths; `mergeResults` extended to 11 params; `devlog` case in `buildContextBlock`
- `bin/daemon.py`: steps 13 (`sync_devlog`) + 14 (`generate_devlog_page`) added
- Both scripts ran successfully: 29 vectors live in Pinecone `meta`; 65,609-char page live at protocolized.io/resources/c3po-devlog
- Worker deployed: version `5a7fc01f`

**Architecture doc — COMPLETE (prior sub-session)**
- `ARCHITECTURE.md` (c3po root): consolidated from stale `ARCHITECTURE.md` + `plans/bot-ecology.md`; covers current state + vision; includes full devlog history table, namespace table, personality split section, design principles, priority queue

**.org website C3PO page — UPDATED (prior sub-session)**
- `website/c3po/index.html`: updated corpus counts, added Discord/MCP sections, fixed copyright year

**Bug fix: generate_devlog_page.py CF token path**
- Fallback path was `Code/.env.keys` — PI tokens are in `protocol-institute/.env.keys`; fixed to `Path(__file__).parent.parent.parent / ".env.keys"`

**Bug fix: generate_devlog_page.py --remote flag**
- Wrangler 4.x defaults to `--local`; script now passes `--remote` unless `--local` flag set

**Pinecone state: ~25,614 vectors** (meta namespace added: 29; other namespaces grew normally)

**Open TODOs (priority order):**
1. Delete personal Cloudflare Worker (`c3po` on `vgr-702`) — 1-week window passed 2026-06-07
2. Delete personal Pinecone index
3. Anthropic key rotation to PI org account — deferred
4. Starter page — tally needs ~20 welcome events before building
5. Exhibit extraction — `ingest/extract_structure.py` (plan in `plans/structural-navigation.md`)
6. `sync_sig_pages.py` — add to GitHub Actions cron
7. Phase E: multi-node swarm planning

---

## 2026-05-31 — PDF URL fix + cover-letter filter (session 28, ~12:00 PT)

**PDF URL bug — FIXED (systemic)**
- Root cause: `ingest_pdfs.py` `load_resource_metadata()` regex matched `/resources/{file}` but all website resource files now use `file: "https://files.protocolized.io/..."` — regex never matched, all PDFs got `/resources/` fallback URL which expanded to `https://protocolized.io/resources/` (404s everywhere)
- Fix: updated regex to match `files.protocolized.io` URLs; fallback also changed to `files.protocolized.io`
- Ran `ingest/fix_pdf_urls.py`: updated 487 vectors in Pinecone `pdfs` namespace to canonical `https://files.protocolized.io/` URLs (263 already correct, 1 external URL left alone)
- Worker `normalizePdf()` URL expansion also fixed (was building `protocolized.io/resources/` from relative URLs)
- Worker secondary PDF body-chunk filter was hardcoded to `/resources/` format — now constructs from `files.protocolized.io`

**Cover-letter PDF filter — FIXED**
- 11 PDFs marked `deprecated: true` in `sources/pdfs/enriched_meta.json` (5 PI cover letters, title page, 2 appendix/starproject covers, blank handout, 2 duplicate `-1` versions)
- These vectors were absent from the PI Pinecone index already (migration gap)
- `ingest_pdfs.py` now skips deprecated PDFs on future runs
- `handle_introduction()` now filters `is_cover_letter` sources from rec_sources (alongside VGR filter)
- Cover letters remain retrievable for general meta queries — only excluded from new-member recs

**Intro handler hotfix (post-session 27, direct commit d6f7218) — DOCUMENTED**
- Forward-only watermark, new-member filter (>30 day join check), mention passthrough — were in code but missing from devlog; now recorded as session 27.5

**Pinecone state: ~25,965 vectors** (pdfs namespace corrected: 800 → 750 after audit; 50 cover-letter vectors absent from PI migration)

**Open TODOs (priority order):**
1. Delete personal Cloudflare Worker (`c3po` on `vgr-702`) — after 1-week verification window
2. Delete personal Pinecone index — after confirming humboldt project updated to its own key path
3. Anthropic key rotation to PI org account — deferred
4. Voyage humboldt key — create in PI Voyage account, wire into humboldt project
5. Starter page — tally needs ~20 welcome events before building
6. Exhibit extraction — `ingest/extract_structure.py` (plan in `plans/structural-navigation.md`)
7. `sync_sig_pages.py` — add to GitHub Actions cron
8. Phase E: multi-node swarm planning

---

## 2026-05-31 — Full infrastructure migration to PI org accounts (session 27, ~09:00–11:00 PT)

**Substack sync workflow bug — FIXED**
- `sync-substack.yml` was failing in 21s daily: script hit `from bs4 import BeautifulSoup` on new posts, printed "Install beautifulsoup4", exited 1
- Fix: added `beautifulsoup4` to the pip install step
- New post `irrigation-by-protocol-when-vineyards` ingested locally (8 vectors) — GHA will catch it tonight

**Cloudflare Worker migration — COMPLETE**
- KV namespace `C3PO_KV` and queue `c3po-oracle` created in PI CF account (`team-7e8`)
- All 9 `c3po.vgr-702.workers.dev` URL references in `worker.js` replaced with `c3po.protocolized.io`
- Worker deployed to PI account; custom domain `c3po.protocolized.io` provisioned on protocolized.io zone (CF auto-SSL)
- All 10 secrets set on PI worker: VOYAGE, PINECONE, PINECONE_HOST, ANTHROPIC, ADMIN, MCP, DISCORD_BOT, ORACLE_BOT, ORACLE_APP_ID, ORACLE_PUBLIC_KEY
- `wrangler.toml` KV namespace ID updated to PI namespace
- Routing decision: subdomain (`c3po.protocolized.io`) preferred over path (`protocolized.io/c3po`) — c3po is a full multi-route web app, already linked externally, clean infrastructure separation

**GitHub repo transfer — COMPLETE**
- `vgururao/c3po` → `Protocol-Institute/c3po` (manual GitHub UI transfer)
- Local git remote updated; verified push to new origin

**Pinecone migration — COMPLETE**
- New `ingest/migrate_pinecone.py`: list+fetch+upsert without re-embedding; idempotent
- Migrated 25,547 vectors across 10 namespaces (humboldt excluded — owned by humboldt project)
- Two bugs fixed during migration: `list()` returns `ListItem` objects not strings; FETCH_BATCH reduced 200→50 to avoid 414 URI Too Large
- `PINECONE_API_KEY` and `PINECONE_C3PO_HOST` updated in `.env`, CF Worker secrets, GitHub Actions secrets
- Live worker verified against PI index

**Voyage AI migration — COMPLETE**
- PI Voyage AI account created; per-app key strategy adopted (`c3po` key; `humboldt` key pending, separate task)
- `VOYAGE_API_KEY` updated in `.env`, CF Worker secrets, GitHub Actions secrets
- Personal key deprecated in `.env.keys`

**Reference updates — COMPLETE**
- `protocolized-website`: resources page link updated
- `protocol-institute.org`: programs page, c3po project page (URL, vector count 12k→25k+, namespace count 5→10, GitHub link), sigpfb page
- `admin/keys.md`: all new keys and Worker secrets documented; Pinecone + Voyage marked as org-owned

**Pinecone state: ~26,015 vectors** (PI org index; +8 substack from irrigation-by-protocol-when-vineyards)

**Open TODOs (priority order):**
1. Delete personal Cloudflare Worker (`c3po` on `vgr-702`) — after 1-week verification window
2. Delete personal Pinecone index — after confirming humboldt project updated to its own key path
3. Anthropic key rotation to PI org account — deferred
4. Voyage humboldt key — create in PI Voyage account, wire into humboldt project
5. Starter page — tally needs ~20 welcome events before building
6. Exhibit extraction — `ingest/extract_structure.py` (plan in `plans/structural-navigation.md`)
7. `sync_sig_pages.py` — add to GitHub Actions cron
8. Phase E: multi-node swarm planning

---

## 2026-05-30 — Phase D + welcome queue + intro recs overhaul (session 26, 08:30–11:02 PT)

**Welcome queue — COMPLETE**
- `bin/welcome_queue.py`: persistent FIFO queue (message_id-keyed, idempotent push, max 3 attempts)
- `bin/seed_welcome_queue.py`: fetches #introductions REST API, finds posts with no bot reply, seeds queue
- `c3po_bot.py`: `on_ready` drains queue at each epoch; live intros push→process→pop on success
- First run: 5 queued (twee-i-double-g, nobo, bubbly_lemur_55426, Snezana/Nonsnens, Malicєnt); all 5 welcomed successfully
- `data/welcome_queue.json` is gitignored (runtime state); seeder re-populates from Discord API as needed

**Intro recs overhaul — COMPLETE**
- `_is_vgr_authored()`: filters sources by primary_author/authors for "venkatesh"; draws from top 8 sources
- `_INTRO_FALLBACK_SRC`: Summer of Protocols Reader as last-resort when all corpus hits are VGR-authored
- `_update_intro_tally()`: running tally of recommended resources + channels in `data/intro_recs_tally.json`
- Session log now records `sources_seen`, `recs_shown`, `used_fallback` per welcome
- Future: tally → curated starter page (head) + intro handler uses starter page + 1 long-tail pick

## 2026-05-30 — Phase D + Phase 4 nav queries + GitHub Actions cron (session 26)

**YouTube pass — already done by daemon**
- Status check: 17 YouTube URLs successfully fetched via transcript API; 156 failed (no transcripts); 24 filtered irrelevant
- 260 deferred URLs remaining are all Twitter/X (184 x.com + 76 twitter.com) — needs paid API

**Phase D — Bot registry + swarm scaffolding — COMPLETE**
- `config/bot_registry.json`: formal node registry (c3po_listener, c3po_bot, c3po_web)
- `bin/session_log.py`: shared append helper — DRY across all bots
- `bin/daemon.py` + `c3po_bot.py`: import shared session_log, replaced local log functions
- `ingest/generate_monitoring_page.py`: Bot Nodes section reads session logs, shows status/last-active/today

**Phase 4 — General Discord nav queries — COMPLETE**
- `NAV_RE` regex: detects "where should I post about X?" intent in @mentions
- `handle_nav_query()`: queries discord_guide without newcomer filter, formats top 3 channels with section, SIG cadence, blurb
- `query_discord_guide_nav()`: nav-mode query (active channels only, no newcomer filter)
- `on_message`: routes nav-intent queries to handle_nav_query before corpus RAG path
- Bot restarted with new code (PID via launchd, log confirmed clean)

**GitHub Actions cron for sync_substack.py — COMPLETE**
- `.github/workflows/sync-substack.yml`: daily at 08:00 UTC, manual trigger enabled
- Secrets set: VOYAGE_API_KEY, PINECONE_API_KEY, PINECONE_C3PO_HOST, ANTHROPIC_API_KEY
- Verified: manual run succeeded end-to-end (all steps ✓, state files committed back)
- Node.js 20 deprecation warning: no action needed until Sep 2026

**Pinecone state: ~25,960 vectors** (humboldt +362 from humboldt project activity; discord_links +12 from daemon)

**Open TODOs (priority order):**
1. Starter page — once tally has ~20 welcome events, compile `data/intro_recs_tally.json` into a curated "good first reads" page; update intro handler to use starter page + 1 long-tail pick
2. Exhibit extraction — `ingest/extract_structure.py` for PDF section summaries + list exhibits (plan in `plans/structural-navigation.md`)
3. `sync_sig_pages.py` — add to GitHub Actions cron once page format stabilizes
4. Phase E: multi-node swarm planning

---

## Bot processes — launchd-managed (Phases A+B+C complete 2026-05-28)

Both bots managed by launchd (`KeepAlive`, `RunAtLoad`). Auto-restart on crash or reboot.

| Bot | Label | Log |
|-----|-------|-----|
| `c3po_listener` | `org.protocol-institute.c3po.daily` | `~/Library/Logs/c3po/daemon.log` |
| `c3po_bot` | `org.protocol-institute.c3po-bot` | `~/Library/Logs/c3po/c3po_bot.log` |

```bash
launchctl list | grep protocol-institute          # check status
launchctl unload ~/Library/LaunchAgents/org.protocol-institute.c3po-bot.plist
launchctl load   ~/Library/LaunchAgents/org.protocol-institute.c3po-bot.plist
```

## 2026-05-29 — Discord guide Phase 3: intro handler + scheduled events (session 25, cont.)

**Discord events sync — COMPLETE**
- `ingest/sync_discord_events.py`: fetches guild scheduled events, decodes recurrence_rule into human-readable cadence, matches events to channels via sig_display/event_keywords/keyword overlap
- Dual-event fix: when multiple events match same channel (SIGPfB main + optional), keeps highest user_count as primary; secondary events stored in `secondary_events` field
- 6 events fetched, 5 unique channels matched (MRG, SIGFPT, SIGPfB, ProtFiSIG, SIGPSY); 0 unmatched
- `daemon.py` step 2: sync_discord_events runs after channels, before discord
- `sync_discord_channels.py`: preserves `next_event_*` fields across rebuild cycles; includes `next_event_time` in embed text and Pinecone metadata
- `c3po_bot.py` intro handler: shows "next meeting: Fri 29 May 17:00 UTC" instead of just cadence string
- Worker deployed: Version `c755bd9b` (also fixes query limit 500→2000 chars from session 25 intro)

**Also this session:**
- `humboldt-notebook.html` was 404ing on protocol-institute.org — website migrated to clean URLs but humboldt still published to the flat path. Fixed: (1) added JS redirect at `humboldt-notebook.html` preserving hash fragments; (2) `publish.py` now writes to `humboldt-notebook/index.html`; (3) `notebook_index.py` URL base updated to `/humboldt-notebook/`
- Humboldt person notebook entries (`notebook/people/`) were being posted to Discord as if they were date entries — they're private mental models of collaborators. Deleted 3 mistaken posts (4umd, _vgr, boredgargoyle); stripped from `index.yaml`; `notebook_watcher.py` now guards on YYYY-MM-DD stem format + excludes `people/` subdir

**Pinecone state: ~25,411 vectors** (5 SIG channels re-embedded in discord_guide)

**Open TODOs (priority order):**
1. YouTube community links pass — `python3 ingest/fetch_discord_links.py --youtube-only` then `enrich_discord_links.py`
2. Phase D: `config/bot_registry.json` + shared session-log helper + monitoring page bot statuses
3. Phase 4: General Discord nav queries (using discord_guide for "where should I post about X?")
4. GitHub Actions cron for `sync_substack.py`
5. Snezana's intro (12:11 today) got no reply — Worker 400 at time of post. Consider a manual welcome.

---

## 2026-05-28 — Warm-cache hits + Phases B+C wrap-up (session 24, cont.)

**Transcript warm-cache — COMPLETE**
- Worker: `CHAT_PUBLIC_BASE = "https://protocolized.io/chats"` (one constant to update at migration)
- `TRANSCRIPT_CACHE_THRESHOLD = 0.52` — calibrated against actual Voyage-3 Q+A pair scores (near-duplicate ~0.62, unrelated ~0.25)
- `mergeResults`: tiered weight boost for transcript hits (score ≥ 0.60 → 1.10×, ≥ 0.52 → 0.92×, else 0.80×)
- `runRagQuery` + `runMcpAsk`: extract `cache_hits` (high-score transcript items with URL); strip transcripts from regular `sources`; include `cache_hits` in response
- POST `/query` handler: destructures and forwards `cache_hits` to client
- Discord bot `send_answer`: shows "**Similar conversation:** `<url>`" before answer when cache hit present; suppresses transcript from Sources block
- Worker deployed: Version `6b7029a9`; tested live — AI adoption query surfaces `bxi03c`

**YouTube community links — READY TO RUN**
- 175 deferred YouTube URLs in `discord_links_registry.json` (community-shared external videos)
- `fetch_discord_links.py --youtube-only` already handles this; `youtube-transcript-api` needs install check
- PI's own 91 videos are fully ingested (2,940 vectors in `videos` namespace) — these are external
- Interrupted before running; next session: install check + run + enrich pass

**Open TODOs (priority order):**
1. YouTube community links pass — `python3 ingest/fetch_discord_links.py --youtube-only` then `enrich_discord_links.py`
2. Phase D: `config/bot_registry.json` + shared session-log helper + monitoring page bot statuses
3. GitHub Actions cron for `sync_substack.py`
4. `sync_sig_pages.py` — add to GitHub Actions cron once page format stabilizes

---

## 2026-05-28 — Bot ecology Phases B+C: Discord + web conversation self-memory (session 24)

**Discord conversation spool — COMPLETE**
- `c3po_bot.py`: `spool_conversation()` writes `data/spool/bot_conversations/{thread_id}_{turn}.json` after each Q&A exchange (both initial mention and thread replies)
- `ingest/sync_bot_conversations.py`: reads spool, embeds Q+A as `chunk_type=discord_conversation`, upserts to `transcripts` namespace, deletes file
- `daemon.py`: step 9 added — runs `sync_bot_conversations.py` each cycle
- Worker: `normalizeTranscript()` normalizes both `discord_conversation` (C3PO-BOT) and `web_conversation` (C3PO-WEB); weight 0.85×; added to all three RAG paths (`runRagQuery`, `runMcpAsk`, and the standalone `/search` endpoint uses `[]` since it's sources-only)
- Worker deployed: Version `40e37f1b`

**Web chat ingest — COMPLETE (Phase C)**
- `ingest/sync_web_chats.py`: polls `/api/chats` (admin), fetches each public chat from `/api/chat/{id}`, embeds full Q+A, upserts as `chunk_type=web_conversation`; state in `data/web_chats_state.json`
- 4 existing public conversations ingested on first run
- Step 10 added to daemon.py cycle
- Worker already handled `web_conversation` via `normalizeTranscript()` from Phase B

**Pinecone state: 25,411 vectors**
- transcripts: **8** (+4 web_conversation) | discord_links: 9,108 | discord: 5,538 | sig: 5,028 | videos: 2,940 | substack: 1,057 | pdfs: 800 | definitions: 560 | bibliography: 278 | humboldt: 94 (aware)

**Open TODOs (priority order):**
1. Phase D: `config/bot_registry.json` + shared session-log helper + monitoring page bot statuses
2. YouTube transcript pass — 161 deferred URLs
3. GitHub Actions cron for `sync_substack.py`
4. `sync_sig_pages.py` — add to GitHub Actions cron once page format stabilizes

---

## 2026-05-28 — PDF URL fix, answer length fix, SIG meeting page ingest, bot ecology Phase A (session 23)

**PDF URL bug — FIXED**
- `normalizePdf()` was prepending `https://protocolized.io` to any `m.url`, including already-absolute URLs (e.g., `https://ai.protocolized.dev/`). Added `startsWith("http")` check. Bug was visible in chat `bxi03c`.

**Answer length cutoff — FIXED**
- `max_tokens` raised 1200 → 2000 on main query path and MCP ask path
- Added system prompt instruction: complete current paragraph rather than truncate mid-sentence
- Previous answer in `bxi03c` cut off mid-word; now produces complete 4400-char responses

**SIG meeting page ingest — COMPLETE**
- New script: `ingest/sync_sig_pages.py`
- Crawls all 91 meeting pages from `protocol-institute.org/sigs/` (SIGFPT: 33, SIGPfB: 28, MRG: 16, ProtFiSIG: 14)
- Embeds as `chunk_type=sig_meeting_page` in `sig` namespace; metadata: `meeting_title`, `meeting_date`, `participants`, `url` (absolute .org URL), `sig_display`
- State file: `data/sig_pages_state.json` (gitignored); incremental via content hash
- Worker: `normalizeSig()` handles new type; tier weight 0.90×; parallel filtered Pinecone query ensures meeting pages surface even when ranked below TOP_K_EACH in general sig query
- Deployed: Version `41fd2d1f`; verified: 3 of 5 sig sources now have `.org` meeting page URLs for AI adoption query

**Pinecone state: 25,406 vectors**
- sig: **5,027** (+95: 91 meeting pages + 4 sig) | discord_links: 9,108 | discord: 5,538 | definitions: 560 | videos: 2,940 | substack: 1,057 | pdfs: 800 | bibliography: 278 | transcripts: 4 | humboldt: 94 (aware)

**Bot ecology Phase A — COMPLETE**
- Architecture documented in `plans/bot-ecology.md`: multi-node pubsub swarm with spool pattern, Phases A–E roadmap
- `c3po_bot.py`: per-conversation session logging to `~/Library/Logs/c3po/bot_sessions.jsonl`
- `daemon.py`: per-cycle session logging to `~/Library/Logs/c3po/daemon_sessions.jsonl`
- `org.protocol-institute.c3po-bot.plist`: c3po_bot now launchd-managed (KeepAlive, RunAtLoad, .venv python)
- Both bots verified running: `launchctl list | grep protocol-institute`

**Open TODOs (priority order):**
1. Phase B: `c3po_bot.py` spool output + `ingest/sync_bot_conversations.py` — Discord conversation self-memory
2. Phase C: `ingest/sync_web_chats.py` — web chat self-memory
3. Phase D: `config/bot_registry.json` + shared session-log helper + monitoring page bot statuses
4. YouTube transcript pass — 161 deferred URLs
5. GitHub Actions cron for `sync_substack.py`
6. `sync_sig_pages.py` — add to GitHub Actions cron once page format stabilizes

## 2026-05-28 — Pubsub refactor Phase 1: registry layer + BaseSource ABC (session 22)

**Architecture pivot — COMPLETE**
- c3po reframed as a pubsub-style knowledge broker for all PI archival corpus needs
- Three ownership tiers: `owned` (c3po creates+maintains), `subscribed` (another PI project owns), `aware` (external/federated, no pipeline)

**Registry layer — COMPLETE**
- `config/source_registry.json` — 11 sources (10 owned, 1 aware: humboldt)
- `config/sink_registry.json` — 7 sinks (3 active: web_ui, discord_bot, mcp; 4 planned)
- `config/corpus_map.json` — 10 namespaces with ownership tags, vector counts, query weights

**BaseSource ABC — COMPLETE**
- `ingest/base.py` — abstract interface: `run()`, `status()`, `supports_incremental()`
- `ingest/sources/` — 8 source stubs (subprocess wrappers over existing scripts): substack, discord, sig, youtube, pdfs, bibliography, definitions, discord_links
- All imports verified clean; `REGISTRY` dict maps source_id → class

**Durable AI Adoption guide ingested — COMPLETE**
- Downloaded `durable-ai-adoption.pdf` (14MB, 53 pages) from `https://ai.protocolized.dev/`
- Added enriched_meta entry with full summary, categories, URL, authors, tags
- Patched `ingest_pdfs.py` to prefer enriched_meta fields (title/date/doc_type/tags/url) when no protocolized-website markdown exists — works for external web resources going forward
- Ingested 34 vectors (33 body + 1 doc_summary) → pdfs: 766 → 800

**protocol-institute.org website — COMPLETE**
- Added new "Resources" section to `sigs/sigpfb/index.html` (new section, distinct from meeting archive)
- First entry: *Durable AI Adoption* with description, topic tags, C3PO chat link
- Committed via parallel agent in website repo

**Pinecone state: 25,311 vectors**
- definitions: 560 | discord_links: 9,108 | discord: 5,538 | sig: 4,932 | videos: 2,940 | substack: 1,057 | **pdfs: 800** | bibliography: 278 | transcripts: 4 | humboldt: 94 (aware)

**Open TODOs (priority order):**
1. Phase 2: FastAPI orchestrator app (`orchestrator/app.py`) — ingest endpoints + APScheduler
2. Phase 2: Inbox watcher (`orchestrator/inbox_watcher.py`) — watchdog on `data/inbox/`
3. Phase 3: CF integration — D1 for ingest state, Cron Trigger → Worker → orchestrator endpoint
4. YouTube transcript pass — 161 deferred URLs
5. GitHub Actions cron for `sync_substack.py`
6. `ai.protocolized.dev` is periodically updated — add to scheduled re-ingest once orchestrator is running

## 2026-05-27 — c3po_bot: thread continuation + introductions monitoring (session 21)

**Thread continuation — COMPLETE**
- Bot now responds to any follow-up in bot-owned threads (no @mention needed), up to MAX_THREAD_TURNS=5
- Fetches full thread history; recovers original trigger query from parent channel (thread.id == message.id in Discord)
- Builds `history[]` of `{role, content}` and passes to Worker for multi-turn RAG context
- Stops gracefully at turn 5 with a redirect to the web UI

**Introductions monitoring — COMPLETE**
- Monitors `INTRODUCTIONS_CHANNEL_ID=1082504762433490975` (#introductions)
- Skips replies (only top-level posts); calls Worker with intro text + SIG menu
- SIG channel mentions use Discord's `<#ID>` format so they render as clickable links
- Replies with greeting + reading recommendation + SIG suggestion

**Bot restarted — PID 37015** (14:54 PT), log at `/tmp/c3po_bot.log`
- PID block added to top of status.md for easy reference going forward

**Open TODOs (priority order):**
1. YouTube transcript pass — 161 deferred URLs
2. GitHub Actions cron for `sync_substack.py`
3. Discord link farming — run `fetch_discord_links.py` + `enrich_discord_links.py` for pending URLs

## 2026-05-27 — Definitions namespace live; Substack synced (session 20)

**Definitions namespace wired into query pipeline — COMPLETE**
- `normalizeDefinition()` added; queries `definitions` namespace in parallel with the other 7
- All 4 query paths updated: `runRagQuery()`, `runMcpSearch()`, `runMcpAsk()`, `POST /search_corpus`
- Context block label: `[LEXICON — "term" — PI-coined | PI-specific]`
- Weight: 1.0× (same as PDFs/Substack — authoritative PI vocabulary)
- MCP `search_corpus` now accepts `"definitions"` as a namespace filter
- Deployed: `c3po.vgr-702.workers.dev` (Version ID: 617c215a)
- Smoke tested: lexicon hits surface correctly for relevant queries

**Substack sync — COMPLETE**
- Ingested `the-overloaded-train` (new) + `introducing-the-protocol-institute` (edited)
- 31 vectors upserted; substack: 1,040 → 1,057

**c3po-oracle Cloudflare Queue created (oracle bot Step 2 done)**
- Queue creation was blocking worker deploy; created it now
- Remaining oracle bot steps: Discord app creation (Step 1), secrets (Step 3), Interactions URL (Step 5), register commands (Step 6), invite bot (Step 7)

**Pinecone state: 25,184 vectors**
- definitions: 560 | discord_links: 9,064 | discord: 5,533 | sig: 4,905 | videos: 2,940 | substack: 1,057 | pdfs: 766 | bibliography: 278 | transcripts: 4 | humboldt: 77 (ignored)

**Open TODOs (priority order):**
1. Execute oracle bot setup — Steps 1, 3–8 remain (see `plans/oracle-bot-setup.md`)
2. YouTube transcript pass — 161 deferred URLs
3. GitHub Actions cron for `sync_substack.py`

## 2026-05-26 — c3po_oracle Discord bot — code complete, pending deploy (session 19)

**c3po_oracle — Phase 3E — COMPLETE (code only; deploy in next session)**
- `POST /interactions` endpoint: Ed25519 sig verification, PING/PONG handshake, channel gating, security filters, per-user rate limit (5/hr via KV)
- `runRagQuery()` shared helper: extracted RAG core (embed → 7-namespace query → secondary retrieval → merge → Claude) from POST /query; used by both the HTTP endpoint and the new queue consumer
- `async queue()` handler: consumes `c3po-oracle` Cloudflare Queue, runs RAG, posts back via Discord followup webhook; error fallback included
- `scripts/register_discord_commands.py`: registers /ask, /search, /help slash commands (guild-scoped for testing, --global for production)
- `api/wrangler.toml`: queue producer + consumer bindings added
- `.env.template`: ORACLE_* vars documented
- `plans/oracle-bot-setup.md`: step-by-step deploy guide (8 steps)

**Pinecone state: 24,765 vectors — unchanged**
- definitions: 560 | discord_links: 8,864 | discord: 5,518 | sig: 4,795 | videos: 2,940 | substack: 1,040 | pdfs: 766 | bibliography: 278 | transcripts: 4

**Substack pending (not yet synced):**
- 1 new post: `the-overloaded-train`
- 1 edited post: `introducing-the-protocol-institute`

**Open TODOs (priority order):**
1. Execute oracle bot setup — see `plans/oracle-bot-setup.md` (create Discord app, wrangler queues create, set secrets, deploy, set Interactions URL, register commands, invite bot)
2. Run `sync_substack.py` to ingest the-overloaded-train + edited post
3. Wire `definitions` namespace into `runRagQuery()` (query it alongside the other 7)
4. YouTube transcript pass — 161 deferred URLs
5. GitHub Actions cron for `sync_substack.py`

## 2026-05-20 — Lexicon namespace, attachment capture, monitoring dashboard, star weighting (session 18)

**Lexicon definitions namespace — COMPLETE**
- Migrated `sources/lexicon_draft.json` schema: flat `{term: entry}` → `{term: [entry, ...]}` (list-of-dicts for multi-definition support)
- New `ingest/sync_lexicon.py`: ingests triage a+b (560 entries) into `definitions` namespace
- Vector IDs: `lexicon__{term_slug}__{source_slug}`; metadata: term, triage, source, source_slug, variant_count, definition_index
- 560 vectors upserted

**Attachment capture — COMPLETE**
- New `ingest/attachments.py`: download Discord CDN attachments locally before 24h expiry
- Storage: `data/attachments/{channel_id}/{msg_id}/{filename}` (gitignored, local only)
- PDF/text attachments embedded as separate `discord_attachment` / `sig_attachment` vectors
- No backfill of historical attachments (deferred indefinitely)

**Monitoring Dashboard — COMPLETE**
- `sync_discord.py`, `sync_sig.py`, `fetch_discord_links.py`, `enrich_discord_links.py` all write structured JSON entries to `data/sync_log.json` (90-day rolling)
- New `ingest/generate_monitoring_page.py`: reads log + manifest + links registry → writes `../website/monitoring.html`
- Wired into `bin/daily_sync.sh`; pushed daily with SIG pages

**Star weighting — already implemented (stale TODO cleared)**
- `normalizeDiscord()` + `mergeResults()` in `api/worker.js` already implemented
- Fixed forum post URL construction in normalizeDiscord (was using channel_id instead of thread_id)

**Daily sync — link farming wired in**
- `bin/daily_sync.sh` now calls `fetch_discord_links.py --limit 200` + `enrich_discord_links.py` after each discord sync

**Pinecone state: 24,765 vectors**
- definitions: 560 | discord_links: 8,864 | discord: 5,518 | sig: 4,795 | videos: 2,940 | substack: 1,040 | pdfs: 766 | bibliography: 278 | transcripts: 4

**Open TODOs (priority order):**
1. Wire `definitions` namespace into worker.js query (decide blend weight vs. other namespaces)
2. YouTube transcript pass — 161 deferred URLs (18 succeeded in first pass; 177 failed)
3. c3po_oracle Discord bot — slash commands via Cloudflare Worker deferred response
4. GitHub Actions cron for `sync_substack.py`

## 2026-05-20 — Archived channel sweep complete; forum channel support (session 17)

**Archived channels onboarded — COMPLETE (13 channels total in manifest)**
- General/archived: #credit-protocols, #death-memory, #unconscious-protocols, #tech-standards, #built-environment, #organizational-protocols, #field-reports
- SIG/archived: #affiliate-chat (Affiliates SIG)
- Forum/archived: #reading-room (Discord type=15 — all content as forum post threads, 81 posts)
- URL registry: 3,824 URLs total after all sweeps

**Forum channel support in sync_discord.py — COMPLETE**
- New `fetch_forum_threads()`: fetches active (guild-level) + archived public threads, sorted oldest-first
- New `format_forum_post_chunk()`: bundles thread + replies into one chunk (chunk_type=`forum_post`)
- `load_general_channels()` now includes `type=forum` entries
- State tracking: `last_thread_ids` dict (separate from `last_message_ids` for text channels)
- URL registration wired in for all forum post content

**onboard_channel.py — Forum-aware**
- Detects Discord type=15; passes `discord_type` and label into ANALYSIS_PROMPT so Claude proposes `type=forum`
- Manifest entry gets `discord_type: 15` stored for reference
- Backfill routes `type=forum` same as `type=general` (sync_discord.py --channel)

**Link farming — COMPLETE for all archived channels**
- Previous fetch (507 URLs): 222 OK, 1,929 vectors → discord_links namespace
- New fetch (134 URLs from #field-reports + #reading-room): running in background

**Pinecone state: 23,992 vectors** (+ ~134 pending link fetch)
- discord: 5,518 | sig: 4,795 | discord_links: 8,651 | videos: 2,940 | substack: 1,040 | pdfs: 766 | bibliography: 278 | transcripts: 4

**Open TODOs (priority order):**
1. Set `DISCORD_SUMMARY_CHANNEL_ID` in `.env` — choose a channel for sync heartbeat posts
2. YouTube transcript pass — 161 deferred URLs
3. Attachment capture in sync scripts — download at ingest time before 24h CDN expiry
4. c3po_oracle Discord bot — slash commands via Cloudflare Worker deferred response
5. Add definitions namespace (`lexicon_draft.json`)
6. GitHub Actions cron for `sync_substack.py`

## 2026-05-20 — Channel manifest, onboarding tool, daily launchd sync (session 16)

**Channel manifest — COMPLETE**
- `data/channel_manifest.json`: unified registry for all 6 monitored channels (2 general, 4 SIG)
- Tracked in git (added `!data/channel_manifest.json` exception to .gitignore)
- Schema: type (general|sig), namespace, meeting_patterns, thresholds, onboarding_notes, status

**Script refactor — COMPLETE**
- `sync_discord.py`: reads general channels from manifest via `load_general_channels()`, falls back to env var
- `sync_sig.py`: reads SIG channels from manifest via `load_sig_channels()`, falls back to hardcoded dict

**`ingest/onboard_channel.py` — COMPLETE**
- Given `--channel <id>`: fetches sample messages + threads, calls Claude Sonnet to classify + propose config
- Prints proposed config for human approval; supports `y/N/edit` prompt and `--yes` / `--backfill` flags
- Adds entry to manifest; optionally triggers backfill via subprocess

**`bin/daily_sync.sh` — COMPLETE**
- Coordinator: sync_discord → sync_sig → rebuild_sig_summaries → generate_sig_pages → conditional website push
- Website push only if `git status` shows changes to sigs/ or sigs.html
- Manual run tested successfully

**launchd plist — COMPLETE**
- `~/Library/LaunchAgents/org.protocol-institute.c3po.daily.plist` loaded
- `StartInterval: 86400` (24h after last run, not calendar time — fires whenever laptop is awake)
- Logs: `~/Library/Logs/c3po/daily.{log,err}`

**Open TODOs (priority order):**
1. Set `DISCORD_SUMMARY_CHANNEL_ID` in `.env` — choose a channel for sync heartbeat posts
2. YouTube transcript pass — 161 deferred URLs
3. Attachment capture in sync scripts — download at ingest time before 24h CDN expiry
4. c3po_oracle Discord bot — slash commands via Cloudflare Worker deferred response
5. Add definitions namespace (`lexicon_draft.json`)
6. GitHub Actions cron for `sync_substack.py`

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
