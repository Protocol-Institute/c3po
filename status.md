# C3PO — Status Log

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
