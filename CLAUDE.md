# CLAUDE.md — C3PO

> **Environment rules, keys & safety policies:** see [Code/CLAUDE.md](../../CLAUDE.md) — read before starting work.

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

All keys are in `Code/.env.keys`. Copy to `.env` (gitignored) before running scripts.
Keys already provisioned: `VOYAGE_API_KEY`, `PINECONE_API_KEY`, `ANTHROPIC_API_KEY`.
Need to create: Pinecone index `c3po` → add `PINECONE_C3PO_HOST` to both `.env` and `Code/.env.keys`.

## Pinecone Index

Index name: `c3po`
Dimensions: 1024 (voyage-3)
Metric: cosine
Cloud: aws / us-east-1 (matches existing indexes)

Create via Pinecone console or API before running ingest scripts.

## Corpus Sources

PDFs: `../protocolized-website/public/resources/*.pdf` — copy to `data/pdfs/` (gitignored, do not commit)
Substack: download export from Protocolized Substack dashboard → unzip → point `SUBSTACK_EXPORT_DIR` at it

## Cloudflare Worker (Phase 2)

Deploy from `api/` using wrangler. Worker secrets:
- `VOYAGE_API_KEY`
- `PINECONE_API_KEY` + `PINECONE_C3PO_HOST`
- `ANTHROPIC_API_KEY`
- `ADMIN_KEY` (generate fresh per security-policy.md)

## Repo Ownership

Currently: `vgururao/c3po` (personal account)
Planned migration: `Protocol-Institute/c3po` when handing off to org (Phase 6)

## After Each Session

- Update `status.md` with what was done
- If Pinecone index was updated, note vector count
- If new env vars were added, update `.env.template` and `Code/.env.keys`
