# Plan: Exhibit Extraction — Figures, Tables, Lists, Section Summaries

**Status:** Revised (2026-05-20)
**Priority:** High
**Trigger:** "10 dimensions of sufficiency" failure — query returned partial enumerated list cut at chunk boundary; bot refused to fabricate missing items but couldn't enumerate a canonical framework from its own corpus. Revised to cover the full space of structured exhibit artifacts: figures, tables, lists, and section summaries.

---

## Problem Statement

The current ingest pipeline has two chunk types per document: body chunks (512-token windows) and a `doc_summary` vector. This handles prose retrieval well but fails for **structural queries**:

- "What are the 10 dimensions of sufficiency?" → framework split across ~12 body chunks, no single unit covers all items
- "Describe the diagram on page 6 of the Sufficiency paper" → no figure description exists in the index
- "What does section 3 of the Protocol Reader argue?" → section heading and body are in different chunks

The fix is to index **exhibit artifacts** — well-defined, self-contained units of structured knowledge from the corpus.

---

## Corpus Reality (from sampling)

Sampling five representative PDFs revealed:

**Background image pattern:** Every SoP paper embeds 20–30 full-page images (≈626×810px for standard letter-format papers) as decorative backgrounds from the publication design. These are NOT content figures.

**Content images:** After filtering out backgrounds, each paper has 4–13 smaller images (typically 150–280px), representing actual diagrams, illustrations, or embedded figures.

**Visual pages:** Most papers have ~3 pages with <50 words of extractable text — these are full-page illustration spreads where the visual IS the content.

**pdfplumber tables unreliable:** `extract_tables()` picks up typographic design elements (letter grids, decorative rules) as tables. One paper's "13 tables" were entirely the letters of the word "ADDRESSABLE SPACE" in a grid title. Programmatic table extraction is not viable; vision detection is required.

**Paper types in corpus:**
- *Text-essay papers* (Protocol Reader intro, Unreasonable Sufficiency, Killswitch, etc.): dense text, few embedded figures, worth section summaries
- *Design-heavy SoP submissions* (most): full-page backgrounds, scattered small figures, section structure less formal
- *Visual-first papers* (FERNANDEZ Flow brochure, some speculative pieces): images are the primary content

---

## Exhibit Types

Four chunk types, all stored in the existing `pdfs` Pinecone namespace (additive, no schema changes):

| Type | Unit | Trigger |
|---|---|---|
| `section_summary` | One per major section | Core essays only (>12 text pages) |
| `list_exhibit` | One per enumerated framework or list | All text-bearing PDFs |
| `figure_exhibit` | One per content image | Pages with qualifying images |
| `table_exhibit` | One per data table | Vision-detected on qualifying pages |

---

## Extraction Strategy: Two Passes Per Paper

### Pass 1 — Text (Haiku, no vision)

For each of the 77 PDFs with extractable text:

1. Extract full text with pdfplumber (same as existing pipeline)
2. Call Haiku with a combined prompt that produces JSON with two keys:
   - `sections`: for core essays only — list of `{num, title, summary, key_claim}`
   - `lists`: all papers — list of `{title, context, items: [...verbatim...]}` for any enumerated framework with 3+ distinct substantive items

**Core essay filter:** Papers where pdfplumber extracts text from >12 pages. Approximately 15–20 papers based on the corpus scan. Passed to Haiku as a flag in the prompt.

**Output:** `sources/pdfs/structure_meta.json` — keyed by slug, merged with Pass 2 output.

---

### Pass 2 — Vision (Haiku with images, via PyMuPDF)

**Dependency:** `pip install pymupdf` (not yet in venv)

**Qualifying pages** — a page qualifies for a vision call if either condition holds:
- It has at least one content image: an embedded image object that is NOT a full-page background (filter: width > 80% of page width AND height > 80% of page height → background, skip) AND NOT a tiny mark (filter: either dimension < 40px → skip)
- It is a visual spread: the page extracts fewer than 50 words of text AND is not a cover/title page (detected by position: not page 1, or page 1 without standard cover patterns)

**Per qualifying page:**
1. Render page to PNG at 150 DPI using `fitz.open()` → `page.get_pixmap()`
2. Send PNG + surrounding context (paper title, page number, text from adjacent pages) to Haiku vision
3. Haiku prompt asks:
   - Identify and describe any content figures (diagrams, charts, maps, illustrations, network graphs, timelines)
   - Identify and reproduce any data tables (structured rows/columns with substantive content — NOT decorative typography)
   - For each exhibit: type, caption (if visible in image), description of what it shows, what argument or claim it supports
   - Return JSON: `{figures: [{caption, description, exhibit_type}], tables: [{caption, content_markdown, description}]}`
4. Skip pages where Haiku returns empty arrays (no exhibits found)

**Output:** Merged into `sources/pdfs/structure_meta.json`.

---

## Embedding Text Formats

### `section_summary`
```
Document: {title} ({primary_author}, {year})
Section {num}: {section_title}
Summary: {2-sentence summary of argument}
Key claim: {verbatim quote or distilled key point}
```
Metadata: `chunk_type`, `section_num`, `section_title`, `doc_slug`, `url`, `title`, `primary_author`, `date`

### `list_exhibit`
```
Document: {title} ({primary_author}, {year})
List: {list_title}
Context: {1 sentence describing what this list is}
Items:
  1. {item verbatim}
  2. {item verbatim}
  ...
```
Metadata: `chunk_type`, `list_title`, `item_count`, `doc_slug`, `url`, `title`, `primary_author`, `date`

### `figure_exhibit`
```
Document: {title} ({primary_author}, {year})
Figure (page {page}): {caption if available, else "Untitled figure"}
Type: {diagram | chart | illustration | map | timeline | other}
Description: {2–4 sentence description of what the figure shows}
Context: {1 sentence on what argument or claim this figure supports}
```
Metadata: `chunk_type`, `page`, `figure_type`, `caption`, `doc_slug`, `url`, `title`, `primary_author`, `date`

### `table_exhibit`
```
Document: {title} ({primary_author}, {year})
Table (page {page}): {caption if available}
Description: {1 sentence on what the table presents}
Content:
{markdown table}
```
Metadata: `chunk_type`, `page`, `caption`, `doc_slug`, `url`, `title`, `primary_author`, `date`

---

## Worker Integration

### Retrieval changes (`buildContextBlock()`)

- `list_exhibit`: render all items verbatim, no truncation, prepend `[COMPLETE LIST]` marker
- `figure_exhibit`: render description + context in full, prepend `[FIGURE]` marker
- `table_exhibit`: render markdown table + description, prepend `[TABLE]` marker
- `section_summary`: include as normal context, no special marker needed

### Secondary retrieval

- `list_exhibit` hit: no follow-up — the list IS the complete answer
- `figure_exhibit` hit: no body-chunk follow-up needed (description is self-contained); optionally surface which pages to look at
- `section_summary` hit: trigger body-chunk follow-up filtered by `doc_slug` + `section_num` to get full section prose

### System prompt addition

```
EXHIBIT TYPES IN RETRIEVED CONTEXT:
[COMPLETE LIST] — a verbatim enumeration extracted from the source. Reproduce all items exactly. Do not add, remove, or reorder items.
[FIGURE] — a vision-generated description of a diagram or illustration. Describe what the figure shows; note it is a figure from the source.
[TABLE] — structured data from a source table. Reproduce faithfully.
```

---

## Implementation Plan

### Files

| File | Purpose |
|---|---|
| `ingest/extract_structure.py` | Runs Pass 1 (text) and Pass 2 (vision); writes `structure_meta.json` |
| `ingest/ingest_structure.py` | Reads `structure_meta.json`; upserts vectors to Pinecone `pdfs` namespace |
| `sources/pdfs/structure_meta.json` | Intermediate output (gitignored if large) |

### Run order

```bash
source .venv/bin/activate
pip install pymupdf
python3 ingest/extract_structure.py   # ~20–30 min; writes structure_meta.json
python3 ingest/ingest_structure.py    # ~5 min; upserts to Pinecone
```

Idempotent: both scripts key by `{slug}__{chunk_type}__{index}` and upsert, not insert.

---

## Cost Estimate

| Pass | Tokens | Cost |
|---|---|---|
| Pass 1 text (Haiku): 77 PDFs × ~10k tokens avg | ~770k input + ~50k output | ~$0.82 |
| Pass 2 vision (Haiku): ~300 qualifying pages × ~1k image tokens + context | ~400k tokens | ~$0.50 |
| **Total** | | **~$1.30** |

One-time cost. Re-run only on new or changed PDFs (idempotency check by slug + content hash).

---

## Estimated Output

| Type | Estimate |
|---|---|
| `section_summary` | ~200 (15–20 core essays × ~10 sections avg) |
| `list_exhibit` | ~80 |
| `figure_exhibit` | ~150–250 |
| `table_exhibit` | ~20 |
| **Total new vectors** | **~450–550** |

---

## Open Questions

1. **Vision model:** Haiku 4.5 supports images. Is quality sufficient for diagram description, or do certain figures (complex network graphs, dense timelines) warrant Sonnet? Recommendation: start with Haiku, spot-check 10 figure_exhibit outputs, upgrade selectively.

2. **FERNANDEZ-type visual brochures:** Pages are all image-dense (70 content images, 0 background images). Every page qualifies for vision. Treat as a single exhibit pass with one figure_exhibit per significant spread rather than per image object.

3. **Section summary scope:** Current filter is >12 text pages. Verify against slug list before running — some long papers may be visual-first and not worth section summaries.

4. **Caption detection:** Captions are not embedded metadata in these PDFs — they appear as text near the figure. Pass 2 should include adjacent page text in the vision prompt context to improve caption detection.

---

## Dependencies

- Existing: `ingest/ingest_pdfs.py`, `sources/pdfs/enriched_meta.json`, `data/pdfs/*.pdf`
- New package: `pymupdf` (fitz)
- New scripts: `ingest/extract_structure.py`, `ingest/ingest_structure.py`
- New intermediate file: `sources/pdfs/structure_meta.json`
- Worker changes: `buildContextBlock()`, system prompt

**Not blocked by anything.** Independent of Discord/SIG work.
