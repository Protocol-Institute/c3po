# SIG Meeting Capture Protocol

**Status:** Design approved — pending discussion with SIG hosts before implementation  
**Drafted:** 2026-06-17  
**Applies to:** All PI SIGs (SIGFPT as reference implementation)

---

## Problem with the Current Approach (Roam)

Roam is a personal thinking tool, not a structured data source. The JSON export schema is whatever whoever took notes decided that day — some pages have vocabulary blocks, Granola summaries, ChatGPT summaries, both, or neither. Extraction logic has to handle all variants defensively and still misses things. No incremental sync; snapshot-only.

---

## Design Principle: Separate Capture from Enrichment

| Layer | Who does it | What they provide |
|-------|-------------|-------------------|
| **Capture** | Facilitator | Irreplaceable inputs: reading list, participants, brief notes on what was covered/surprising |
| **Enrichment** | c3po pipeline | AI summary, vocabulary, corpus connection, Pinecone ingest |

Humans provide what AI cannot reliably reconstruct from a transcript. c3po does the heavy lifting using the full corpus as context.

---

## Capture Format: Structured Markdown with YAML Frontmatter

### Repository structure

A dedicated GitHub repo: **`Protocol-Institute/sig-notes`**

```
sig-notes/
  _template.md                              ← copy this to start each meeting file
  sigfpt/
    2025-06-27-observability.md
    2025-06-27-observability-transcript.txt ← raw transcript, alongside the notes file
    2025-07-12-notation.md
    ...
  mrg/
    ...
  sigpfb/
    ...
```

Separate repo (not inside c3po) so SIG facilitators can commit meeting notes without needing c3po codebase access.

### `_template.md`

```yaml
---
sig: SIGFPT                         # SIG identifier — lowercase slug
date: YYYY-MM-DD
topic: ""
participants: []                    # list of names
reading:
  - title: ""
    url: ""
discord_thread: ""                  # Discord thread ID (18-digit integer, as string)
transcript_file: ""                 # filename of raw transcript if present, else omit
recording_url: ""                   # Zoom/Meet recording URL if available, else omit
---

## Facilitator notes
[1–3 sentences: what the session actually covered, what surprised you, what was left unresolved.
Not a full summary — just what the AI can't infer from the transcript.]

## Key questions raised
- [Optional — bullet list of open questions the group was wrestling with]
```

**Required frontmatter fields:** `sig`, `date`, `topic`  
**Optional but valuable:** `participants`, `reading`, `discord_thread`, `transcript_file`  
**Free text sections:** low-pressure — 2–4 sentences is enough

---

## Transcript Processing Pipeline

Script: `ingest/process_sig_meeting.py`

```bash
python3 ingest/process_sig_meeting.py sig-notes/sigfpt/2025-06-27-observability.md
python3 ingest/process_sig_meeting.py sig-notes/sigfpt/2025-06-27-observability.md --dry-run
```

### Pipeline steps

1. Parse frontmatter → structured metadata
2. Load transcript file if `transcript_file` is set
3. Query Pinecone with the meeting topic → retrieve top-k related corpus items across all namespaces (papers, definitions, Substack posts, past SIG discussions, videos)
4. Feed to Claude:
   - Input: `[raw transcript] + [related corpus context from Pinecone]`
   - Output: `{summary, vocabulary, corpus_links, key_concepts}`
   - The corpus context is the key differentiator — Claude can annotate "when the group discussed X, that maps to what the USoP essay calls Y" or "this concept appears in the Process Calculi paper at [url]"
5. Write `data/sig_meeting_notes/{sig}/{date}.json` — the enriched structured record (tracked in c3po repo)
6. Ingest the **enriched version** to Pinecone `sig` namespace — not the raw transcript

### Output JSON schema

```json
{
  "sig": "SIGFPT",
  "date": "2025-06-27",
  "topic": "Observability",
  "participants": ["Venkatesh Rao", "Rich McDowell", "..."],
  "reading_list": [
    {"title": "...", "url": "...", "note": ""}
  ],
  "summary": "...",
  "vocabulary": [
    {"term": "Observability", "definition": "..."}
  ],
  "key_concepts": ["...", "..."],
  "corpus_links": [
    {"title": "...", "url": "...", "relevance": "..."}
  ],
  "discord_thread": "1387142362937032714",
  "source": "sig_meeting_capture"
}
```

The raw transcript never goes to Pinecone. What goes to Pinecone is the AI-processed version with corpus connections embedded — higher signal, no noise.

---

## Generalizing to All SIGs

The same template and pipeline work for all SIGs. Differences are configuration, not structure:

| SIG | `sig:` slug | Typical reading namespaces | Produces transcripts? |
|-----|-------------|---------------------------|----------------------|
| SIGFPT | `sigfpt` | `pdfs`, `definitions`, `sig` | Yes (Discord chat logs) |
| MRG | `mrg` | `substack`, `videos`, `discord_links` | TBD |
| SIGPfB | `sigpfb` | `pdfs`, `substack` | TBD |
| ProtFiSIG | `protfisig` | `pdfs`, `bibliography` | TBD |
| SIGPSY | `sigpsy` | `pdfs`, `substack` | TBD |
| DRG | `drg` | `discord_links`, `substack` | TBD |

Per-SIG namespace query weights can be configured in `config/sig_query_weights.json` (new file, not yet created).

---

## SIGFPT-Specific Implementation Notes

### Bootstrapping from Roam

The Roam export (`c3po_inbox/ProtocolTheory-2026-06-17-14-32-07.json`) contains 14 meeting topic pages with rich content. One-time backfill plan:

1. Convert each Roam topic page to `_template.md` format — frontmatter from SIG master schedule, free text from AI summary block
2. Where raw transcripts exist in Roam (Observability, Process Calculi, Paper Napkin), run them through `process_sig_meeting.py` as `transcript_file`
3. Commit the resulting `sig-notes/sigfpt/` files and the enriched `data/sig_meeting_notes/sigfpt/*.json`
4. Retire the Roam inbox file

This uses the new pipeline to ingest the old data, validating the design before the first live meeting uses it.

### Discord thread IDs

The Roam SIG master page has thread IDs for June–August 2025 sessions. These should be preserved in the backfilled `.md` files. Later sessions in the master page lack thread IDs — they can be looked up in Discord if needed.

### Trigger cadence

SIGFPT meets biweekly (alternate Fridays). Facilitator commits the meeting notes file within 48 hours of the session. `process_sig_meeting.py` is run manually for now; can be wired to GitHub Actions webhook later (post-VPS migration).

---

## Open Decisions (for SIG host discussion)

1. **Repo location:** `Protocol-Institute/sig-notes` (recommended) vs `sig-notes/` subfolder inside c3po. Separate repo lowers barrier for non-technical facilitators.

2. **Transcript format:** Discord chat logs are plain text (easily pasted). Zoom transcripts are VTT/SRT. Granola notes are markdown. The pipeline should handle all three — need to decide which formats to standardize on, or whether to auto-detect.

3. **Who runs the processing pipeline:** c3po admin (VGR) runs `process_sig_meeting.py` after each session, or each SIG host runs it themselves. Starting with centralized (admin-run) makes sense; distribute later when the workflow is proven.

4. **Trigger automation:** Manual for now. When the VPS migration is complete (`plans/vps-migration.md`), wire up a GitHub Actions webhook that fires `process_sig_meeting.py` whenever a new `.md` is committed to `sig-notes/`.

5. **Website rendering:** Enriched `data/sig_meeting_notes/*.json` will feed into the SIGFPT meeting pages on protocol-institute.org. Rendering changes belong in `protocol-institute/protocolized-website/` per the c3po-writes-JSON-only rule (`plans/website-interface.md`).
