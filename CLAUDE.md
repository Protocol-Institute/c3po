# CLAUDE.md — C3PO

> **Environment rules, keys & safety policies:** see [Code/CLAUDE.md](../../CLAUDE.md) — read before starting work.
> **PI key registry & security policy:** see [`../admin/keys.md`](../admin/keys.md) and [`../admin/security.md`](../admin/security.md). Do not register PI keys in `Code/.env.keys`.

RAG research assistant for the Protocol Institute corpus. Named for the Star Wars protocol droid.

## Architecture

**→ See [`plans/bot-ecology.md`](plans/bot-ecology.md)** for the full pubsub-swarm architecture, bot node inventory, and roadmap (Phases A–E).

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
Host: `https://c3po-bwo39z7.svc.aped-4627-b74a.pinecone.io`

| Namespace | Vectors | Notes |
|-----------|---------|-------|
| `discord_links` | 9,108 | Community-shared URLs, scored by Haiku |
| `discord` | 5,538 | General + forum channels; starred msgs weighted 1.0×, unstarred 0.70× |
| `sig` | 5,028 | SIG Discord messages/summaries + 91 .org meeting pages (`sig_meeting_page`) |
| `videos` | 2,940 | YouTube talks (91 videos) |
| `substack` | 1,057 | Protocolized magazine (116+ posts) |
| `definitions` | 560 | PI lexicon (914 terms, triage a/b/c) |
| `pdfs` | 800 | 82+ papers/essays including external resources |
| `bibliography` | 278 | External works cited by PI corpus |
| `discord_guide` | 78 | All active guild channels; Haiku-described; SIG channels include cadence + next_event_time |
| `transcripts` | 9 | Bot conversation self-memory: 4 web_conversation (Phase C); grows with Discord spool (Phase B) |
| `humboldt` | 468 | Owned by humboldt project (`aware` only — do not ingest) |
| **Total** | **~25,960** | |

## Key Ingest Scripts

| Script | What it does | State file |
|--------|-------------|-----------|
| `ingest/sync_substack.py` | Protocolized Substack posts | `data/substack_state.json` |
| `ingest/sync_discord.py` | General Discord channels | `data/discord_state.json` |
| `ingest/sync_sig.py` | SIG channels (4 groups) | `data/sig_state.json` |
| `ingest/sync_sig_pages.py` | SIG meeting pages from .org | `data/sig_pages_state.json` |
| `ingest/fetch_discord_links.py` | Fetch pending shared URLs | `data/discord_links_registry.json` |
| `ingest/enrich_discord_links.py` | Score/prune links with Haiku | same |
| `ingest/ingest_pdfs.py` | PDFs from local or web resources | `data/enriched_meta.json` |
| `ingest/sync_discord_channels.py` | Guild channel map → `discord_guide` namespace | `config/discord_channels.json` |
| `ingest/sync_discord_events.py` | Scheduled events → cadence + next_event_time in registry | `config/discord_channels.json` |
| `ingest/sync_bot_conversations.py` | Discord bot spool → `transcripts` | `data/spool/bot_conversations/` |
| `ingest/sync_web_chats.py` | Public web chats → `transcripts` | `data/web_chats_state.json` |

All run automatically via `bin/daemon.py` (c3po_listener). Run manually with `--dry-run` to preview.

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
4. Summarize: vector counts vs. last session, pending Substack posts, open TODOs from `status.md`.

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
