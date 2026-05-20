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
Namespaces: `substack` (1,040 · 2026-05-14), `pdfs` (766 · 2026-05-15), `videos` (2,940 · 2026-05-17), `bibliography` (278 · 2026-05-18), `transcripts` (~4 · grows with use), `discord` (3,301 · 2026-05-19), `sig` (4,583 · 2026-05-19) — **Total: 12,912**

Discord ingest: `ingest/sync_discord.py` — REST-only batch poll, no gateway, no privileged intents.
Channels: `#idle-musings` + `#protocol-watch` whitelisted via `DISCORD_CHANNEL_IDS`. State in `data/discord_state.json`.
Star weighting (TODO — worker not yet updated): starred messages (`star_count > 0`) → 1.0×; unstarred → 0.70× in `normalizeDiscord()` + `mergeResults()` in `api/worker.js`.

SIG ingest: `ingest/sync_sig.py` — same REST approach, all 4 SIG channels. State in `data/sig_state.json`.
All 4 SIG channels ingested (2026-05-19): SIGFPT (757), MRG (433), SIGPfB (2,214), ProtFiSIG (1,179).
Totals: 77 meeting summaries + body chunks, 169 discussions, 4,015 main messages.
SIG chunk types: `sig_message`, `sig_reply`, `sig_discussion`, `sig_meeting_body`, `sig_meeting_summary`.

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

**Documentation (always):**
1. `status.md` — add a dated log entry with PT start–end times and a one-line summary of what changed.
2. `CLAUDE.md` — update Pinecone vector counts and namespace state if the index was modified.

**Keys/env (if changed):**
3. New env vars: update `.env.template`; add to `../.env.keys` with `owner`/`billing`/`projects`/`registered` annotations; add a row to `../admin/keys.md`. Do not add to `Code/.env.keys`.

**Repo:**
4. `git add` relevant files (never `.env`); `git commit`; `git push`.

**Memory:**
5. Update Claude memory (`/Users/Venkat/.claude/projects/.../memory/`) — save anything non-obvious about corpus structure, pipeline decisions, or workflow preferences that would help future sessions. Do not duplicate what's in CLAUDE.md or recoverable from code.
