# CLAUDE.md — C3PO

> **Environment rules, keys & safety policies:** see [Code/CLAUDE.md](../../CLAUDE.md) — read before starting work.
> **PI key registry & security policy:** see [`../admin/keys.md`](../admin/keys.md) and [`../admin/security.md`](../admin/security.md). Do not register PI keys in `Code/.env.keys`.

RAG agent for the Protocol Institute research library. Named for the Star Wars protocol droid.

## Python

Use `/opt/homebrew/bin/python3` (Python 3.14). Activate venv before running scripts:

```bash
source .venv/bin/activate
```

Install deps:
```bash
pip install pdfplumber voyageai pinecone-client anthropic python-dotenv discord.py
```

## Keys

PI keys are stored in `../.env.keys` and inventoried in `../admin/keys.md`. Copy to `.env` (gitignored) before running scripts. Do not register PI keys in `Code/.env.keys` — that file is personal scope only.

Keys provisioned: `VOYAGE_API_KEY`, `PINECONE_API_KEY`, `PINECONE_C3PO_HOST`, `ANTHROPIC_API_KEY`. All currently personal-account keys (billing: expense) — see `../admin/keys.md` for ownership details.

## Pinecone Index

Index name: `c3po` (live)
Dimensions: 1024 (voyage-3)
Metric: cosine
Cloud: aws / us-east-1

## Corpus Sources

PDFs: `../protocolized-website/public/resources/*.pdf` — copy to `data/pdfs/` (gitignored, do not commit)
Substack: download export from Protocolized Substack dashboard → unzip → point `SUBSTACK_EXPORT_DIR` at it

## Cloudflare Worker (Phase 2)

Deploy from `api/` using wrangler. Worker secrets:
- `VOYAGE_API_KEY`
- `PINECONE_API_KEY` + `PINECONE_C3PO_HOST`
- `ANTHROPIC_API_KEY`
- `ADMIN_KEY` (generate fresh per `../admin/security.md`)

## Repo Ownership

Currently: `vgururao/c3po` (personal account)
Planned migration: `Protocol-Institute/c3po` when handing off to org (Phase 6)

## Pinecone Index (live)

Host: `https://c3po-bwo39z7.svc.aped-4627-b74a.pinecone.io`
Namespaces: `substack` (1,057 · 2026-05-27), `pdfs` (766 · 2026-05-15), `videos` (2,940 · 2026-05-17), `bibliography` (278 · 2026-05-18), `transcripts` (4 · grows with use), `discord` (5,537 · 2026-05-28), `sig` (4,932 · 2026-05-28), `discord_links` (9,108 · 2026-05-28), `definitions` (560 · 2026-05-20 · **now wired into query pipeline**) — **Total: 25,276** (excl. humboldt: 94 · ownership: `aware`)

Discord ingest: `ingest/sync_discord.py` — REST-only batch poll, no gateway, no privileged intents.
Channel types supported: `general` (text channel messages), `forum` (Discord type=15, fetches threads as posts).
Channel registry: `data/channel_manifest.json` — 15 channels (4 active general+forum, 4 active SIG, 7 archived).
Star weighting: starred messages (`star_count > 0`) → 1.0×; unstarred → 0.70× in `normalizeDiscord()` + `mergeResults()` in `api/worker.js`.

SIG ingest: `ingest/sync_sig.py` — same REST approach, all 4 SIG channels. State in `data/sig_state.json`.
All 4 SIG channels ingested (2026-05-19): SIGFPT (757), MRG (433), SIGPfB (2,214), ProtFiSIG (1,179).
Totals: 77 meeting summaries + body chunks, 169 discussions, 4,015 main messages.
SIG chunk types: `sig_message`, `sig_reply`, `sig_discussion`, `sig_meeting_body`, `sig_meeting_summary`.

Link farming: `data/discord_links_registry.json` — 3,824 URLs total. Run `python3 ingest/fetch_discord_links.py` to fetch pending. Run `python3 ingest/enrich_discord_links.py` after to score/prune.

## At Session Start

1. Read `status.md` — review the last entry for open questions, blockers, and where the previous session ended.
2. Check current Pinecone vector counts:
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
3. Run the Substack sync dry-run to check for new or edited posts since last session:
   ```bash
   source .venv/bin/activate
   python3 ingest/sync_substack.py --dry-run
   ```
4. Briefly summarize to Venkat: vector counts vs. last session's recorded counts in `status.md`, any new/edited Substack posts pending, and the open questions or next steps from `status.md`.

---

## After Each Session

**Documentation (always — do not skip any of these):**
1. `status.md` — add a dated log entry with PT start–end times and a one-line summary of what changed.
2. `CLAUDE.md` — update Pinecone vector counts and namespace state if the index was modified.
3. `data/devlog.json` — append a session entry with items covering the substantive work done. This is a public build log read by people curious about the process. Use the existing entries as a style guide. Do not skip this step.

**Keys/env (if changed):**
4. New env vars: update `.env.template`; add to `../.env.keys` with `owner`/`billing`/`projects`/`registered` annotations; add a row to `../admin/keys.md`. Do not add to `Code/.env.keys`.

**Repo:**
5. `git add` relevant files (never `.env`); `git commit`; `git push`.

**Memory:**
6. Update Claude memory (`/Users/Venkat/.claude/projects/.../memory/`) — save anything non-obvious about corpus structure, pipeline decisions, or workflow preferences that would help future sessions. Do not duplicate what's in CLAUDE.md or recoverable from code.

**Checklist report (always last — do not skip):**
7. Print the checklist with a ✅/⚠️/n/a next to every item, and one sentence on what was done or why it was skipped. This is shown to Venkat before the session ends.
