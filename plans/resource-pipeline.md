# Resource Pipeline — c3po as Enrichment Source

**Status:** Direction set; partially implemented (YouTube complete, PDFs pending).  
**Companion plan:** [`website-interface.md`](website-interface.md) — covers the SIG/meeting content flow; the same JSON-only boundary principle applies here.

---

## Background

`protocolized-website` was built first. Its `src/content/resources/` tree (~294 Markdown files) was the original authoritative record for the PI research library. c3po bootstrapped from it: `ingest/enrich_pdfs.py` reads those Markdown files to get title/authors/type/tags as context before calling Haiku for enrichment.

That bootstrapping is now complete. **Going forward, c3po is the enrichment source and protocolized-website is a client.** New resources enter the library via c3po's ingest pipeline and are then synced to protocolized-website for public presentation.

---

## Division of Labor

| Concern | Owned by |
|---|---|
| Fetching raw content (PDFs, YouTube captions, etc.) | **c3po** |
| Enrichment (Haiku summaries, categories, speaker extraction) | **c3po** |
| Canonical enriched metadata store | **c3po** (`sources/*/enriched_meta.json`) |
| Pinecone embedding and retrieval | **c3po** |
| Resource Markdown files + D1 database | **protocolized-website** |
| Public rendering (resource cards, detail pages, search) | **protocolized-website** |

c3po's output boundary is `sources/*/enriched_meta.json`. It never writes Markdown, HTML, or D1 rows.

---

## The Handoff Files

c3po produces per-source enriched metadata. protocolized-website reads these files via sync scripts:

| Source | c3po writes | protocolized-website reads via |
|---|---|---|
| YouTube videos | `sources/youtube/enriched_meta.json` | `scripts/sync-youtube-resources.py` ✅ |
| PDFs | `sources/pdfs/enriched_meta.json` | `scripts/sync-pdf-resources.py` ✅ |
| Substack posts | `sources/substack/enriched_meta.json` | `scripts/sync-substack-resources.py` ✅ (catchup done) |

All PI content types — research papers, videos, and magazine posts (Articles, Fictions,
Obliquities, editorial) — belong in the resource library. The library is not only a
research index; it's the full content catalogue.

---

## Enriched Metadata Contracts

### YouTube (`sources/youtube/enriched_meta.json`)

Keyed by `video_id`:

```json
{
  "VIDEO_ID": {
    "video_id": "...",
    "title": "...",
    "url": "https://www.youtube.com/watch?v=...",
    "duration_sec": 3443,
    "upload_date": "NA",
    "series": "guest-talks",
    "has_captions": true,
    "summary": "2-3 concrete sentences naming speaker + argument.",
    "categories": ["protocol-theory", "interview"],
    "speakers": ["Speaker Name"],
    "key_concepts": ["concept-a", "concept-b"]
  }
}
```

### Substack posts (`sources/substack/enriched_meta.json`)

Keyed by Substack slug:

```json
{
  "SLUG": {
    "summary": "Two sentences. Specific argument, story, or contribution.",
    "categories": ["protocol-fiction", "editorial"],
    "primary_author": "Author Name",
    "all_authors": ["Author Name"]
  }
}
```

Joined with `sources/substack/api_metadata.json` (keyed by slug) for title, date,
section, and full author list. Section determines resource type: `Articles` →
`article`, `Fictions` → `fiction`, `Obliquities`/`Protocolized` → `article`.
Posts with no api_metadata entry get `date: 1970-01-01` as a cleanup sentinel.

### PDFs (`sources/pdfs/enriched_meta.json`)

Keyed by PDF basename (e.g. `02-AUSTIN-2023-12-13.pdf`):

```json
{
  "BASENAME.pdf": {
    "summary": "Two sentences. Specific argument or contribution.",
    "categories": ["protocol-theory", "research-report"],
    "primary_author": "Drew Austin",
    "all_authors": ["Drew Austin"]
  }
}
```

The PDF basename maps to a resource's `file:` field in its Markdown frontmatter (which becomes the R2 path). `enrich_pdfs.py` already reads protocolized-website Markdown files to cross-reference.

---

## Sync Scripts (protocolized-website side)

Each sync script follows the same pattern:

1. Read c3po's `enriched_meta.json`
2. Cross-reference against existing resource Markdown files (YouTube: by video_id in `url:`; PDFs: by filename in `file:`)
3. Update matched files with enriched description, tags, authors, thumbnail
4. Create stub files for any enriched items not yet in the library
5. Caller runs `migrate-to-d1.py --remote` to push to D1

**Already implemented:**
- `scripts/sync-youtube-resources.py` — enriches 91 video resources (Session 19, 2026-06-15)
- `scripts/sync-pdf-resources.py` — enriches 70 PDF resources (Session 19, 2026-06-15)
- `scripts/sync-substack-resources.py` — catchup sync of all Substack posts to resource library (Session 19, 2026-06-16)

---

## Forward Pipeline: New Substack Posts

When `ingest/sync_substack.py` enriches a new post, it should write a resource
Markdown stub to `../protocolized-website/src/content/resources/{slug}.md`.
This mirrors what `draft_resource.py` will do for PDFs.

The `protocolized-website` daily GH Action (`sync-substack.yml`) currently runs
`scripts/sync-substack.py` which creates Markdown stubs from the RSS feed (last ~20
posts, no enrichment). Once c3po takes over Markdown creation for new posts, that
script should be updated to **skip Markdown creation** and only handle D1 posts-table
writes. The c3po enrichment path produces higher-quality stubs (real author names,
Haiku summary as description, correct categories → tags).

**Trigger:** After each run of `ingest/sync_substack.py` that processes new posts,
also run `sync-substack-resources.py` in protocolized-website (or have c3po write
directly). This is not yet wired; Substack sync is currently a manual trigger in c3po.

---

## New Content Intake Flow

When a new PDF arrives that is not yet in the resource library:

1. **Drop PDF** in `c3po/data/pdfs/`
2. **Enrich**: `python3 ingest/enrich_pdfs.py` — pdfplumber extracts text, Haiku generates summary + categories
3. **Draft resource stub**: a new `ingest/draft_resource.py` script (to build) that writes a near-complete Markdown stub to `protocolized-website/src/content/resources/`. Prefills: description (from summary), tags (from categories), type (inferred). Leaves for human review: `date`, `authors`, `audience`, `file` URL.
4. **Author fills in** missing fields in the stub file
5. **Sync enrichment**: `python3 scripts/sync-pdf-resources.py` (protocolized-website) — backfills any remaining enrichment
6. **Publish**: `python3 scripts/migrate-to-d1.py --remote`

For new YouTube videos: already handled via `fetch_youtube_meta.py` + `enrich_youtube.py`. Run `sync-youtube-resources.py` in protocolized-website after enrichment to publish.

---

## What to Build Next

In priority order:

1. **`ingest/draft_resource.py`** (c3po) — for new PDFs dropped in `data/pdfs/`,
   write a Markdown stub to protocolized-website with enrichment pre-filled. Makes
   intake a one-drop flow rather than two-step manual entry.

2. **Wire Substack enrichment into forward pipeline** — after `sync_substack.py`
   enriches new posts, write resource Markdown stubs directly to protocolized-website.
   Until then, run `sync-substack-resources.py` manually after each c3po Substack sync.

3. **Add PDF + YouTube enrichment steps to daemon** — currently daemon handles only
   Discord. Detecting new files in `data/pdfs/` would allow new PDFs to auto-enrich
   without a manual `enrich_pdfs.py` run.

4. **Clean up 1970-01-01 sentinel dates** — `sync-substack-resources.py` writes
   `date: 1970-01-01` for posts in `enriched_meta` with no `api_metadata` entry (16
   posts). Resolve by running `fetch_api_metadata.py` in c3po to backfill those slugs,
   then re-running `sync-substack-resources.py`.

---

## Historical Note on Bootstrapping

The first round of YouTube enrichment sync (Session 19, 2026-06-15) updated 88 existing resource files and added 3 new ones. Descriptions replaced boilerplate ("A presentation exploring...") with Haiku summaries; real upload dates fetched via yt-dlp; 91 thumbnails uploaded to R2 `covers/yt-{id}.jpg`. Six library-only video resources (not in c3po's corpus) were left untouched.

PDF enrichment sync is the immediate next step — the enriched_meta already exists; only the protocolized-website sync script is missing.
