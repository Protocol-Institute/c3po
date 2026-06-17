# Roam Graph Ingest Plan — SIGFPT

**Source:** `c3po_inbox/ProtocolTheory-2026-06-17-14-32-07.json`  
**Graph:** Protocol Institute / SIGFPT working graph  
**Inspected:** 2026-06-17  
**Status:** Planning — not yet implemented

---

## What's in the Export

165 pages, classified:

| Category | Count | Action |
|----------|-------|--------|
| Meeting topic pages (rich content) | 14 | **Ingest** |
| Protocol Foundations Workshop pages | ~8 | **Ingest** |
| Standalone concept pages | ~5 | **Ingest** |
| Raw transcripts (Observability, Process Calculi, Paper Napkin) | 3 | Skip — too noisy |
| Daily journal stubs (empty) | 52 | Skip |
| Person stubs (mostly empty) | ~38 | Skip |
| Protocol School lists | 2 | Skip — person/project lists |
| SIGFPT Member Directory | 1 | Skip — personal info |
| Protocol School Commencement Speech | 1 | Ingest (standalone essay) |
| Misc concept stubs | ~10 | Ingest if >5 children |

**Meeting topic pages (14):**  
Observability, Notation, Paper Napkin Math, Process Calculi, Impossibilities and Symmetries,
Maneuver Automata, Actor Models, Stigmergy, Protocolizing Agent Space, Atomic Protocol Questions,
Linear logic, Protocol Homework Problem Sets, Autocurricula, Design Principles for Protocols

**Standalone concept pages:**  
Efficiency Drift, Protocols and Drift, Do-Calculus, Feedgap Loop, Pebble Automata

**Workshop pages:**  
Edge Esmeralda 2025 Workshop, Protocol Foundations Workshop (sessions 1–4 embedded), PFW Idea Index

---

## Content Structure (per meeting topic page)

Each meeting topic page typically has:

```
ChatGPT Summary
  Facilitator / Participants
  Topic overview
  🔑 Key Concepts  [vocab list]
  🧠 Highlights from Discussion
    [per-participant contributions]
  🧪 Exercise
    [exercise prompt + participant responses]
Agenda  [sometimes contains reading list + paper summaries]
Summary (Granola)  [bullet-point summary of discussion arcs]
Reading [links to papers + ChatGPT summaries]
Vocabulary  [explicit term list for some sessions, e.g. Stigmergy]
```

Sections vary by session; not all sessions have all sections.

---

## Pinecone Ingestion Plan

### Namespace
All Roam content → **`sig`** namespace (alongside existing SIGFPT Discord/website vectors).

### Vector ID scheme
`roam__{page_uid}__{chunk_type}` — stable across re-runs; prevents duplication.

### Chunking strategy

Each meeting topic page produces **up to 4 chunks**, by section:

| `chunk_type` | Content | Typical size |
|---|---|---|
| `roam_summary` | ChatGPT/Granola AI summary block | 400–800 chars |
| `roam_discussion` | Key concepts + participant contributions + exercises | 800–2000 chars |
| `roam_vocabulary` | Glossary terms + definitions (when present) | 300–1500 chars |
| `roam_reading` | Reading list items + per-item ChatGPT summaries | 300–800 chars |

Workshop and concept pages: single chunk, `chunk_type = roam_workshop` or `roam_concept`.

### Metadata schema per vector

```json
{
  "source":      "roam",
  "sig":         "SIGFPT",
  "chunk_type":  "roam_summary | roam_discussion | roam_vocabulary | roam_reading | roam_workshop | roam_concept",
  "title":       "Observability",
  "date":        "2025-06-27",
  "url":         "https://protocol-institute.org/sigs/sigfpt/2025-06-27-observability-for-protocols",
  "roam_uid":    "abc123xyz"
}
```

`url` comes from `data/sig_pages_state.json` matched on date (see matching strategy below).  
`date` comes from SIG master page schedule mapping.

### Date/URL matching strategy

The SIG master page (`Formal Protocol Theory Special Interest Group (SIG)`) contains the canonical schedule:

```
June 27: [[Observability]] | Discord thread 1387142362937032714
July 12: [[Notation]] | Discord thread ...
```

Algorithm:
1. Parse master page → `{roam_title: [list of (date, discord_thread_id)]}` (multi-session topics like Process Calculi appear multiple times)
2. Load `data/sig_pages_state.json` → `{url: {date: "2025-06-27", ...}}`
3. For each Roam page, match on date → get website URL
4. Multi-session pages (Process Calculi appears Aug 8, Sep 5, Oct 3; Stigmergy appears May 1, 15, 27; Atomic Protocol Questions Oct 31, Feb 6): a single Roam page covers multiple meetings. Assign the **most recent** date for the main meeting record; tag with `multi_session: true` and include all dates in metadata.

### Text extraction

```python
def flatten_blocks(blocks: list, skip_patterns: list = None) -> str:
    """Recursively flatten Roam nested blocks to plain text."""
    lines = []
    for block in blocks:
        text = block.get("string", "").strip()
        # Skip Firebase image URLs
        if text.startswith("![](https://firebasestorage"):
            continue
        # Skip mermaid/widget blocks
        if text.startswith("{{"):
            continue
        # Strip [[page references]] → page name
        text = re.sub(r'\[\[([^\]]+)\]\]', r'\1', text)
        # Strip ((block references))
        text = re.sub(r'\(\([^)]+\)\)', '', text)
        # Strip markdown emphasis but keep content
        text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
        text = re.sub(r'__(.+?)__', r'\1', text)
        if text:
            lines.append(text)
        lines.extend(flatten_blocks(block.get("children", [])))
    return "\n".join(lines)
```

### Estimated vector count

| Category | Pages | Chunks/page | Vectors |
|---|---|---|---|
| Meeting topic pages | 14 | ~3 avg | ~42 |
| Workshop/PFW pages | 8 | 1 | ~8 |
| Standalone concept pages | 6 | 1 | ~6 |
| **Total** | | | **~56** |

sig namespace currently: ~5,588 vectors. This adds < 1%.

---

## New Script: `ingest/sync_roam.py`

```
python3 ingest/sync_roam.py                    # dry-run (default)
python3 ingest/sync_roam.py --upsert           # write to Pinecone
python3 ingest/sync_roam.py --dump-json        # write enrichments JSON only
python3 ingest/sync_roam.py --page Observability  # single page
```

**Inputs:**
- `c3po_inbox/ProtocolTheory-2026-06-17-14-32-07.json` — Roam export
- `data/sig_pages_state.json` — for URL matching

**Outputs:**
- Pinecone `sig` namespace (when `--upsert`)
- `data/roam_enrichments.json` — website enrichment data (always written; see below)

**Script structure:**
1. `load_roam(path)` → page dict
2. `parse_master_schedule(pages)` → `{title: [{date, discord_thread_id}]}`
3. `build_url_index(sig_pages_state)` → `{date_iso: url}`
4. `extract_sections(page)` → `{summary, discussion, vocabulary, reading_list}` (each as string)
5. `make_chunks(page, sections, date, url)` → list of `{id, text, metadata}`
6. `upsert_chunks(chunks)` via Pinecone batch upsert (batch=50)
7. `write_enrichments(all_pages, date_url_index)` → `data/roam_enrichments.json`

Add `sync_roam.py` to daemon's job list once tested manually.

---

## Website Enrichment Plan

Per **`plans/website-interface.md`**: c3po writes JSON only; the website renders. This plan follows that contract.

### Output: `data/roam_enrichments.json`

Keyed by website URL. Each entry contains structured data the website can render:

```json
{
  "https://protocol-institute.org/sigs/sigfpt/2025-06-27-observability-for-protocols": {
    "roam_title": "Observability",
    "date": "2025-06-27",
    "participants": ["Venkatesh Rao", "Rich McDowell", "Patrick Nast", "Timber", "Seth Killian"],
    "session_summary": "...",
    "reading_list": [
      {
        "title": "Observability (Wikipedia)",
        "url": "https://en.wikipedia.org/wiki/Observability",
        "note": ""
      }
    ],
    "vocabulary": [
      {
        "term": "Observability",
        "definition": "The degree to which the internal state of a system can be inferred from its external outputs."
      }
    ],
    "key_concepts": ["Observability vs controllability", "Partial observability in card games", "Engineering observability"]
  }
}
```

Fields:
- `participants` — extracted from the ChatGPT summary block's "Participants:" line
- `session_summary` — flatten of ChatGPT summary block (1–3 paragraphs)
- `reading_list` — list of URLs extracted from reading/agenda blocks
- `vocabulary` — from explicit "Vocabulary" or "🔑 Key Concepts" blocks; Stigmergy has 16 entries
- `key_concepts` — 3–5 bullet strings from key concepts section

### Matching: Roam page → website URL

Not all 14 Roam meeting pages have a clean 1:1 match to one website page. Cases:

| Pattern | Example | Handling |
|---|---|---|
| 1 Roam page = 1 meeting | Observability, Notation | Direct date match |
| 1 Roam page = N consecutive meetings (same topic) | Process Calculi (3 sessions), Stigmergy (3 sessions), Atomic Protocol Questions (2 sessions) | Attach enrichment to **all** matching website pages; vocabulary/summary applies to the whole topic series |
| Roam page exists but website page is from a different date range | Actor Models (Dec 12 in Roam, website has Dec 12 page) | Match by exact date |
| No date in master schedule | Design Principles for Protocols | Skip URL linkage; ingest to Pinecone without URL metadata |

### Website rendering changes (protocolized-website scope)

The following changes are needed in **`protocol-institute/protocolized-website/`**, not here:

1. **`scripts/sync-sig-enrichments.py`** (new) — reads `data/roam_enrichments.json` from c3po repo (via `C3PO_ROOT` env var pattern, same as existing sync scripts), and writes enrichment data into D1 alongside each meeting page record.

2. **SIGFPT meeting page template** — add optional sections:
   - "Reading for this session" (from `reading_list`)
   - "Key vocabulary" (collapsible accordion, from `vocabulary`)
   - "Participants" (from `participants`)
   These should only render when the enrichment JSON has data for that meeting.

3. The `generate_sig_pages.py` script in c3po currently generates HTML — per `plans/website-interface.md` this needs to be refactored to write pure JSON. That refactor should happen alongside this work (same PR).

---

## Rollout Sequence

1. **Write `ingest/sync_roam.py`** — date parsing + section extraction + dry-run output
2. **Verify date matching** — run dry-run, compare against `sig_pages_state.json`; confirm all 14 topic pages match to at least one website URL
3. **Review enrichments JSON** — skim `data/roam_enrichments.json` before first Pinecone write
4. **Upsert to Pinecone** — `python3 ingest/sync_roam.py --upsert`
5. **Spot-check** — query `sig` namespace for "stigmergy vocabulary" or "observability key concepts"; verify roam chunks surface
6. **Commit enrichments JSON** — tracked in repo for website to consume
7. **Website rendering** (separate session, protocolized-website scope) — add sync script + template changes
8. **Add to daemon** — after manual testing passes

---

## Open Questions

- **Multi-session pages**: For topics with 3 sessions (Process Calculi, Stigmergy), the Roam page reflects the full topic arc, not individual sessions. The enrichment will be the same on all 3 website pages — acceptable as a starting point.
- **No-URL concept pages**: Efficiency Drift, Protocols and Drift, Do-Calculus — these have no corresponding website page. They'll be Pinecone-only until we create standalone concept pages on the website.
- **Reading list URLs only**: Firebase-hosted images are inaccessible externally. The plan already skips these. External links (gist.github.com, Wikipedia, arxiv.org) should all be valid and can be included in enrichment JSON.
- **Incremental updates**: Roam is a live graph. The current plan treats the export as a one-time snapshot. When a new export is provided, `sync_roam.py` should re-upsert (idempotent by vector ID). No auto-sync yet.
