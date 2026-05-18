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
Namespaces: `substack` (1,040 · 2026-05-14), `pdfs` (766 · 2026-05-15), `videos` (2,940 · 2026-05-17), `transcripts` (~4 · grows with use) — **Total: ~4,750**

## At Session Start

**Always do this first before any other work:**

1. Run `python3 devlog_session.py start` — records session start time to `/tmp/c3po_devlog_session_start.txt`.
2. Run `python3 ../admin/expenses/track.py status` — shows all active PI project sessions and flags any overlap. If another project session is already running, no action needed; overlap is tracked automatically.
3. Read `status.md` — review the last entry for open questions, blockers, and where the previous session ended.
4. Check current Pinecone vector counts:
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
5. Run the Substack sync dry-run to check for new or edited posts since last session:
   ```bash
   source .venv/bin/activate
   python3 ingest/sync_substack.py --dry-run
   ```
6. Briefly summarize to Venkat: vector counts vs. last session's recorded counts in `status.md`, any new/edited Substack posts pending, and the open questions or next steps from `status.md`.

---

## After Each Session

**Documentation (always):**
1. `data/devlog.json` — add session entry (label, title, date, time_pt, tracks, costs_usd, vector_counts, items in HTML). Run `python3 devlog_session.py end` for the timestamp. Run `python3 devlog_render.py` to regenerate `DEVLOG.md`. The devlog is the primary record of architectural decisions, corpus discoveries, and cost data — write for a public technical audience.
2. `status.md` — add a dated log entry with PT start–end times and a one-line summary of what changed.
3. `CLAUDE.md` — update Pinecone vector counts and namespace state if the index was modified.

**Keys/env (if changed):**
4. New env vars: update `.env.template`; add to `../.env.keys` with `owner`/`billing`/`projects`/`registered` annotations; add a row to `../admin/keys.md`. Do not add to `Code/.env.keys`.

**Repo:**
5. `git add` relevant files (never `.env`); `git commit`; `git push`.

**Expenses (always):**
6. `python3 ../admin/expenses/track.py end` — computes billable hours from all active session start files; detects overlap; prints a pre-filled log entry.
7. Paste the entry into `../admin/expenses/log-{your-id}.json` sessions array; fill in `api_costs` (pull from this session's `costs_usd` in devlog) and `notes`.
8. `python3 ../admin/expenses/render.py` — regenerates `EXPENSES.md` and `expenses.csv`.

**Memory:**
9. Update Claude memory (`/Users/Venkat/.claude/projects/.../memory/`) — save anything non-obvious about corpus structure, pipeline decisions, or workflow preferences that would help future sessions. Do not duplicate what's in CLAUDE.md or recoverable from code.
