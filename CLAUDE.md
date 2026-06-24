# CLAUDE.md — C3PO

> **Environment rules, keys & safety policies:** see [Code/CLAUDE.md](../../CLAUDE.md) — read before starting work.
> **PI key registry & security policy:** see [`../admin/keys.md`](../admin/keys.md) and [`../admin/security.md`](../admin/security.md). Do not register PI keys in `Code/.env.keys`.

RAG research assistant for the Protocol Institute corpus. Named for the Star Wars protocol droid.

## Project scope and factorization

**C3PO is the AI backend.** Work here covers: Pinecone ingest pipelines, embedding, RAG query logic, the Cloudflare Worker API (`c3po.protocolized.io`), the Discord bot, and the ingest daemon.

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

**→ See [`plans/resource-pipeline.md`](plans/resource-pipeline.md)** for the resource library pipeline. **c3po is the enrichment source; protocolized-website is a client.** New resources enter via c3po's ingest pipeline (`enrich_pdfs.py`, `enrich_youtube.py`) and are synced to the website via its `sync-*-resources.py` scripts. Do not manually create resource Markdown files in protocolized-website for content that c3po can enrich — run the ingest pipeline first.

Three nodes: `c3po_listener` (ingest daemon), `c3po_bot` (Discord gateway), `c3po_web` (Cloudflare Worker). Both local bots managed by launchd; logs at `~/Library/Logs/c3po/`.

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

Live URL: **`https://c3po.protocolized.io`** (custom domain on protocolized.io zone, migrated 2026-05-31).
Workers subdomain: `c3po.team-7e8.workers.dev`.

Secrets on PI worker: `VOYAGE_API_KEY`, `PINECONE_API_KEY`, `PINECONE_C3PO_HOST`, `ANTHROPIC_API_KEY`, `ADMIN_KEY`, `MCP_API_KEY`, `DISCORD_BOT_TOKEN`, `ORACLE_BOT_TOKEN`, `ORACLE_APPLICATION_ID`, `ORACLE_PUBLIC_KEY`.

## Repo Ownership

Repo: `Protocol-Institute/c3po` (transferred from `vgururao/c3po` 2026-05-31).

## Pinecone Index (live)

Index: `c3po` · 1024 dims (voyage-3) · cosine · aws/us-east-1
Host: `https://c3po-1os2tli.svc.aped-4627-b74a.pinecone.io` (PI org account, migrated 2026-05-31)

| Namespace | Vectors | Notes |
|-----------|---------|-------|
| `discord_links` | 9,944 | Community-shared URLs, scored by Haiku |
| `discord` | 5,597 | General + forum channels; starred msgs weighted 1.0×, unstarred 0.70× |
| `sig` | 5,588 | SIG Discord messages/summaries + .org meeting pages (`sig_meeting_page`); 6 SIGs: SIGFPT, MRG, SIGPfB, ProtFiSIG, SIGPSY, DRG |
| `videos` | 3,127 | YouTube talks (97 videos) |
| `substack` | 1,106 | Protocolized magazine (121+ posts) |
| `definitions` | 560 | PI lexicon (914 terms, triage a/b/c) |
| `pdfs` | 750 | 72 papers/essays (11 cover letters/title pages absent from PI migration) |
| `bibliography` | 278 | External works cited by PI corpus |
| `discord_guide` | 79 | All active guild channels; Haiku-described; SIG channels include cadence + next_event_time |
| `meta` | 35 | C3PO self-knowledge: 1 vector/devlog session; queried at 3 results max alongside all other namespaces |
| `transcripts` | 25 | Bot conversation self-memory: web + Discord Q&A |
| **Total** | **~27,089** | |

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
| `ingest/sync_bot_conversations.py` | Discord bot spool → `transcripts` | `data/spool/bot_conversations/` |
| `ingest/sync_web_chats.py` | Public web chats → `transcripts` | `data/web_chats_state.json` |
| `ingest/sync_devlog.py` | Devlog sessions → `meta` namespace | `data/devlog_state.json` |
| `ingest/generate_devlog_page.py` | Render devlog → D1 slug `c3po-devlog` | `data/devlog_page_state.json` |

All run automatically via `bin/daemon.py` (c3po_listener). Run manually with `--dry-run` to preview.

After editing `data/devlog.json`, run `python3 ingest/sync_devlog.py` then `python3 ingest/generate_devlog_page.py` to republish.

## At Session Start

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
5. Check Anthropic API cost since last session:
   ```bash
   source .venv/bin/activate
   python3 bin/cost_report.py
   ```
   Report last-7-days spend and all-time total. If `data/cost_log.jsonl` does not yet exist, note that tracking starts from this session onward.
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
