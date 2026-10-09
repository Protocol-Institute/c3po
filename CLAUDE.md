# CLAUDE.md — PIBot (internal name: c3po)

> **Environment rules, keys & safety policies:** see [Code/CLAUDE.md](../../CLAUDE.md) — read before starting work.
> **PI key registry & security policy:** see [`../admin/keys.md`](../admin/keys.md) and [`../admin/security.md`](../admin/security.md). Do not register PI keys in `Code/.env.keys`.

RAG research assistant for the Protocol Institute corpus. Renamed **PIBot** on 2026-10-07 (was C3PO). Identity surfaces (persona, web UI, Discord handles, MCP name, bot copy) say PIBot; the plumbing keeps its `c3po` names for historical reasons (Pinecone index, KV, queue, Worker name, VM, systemd units, env vars, laptop folder, bot ids) — see [`plans/rename.md`](plans/rename.md). History (devlog, `status.md`, `meta`/`transcripts` vectors) still says C3PO, correctly.

> ## ⚠️ SECURITY INCIDENT — exposure removed, two steps still open
>
> **[`Code/incidents/2026-09-09-c3po-vm-github-token-scope.md`](../../incidents/2026-09-09-c3po-vm-github-token-scope.md)** —
> High. `c3po-vm.exe.xyz` held a GitHub token authenticated as the account owner with
> **admin + push on every repo the account could reach** (personal repos included),
> readable by `exedev`, the user both services run as. Found 2026-09-09 from the Humboldt
> project; no evidence of misuse.
>
> **Done 2026-09-09 (session 52):** the VM now authenticates with a fine-grained PAT scoped
> to `c3po` + `website` + `protocolized-website`, **Contents write only, no API access** —
> the daemon's `gh pr create` moved into the website repo's own workflow. Old token revoked.
> Runbook and the full findings: [`plans/vm-credential-hardening.md`](plans/vm-credential-hardening.md).
>
> **Still open:** (1) both services still run as `exedev` with passwordless sudo, so the
> new token is readable by the Discord bot — phase 2 of the runbook splits them into
> non-sudo service users; (2) the two scoped PATs above were exposed in session 53's
> transcript — rotate at their 2026-12-08 expiry (VGR, session 57). `GH_PAT` turned out to
> be the laptop's own `gh` CLI login (`gho_`), not a classic PAT: its plaintext copy was
> removed from `../.env.keys` (session 57); dropping its `delete_repo` scope is VGR's to run.
>
> **Before touching credentials anywhere:** `Code/security-policy.md` Rule 8. Three places
> claimed to hold "the" GitHub token here and all three held different values.


## Project scope and factorization

**C3PO is the AI backend.** Work here covers: Pinecone ingest pipelines, embedding, RAG query logic, the Cloudflare Worker API (`pibot.protocolized.io`), the Discord bot, and the ingest daemon.

**Front-end website work belongs elsewhere:**
- `protocol-institute/protocolized-website/` — protocolized.io (Hono + HTMX, D1, R2). Magazine posts, resource library, public-facing pages.
- `protocol-institute/website/` — protocol-institute.org (static HTML). Org pages, project listings.

When a request touches both layers — e.g. "mirror Substack images" — the right factorization is usually:
- **Ingest/pipeline logic** (fetch, enrich, embed, upsert to Pinecone) → c3po
- **Storage and serving** (R2, D1, rendered HTML routes) → protocolized-website
- **Static content updates** (project descriptions, links) → website

**Resource library ownership:** c3po is the enrichment source for all PI research content (PDFs, YouTube, etc.). protocolized-website is a downstream client — its `scripts/sync-*-resources.py` scripts pull from c3po's `sources/*/enriched_meta.json`. New content should be ingested through c3po first, not added manually to protocolized-website. See [`plans/resource-pipeline.md`](plans/resource-pipeline.md).

If a request seems to belong in a front-end project, flag it and suggest the correct folder before starting work. Don't implement front-end features here unless they are purely API surface (e.g. a new Worker endpoint that the front-end calls).

## Architecture

**→ See [`plans/bot-ecology.md`](plans/bot-ecology.md)** for the full pubsub-swarm architecture, bot node inventory, and roadmap (Phases A–E).

**→ See [`plans/website-interface.md`](plans/website-interface.md)** for the approved design for how c3po supplies content (meeting summaries, etc.) to the website. **Key rule: c3po writes JSON only; it never writes HTML or page structure.** The website owns all rendering. Implementation is pending — `generate_sig_pages.py` still does HTML generation and needs to be refactored per that plan.

**→ See [`plans/reranker.md`](plans/reranker.md)** for retrieval ordering. Every answer path (web, Discord, `ask_c3po`) reranks the merged pool with Voyage `rerank-3` × tier weight before the source cut (`rankPool()`; kill switch `RERANK_ANSWER_PATH`); `search_corpus` reranks only with `rerank: true`. Re-run `bin/probe_rerank.py` after any retrieval change.

**→ See [`plans/resource-pipeline.md`](plans/resource-pipeline.md)** for the resource library pipeline. **c3po is the enrichment source; protocolized-website is a client.** New resources enter via c3po's ingest pipeline (`enrich_pdfs.py`, `enrich_youtube.py`) and are synced to the website via its `sync-*-resources.py` scripts. Do not manually create resource Markdown files in protocolized-website for content that c3po can enrich — run the ingest pipeline first.

Three nodes: `c3po_listener` (ingest daemon), `c3po_bot` (Discord gateway), `c3po_web` (Cloudflare Worker). `c3po_listener`/`c3po_bot` run on `c3po-vm.exe.xyz` (exe.dev VM, systemd units `c3po-daemon.service`/`c3po-bot.service`, migrated 2026-08-01 — see `plans/exe-dev-migration.md`) — not the laptop. Logs: `ssh c3po-vm.exe.xyz "sudo journalctl -u c3po-daemon -f"` / `-u c3po-bot`. The laptop clone and the VM clone are independent git checkouts; push/pull to move work between them. The daemon self-pulls each cycle and runs ingest scripts as subprocesses, so **script** changes take effect on the next cycle — but `bin/daemon.py`'s own code (step list, push flow) is loaded at process start and needs `sudo systemctl restart c3po-daemon`. `c3po_web` deploys stay laptop-initiated (`wrangler deploy` from `api/`).

## Python

Use `/opt/homebrew/bin/python3` (Python 3.14). Activate venv before running scripts:

```bash
source .venv/bin/activate
```

## Keys

PI keys stored in `../.env.keys`; copy to `.env` (gitignored) before running. Keys provisioned: `VOYAGE_API_KEY`, `PINECONE_API_KEY`, `PINECONE_C3PO_HOST`, `ANTHROPIC_API_KEY`, `ORACLE_BOT_TOKEN`, `ORACLE_APPLICATION_ID`, `DISCORD_BOT_TOKEN`. See `../admin/keys.md` for ownership.

## Cloudflare Worker

Deploy from `api/` using wrangler against the **PI org CF account** (`7e8c7969b2464d23795c555bc6a32af8`).

```bash
CLOUDFLARE_API_TOKEN=$(grep CLOUDFLARE_API_TOKEN ../.env.keys | cut -d= -f2) \
CLOUDFLARE_ACCOUNT_ID=7e8c7969b2464d23795c555bc6a32af8 \
npx wrangler deploy
```

Live URL: **`https://pibot.protocolized.io`** (custom domain on protocolized.io zone). `c3po.protocolized.io` is the legacy host: browser page loads 301 to the new one, while API/MCP/POST callers keep being served there (the redirect gate is in `worker.js` `fetch`). MCP tool is `ask_pibot`; `ask_c3po` remains a hidden alias. The Worker takes an admin-only `?now=ISO` on `POST /query` (valid `X-Admin-Key` required; it also skips the per-IP rate limit) so `bin/probe_event_scope.py` can replay any day; event awareness (registry, `KNOWN EVENTS` digest, live-event window) is described in [`plans/event-awareness.md`](plans/event-awareness.md). Both domains are Worker custom domains in the PI CF account (added via the `workers/domains` API, not in `wrangler.toml`).
Workers subdomain: `c3po.team-7e8.workers.dev`.

Secrets on PI worker: `VOYAGE_API_KEY`, `PINECONE_API_KEY`, `PINECONE_C3PO_HOST`, `ANTHROPIC_API_KEY`, `ADMIN_KEY`, `MCP_API_KEY`, `DISCORD_BOT_TOKEN`, `ORACLE_BOT_TOKEN`, `ORACLE_APPLICATION_ID`, `ORACLE_PUBLIC_KEY`.

## Repo Ownership

Repo: `Protocol-Institute/pibot` (renamed from `c3po` 2026-10-07; transferred from `vgururao/c3po` 2026-05-31; GitHub redirects the old name). **Public** since 2026-07-23.

## Pinecone Index (live)

Index: `c3po` · 1024 dims (voyage-3) · cosine · aws/us-east-1
Host: `https://c3po-1os2tli.svc.aped-4627-b74a.pinecone.io` (PI org account, migrated 2026-05-31)

| Namespace | Vectors | Notes |
|-----------|---------|-------|
| `discord_links` | 13,718 | Community-shared URLs, scored by Haiku |
| `sig` | 8,890 | SIG Discord messages/summaries + .org meeting pages (`sig_meeting_page`) + audio summaries (`audio_meeting_summary`, `audio_meeting_section`); 8 SIGs: SIGFPT, MRG, SIGPfB, ProtFiSIG, SIGPSY, DRG, PRG (Personhood Research Group — audio only, no Discord channel), Intelligence Media (Discord only, no meetings yet; `sig_display` is the full name — session 57) |
| `discord` | 5,974 | General + forum channels; starred msgs weighted 1.0×, unstarred 0.70× |
| `videos` | 3,127 | YouTube talks (97 videos; whole-transcript Sonnet enrichment, full chunk text — session 56) |
| `substack` | 1,267 | Protocolized magazine |
| `pdfs` | 765 | 85 papers/essays (`sources/pdfs/enriched_meta.json`) |
| `definitions` | 560 | PI lexicon (914 terms, triage a/b/c); metadata carries `definition` + `text` since session 57 (was term-only, so hits had empty excerpts) |
| `bibliography` | 278 | External works cited by PI corpus |
| `discord_guide` | 80 | Scoped per [`plans/discord-guide-scope.md`](plans/discord-guide-scope.md): excludes transient/admin channels (MOD, Server Link Feed, introductions/bugs/announcements); archived-read-only channels embed once then freeze; SIG channels include cadence + next_event_time |
| `meta` | 59 | C3PO self-knowledge: 1 vector/devlog session (59 sessions; half-numbered entries 8.5 and 27.5 anchor as `#session-8-5` / `#session-27-5`); queried at 3 results max alongside all other namespaces |
| `transcripts` | 58 | Bot conversation self-memory: web + Discord Q&A |
| `symposium` | 714 | Protocol Symposium 2026: programme (`symposium_overview`, `symposium_block`, `symposium_session`, `symposium_workshop`), slide decks and speaker papers (`symposium_slides`, 52 Drive files all matched; 4 stubs), and recordings (`symposium_recording` + timestamped `symposium_transcript`, 20 of 40 so far — laptop-only; YouTube IpBlocks the laptop too as of 2026-10-05, see `ingest/sync_symposium_videos.py`). Queries that name the event are answered from this namespace alone; see [`plans/symposium-ingest.md`](plans/symposium-ingest.md) |
| **Total** | **35,490** | |

## Key Ingest Scripts

| Script | What it does | State file |
|--------|-------------|-----------|
| `ingest/sync_substack.py` | Protocolized Substack posts | `data/substack_state.json` |
| `ingest/sync_discord.py` | General Discord channels | `data/discord_state.json` |
| `ingest/sync_sig.py` | SIG channels (6 groups) | `data/sig_state.json` |
| `ingest/sync_sig_pages.py` | SIG meeting pages from .org | `data/sig_pages_state.json` |
| `ingest/fetch_discord_links.py` | Fetch pending shared URLs | `data/discord_links_registry.json` |
| `ingest/enrich_discord_links.py` | Score/prune links with Haiku | same |
| `ingest/ingest_pdfs.py` | PDFs from local or web resources | `data/enriched_meta.json` |
| `ingest/sync_discord_channels.py` | Guild channel map → `discord_guide` namespace | `config/discord_channels.json` |
| `ingest/sync_discord_events.py` | Scheduled events → cadence + next_event_time in registry | `config/discord_channels.json` |
| `ingest/sync_meeting_notes.py` | Audio summaries from #meeting-notes → `sig` namespace | `data/meeting_notes_state.json` |
| `ingest/update_sig_pages.py` | Create/update individual meeting detail pages on .org | (reads `data/sigs/meetings/`) |
| `ingest/sync_bot_conversations.py` | Discord bot spool → `transcripts` | `data/spool/bot_conversations/` |
| `ingest/sync_web_chats.py` | Public web chats → `transcripts` | `data/web_chats_state.json` |
| `bin/backfill_event_tags.py` | One-off/idempotent backfill of `ts_unix` + `event_id` on existing vectors (new ones are tagged at upsert by `ingest/event_tags.py`). Run `--dry-run` first | — |
| `ingest/sync_events.py` | Event registry: Community + Institute iCal feeds + `events.json` -> `data/events_registry.json` and Worker KV `events:registry` (the known-events digest). Aliases/series in `config/event_aliases.json`. No Pinecone writes | `data/events_state.json` |
| `ingest/sync_devlog.py` | Devlog sessions → `meta` namespace | `data/devlog_state.json` |
| `ingest/sync_symposium.py` | Protocol Symposium 2026 programme → `symposium` namespace | `data/symposium_state.json` |
| `ingest/sync_symposium_decks.py` | Symposium slide decks from the public Drive folder → `symposium` namespace (self-throttles to weekly under `--daemon`) | `data/symposium_decks_state.json` |
| `ingest/sync_symposium_videos.py` | Symposium recordings (YouTube playlist) → `symposium` namespace + resource library. **Laptop only** — YouTube 429s the VM; re-run after new uploads | `data/symposium_videos_state.json` |
| `ingest/generate_devlog_page.py` | Render devlog → D1 slug `c3po-devlog` | `data/devlog_page_state.json` |

All run automatically via `bin/daemon.py` (c3po_listener), except `sync_symposium_videos.py`. Run manually with `--dry-run` to preview.

After editing `data/devlog.json`, run `python3 ingest/sync_devlog.py` then `python3 ingest/generate_devlog_page.py` to republish.

**The devlog is paged.** `data/devlog.json` is the live page a session appends to; older
entries live in `data/devlog_archive_NNN.json`. This is a storage split only — every
consumer loads the merged log via `ingest/devlog_store.load_devlog()`, so the published
page, `DEVLOG.md` and the `meta` namespace all see one continuous record and
`#session-{id}` anchors survive a roll. Roll the live page when it gets unwieldy:
`python3 bin/devlog_roll.py --keep 10` (or `--through-id N`); commit the new archive page.
The published page is written to D1 as one INSERT plus `body = body || '...'` appends, so
D1's 100KB per-statement limit no longer caps it.

## At Session Start

> **Base ritual:** [`Code/devops/rituals.md`](../../devops/rituals.md) (v1.0) is canonical: startup S1–S7, wrap-up W0–W7. The steps below are this project's **local mods** — they run in addition to the base, and the stricter step wins. Check this section against the base when you next edit it; `Code/devops/rituals-survey.md` lists the common gaps (`date`, security sweep W3, cross-project items S4, a wrap-up report keyed to step IDs).

1. Read `status.md` — open questions, blockers, previous session end state.
2. Check Pinecone vector counts:
   ```bash
   source .venv/bin/activate
   python3 -c "
   import os; from dotenv import load_dotenv; load_dotenv()
   from pinecone import Pinecone
   pc = Pinecone(api_key=os.environ['PINECONE_API_KEY'])
   idx = pc.Index(host=os.environ['PINECONE_C3PO_HOST'])
   stats = idx.describe_index_stats()
   for ns, info in stats.namespaces.items():
       print(f'{ns}: {info.vector_count:,} vectors')
   print(f'Total: {stats.total_vector_count:,}')
   "
   ```
3. Run Substack dry-run: `python3 ingest/sync_substack.py --dry-run`
4. Review intro quality issues since last session:
   ```bash
   source .venv/bin/activate
   python3 bin/review_intro_quality.py
   ```
   Present any unreviewed issues to the user and discuss fixes before starting other work.
   After reviewing, run `python3 bin/review_intro_quality.py --mark-reviewed` to clear them.
5. Check Anthropic API cost since last session. **Run this on the VM, not the laptop** — `data/cost_log.jsonl` is
   gitignored, so the laptop copy froze on 2026-08-01 when the daemon moved to `c3po-vm.exe.xyz` and reports $0:
   ```bash
   ssh c3po-vm.exe.xyz "cd ~/c3po && source .venv/bin/activate && python3 bin/cost_report.py"
   ```
   Report last-7-days spend and all-time total.
6. Summarize: vector counts vs. last session, pending Substack posts, open TODOs from `status.md`, intro quality findings, and API spend.

---

## After Each Session

**Documentation (always — do not skip):**
1. `status.md` — dated log entry with PT start–end times and one-line summary.
2. `CLAUDE.md` — update Pinecone vector counts table if index was modified.
3. `data/devlog.json` — append session entry. Public build log; use existing entries as style guide.

**Keys/env (if changed):**
4. New env vars: update `.env.template`; add to `../.env.keys`; add row to `../admin/keys.md`.

**Repo:**
5. `git add` relevant files (never `.env`); `git commit`; `git push`.

**Memory:**
6. Update Claude memory — anything non-obvious about decisions or workflow. Don't duplicate CLAUDE.md.

**Checklist report (always last):**
7. Print checklist with ✅/⚠️/n/a per item and one sentence on each.

## Anthropic API key (changed 2026-10-07)

c3po (being renamed pibot) now has its own key: `ANTHROPIC_KEY_PIBOT` in `protocol-institute/.env.keys` — service account `pibot`, Developer role, scoped to the PI workspace. `c3po/.env` already carries it as `ANTHROPIC_API_KEY`. **Rotated 2026-10-07:** the c3po API Worker secret (verified with a live `/query`), `c3po-vm.exe.xyz:/home/exedev/c3po/.env` (daemon + bot restarted; a backup `.env.bak-2026-10-07` holding the old key is still on the VM — delete it once confirmed), and the GitHub Actions secret `ANTHROPIC_API_KEY` (not yet exercised by a workflow run). Spend is attributable per key in the Console; `data/cost_log.jsonl` remains the local record. Plan: `Code/anthropic-key-plan.md`. Do not copy the old shared key into new files.

**Follow-ups for the next session here:** (1) confirm the next scheduled `sync-substack` run (daily 08:00 UTC) is green — it is the first to use the rotated GitHub Actions secret (`gh run list --repo Protocol-Institute/pibot --workflow sync-substack.yml`); (2) delete `c3po-vm:/home/exedev/c3po/.env.bak-2026-10-07` (holds the old shared key) once that run passes; (3) c3po is being renamed pibot — when it is, rename the Worker secret/registry rows but keep key name `pibot`; (4) `data/cost_log.jsonl` records only input/output tokens, not cache read/write — add `cache_creation_input_tokens`/`cache_read_input_tokens` if prompt caching is extended to ingest (see `Code/anthropic-key-plan.md` §4).
