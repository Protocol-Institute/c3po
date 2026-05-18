# Plan: Magazine Lexicon Pass — Fiction and Nonfiction

**Status:** Ready to execute  
**Priority:** P2 (after full-corpus PDF pass completes)  
**Estimated cost:** ~$0.70 Haiku (one-time)

---

## What We're Capturing

The Protocolized magazine archive (116 posts) contains three analytically distinct content types that each yield different lexicon contributions:

| Type | Posts | What to extract |
|---|---|---|
| Protocol fiction | 72 | Fictional protocols, design fictions, memetically resonant concepts |
| Protocol theory / editorial | 47 | Coined terms, named framings, analytical positions — same as PDF pass |
| Protocol watching | 22 | Named real-world protocols observed through PI lens, key observations |
| Announcements / admin | ~30 | Skip |

The fiction pass is the novel challenge. The PI fiction is not decorative — it uses narrative to argue about protocol dynamics that resist direct exposition. "A Chronicle of Lumina," "The Clockless Clock Maze," and similar works introduce protocols as world-building elements that function as thought experiments. These have lexicon value, but they need to be clearly marked as fictional to prevent C3PO from treating them as real coordination mechanisms.

---

## Text Access

Full post HTML is available at `data/substack/posts/` in the format `{post_id}.{slug}.html`. The existing `ingest_substack.py` already has `plain_text_from_html()` for extracting clean text. The extraction script can reuse that function directly.

Post-to-category mapping comes from `sources/substack/enriched_meta.json` (Haiku-enriched categories) cross-referenced with `sources/substack/api_metadata.json` for titles, slugs, and dates.

---

## Filtering Strategy

**Fiction pass** — include if:
- `enriched_categories` contains `protocol-fiction`
- OR `substack_categories` (raw tags) contains fiction-related tags
- Skip: posts under 500 words (flash fiction fragments, announcement wrappers)

**Nonfiction pass** — include if:
- `enriched_categories` contains any of: `protocol-theory`, `editorial`, `protocol-watching`, `governance`, `research-report`, `organizations`, `technology-ai`, `memory-archival`, `interview`
- Skip: `announcement` only (no conceptual content)
- Skip: posts that are primarily link-roundups or event listings (wordcount heuristic: < 400 words)

**Both passes skip:**
- Posts already well-covered by the PDF corpus (same author writing about the same concept in a paper also indexed — acceptable duplication, not worth special-casing)

---

## Extraction Prompt: Fiction

```
You are analyzing a piece of protocol fiction from the Protocolized magazine. Extract three types of content:

1. FICTIONAL PROTOCOLS: Explicit coordination mechanisms, rules, or systems the characters in the story follow. For each:
   - name: what the protocol is called (or invent a short descriptive name if unnamed)
   - description: what it governs and how it works — roles, sequences, conditions, enforcement
   - function: what protocol-theoretic purpose it illustrates (what would this illuminate if real?)
   - fictional: true (always)

2. MEMETIC CONCEPTS: Ideas, terms, or framings introduced in the story that could be extracted and used analytically — not just plot elements, but concepts that illuminate real protocol dynamics through fictional exploration. These might become useful thought-experiment vocabulary.
   - term, definition, why_resonant

3. DESIGN FICTIONS: Speculative systems or scenarios in the story that function as thought experiments about protocolization — "what if coordination worked this way?" moments that have analytical value independent of the narrative.
   - name, scenario, protocol_insight

Be selective. Most narrative events don't qualify. Focus on what has conceptual utility beyond the story itself. A story about a community that votes using a specific ritual has a fictional protocol worth capturing; the plot about who wins the vote does not.

Return JSON: { "fictional_protocols": [...], "memetic_concepts": [...], "design_fictions": [...] }
If a category has nothing worth capturing, return an empty array for it.

Story title: {title}
Author: {author}

Story text:
---
{text}
```

---

## Extraction Prompt: Nonfiction (Theory / Editorial / Protocol Watching)

This uses the same base prompt as the PDF pass with two additions:

```
[Base prompt from extract_lexicon.py]

Additional instructions for magazine content:

4. PROTOCOL WATCHING OBSERVATIONS: If this piece analyzes a specific real-world protocol (a voting system, a medical triage protocol, an online platform governance mechanism, etc.), name the protocol and record the key analytical observation the author makes — the "what PI sees when it looks at this" insight. These are not term definitions but named analytical framings about specific cases.
   - protocol_name, domain, observation, source

5. EDITORIAL FRAMINGS: In editorial pieces, flag any named tensions, named phenomena, or analytical positions that give something a label it didn't have before — even if not formally defined. The act of naming is the contribution.

Return both the standard terms array AND an optional protocol_observations array.
```

---

## Output Schema

The current `lexicon_draft.json` schema is `{ term_key: { term, definition, source, source_slug, context } }`.

New fields added for magazine content:

```json
{
  "term": "the Memory Protocol",
  "definition": "A community ritual for collectively re-encoding traumatic events into shared narrative form, replacing personal memory with protocol-sanctioned communal memory.",
  "source": "Weaving Memory — Protocolized magazine",
  "source_slug": "weaving-memory",
  "source_type": "fiction",
  "fictional": true,
  "fiction_type": "fictional_protocol",
  "context": "...",
  "design_insight": "Illustrates how protocols can be used to manage collective grief — hardness applied to memory, not action."
}
```

For protocol-watching observations, stored separately in a new file `sources/protocol_observations.json`:

```json
{
  "twitter-community-notes": {
    "protocol_name": "Twitter/X Community Notes",
    "domain": "platform content moderation",
    "observation": "A rare example of a soft protocol operating at scale inside a hard-protocol substrate — community judgment is the soft layer; the algorithmic scoring is the hard enforcement layer.",
    "source": "Article Title",
    "source_slug": "slug"
  }
}
```

These observations don't belong in the lexicon (they're not term definitions) but are valuable for C3PO to surface in relevant queries, and for the resource page.

---

## Integration Plan

### lexicon_draft.json
Merge all new terms in. Fictional terms get `"fictional": true` — the curation step will decide which make it into the hosted lexicon and the system prompt.

### Hosted resource page (protocolized.io)
Add two new sections to `protocol-lexicon.md`:

**Section: "From Protocol Fiction"**
> *These are concepts and protocols from the Protocolized fiction archive. They are invented — speculative constructs that illuminate real protocol dynamics through narrative. They are included here because they have become part of the shared vocabulary of the PI research community.*

Then: fictional protocols and memetic concepts from fiction, with story attribution.

**Section: "Protocol Observations"**
> *The Protocolized magazine regularly applies PI analytical frameworks to specific real-world protocols. These named observations are collected here as a reference for what the corpus covers.*

### System prompt
Fictional terms are **not** added to the main PROTOCOL LEXICON block. They go in a small separate block after it:

```
FICTIONAL PROTOCOLS (from PI fiction archive — analytically useful but clearly speculative):
[10-15 most resonant fictional protocols and concepts, labeled "(fictional)"]
```

This keeps Claude from accidentally citing a fictional protocol as a real one, while still letting it discuss the fiction intelligently.

---

## Implementation

New script: `ingest/extract_magazine_lexicon.py`

Structure mirrors `extract_lexicon.py` with these differences:
- Text source: `data/substack/posts/*.html` → `plain_text_from_html()` from ingest_substack.py
- Post metadata: join `api_metadata.json` + `enriched_meta.json` by slug
- Two modes: `--type fiction` and `--type nonfiction` (default: both)
- Fiction output goes to `sources/lexicon_draft.json` with `fictional: true`
- Protocol observations go to `sources/protocol_observations.json`
- Posts with < 400 words auto-skipped (logged, not silently dropped)

---

## Cost Estimate

| Pass | Posts | Avg input tokens | Total input | Total output | Cost |
|---|---|---|---|---|---|
| Fiction | ~65 | 3,000 | ~195k | ~65k | ~$0.42 |
| Nonfiction | ~47 | 2,500 | ~118k | ~47k | ~$0.28 |
| **Total** | **~112** | — | **~313k** | **~112k** | **~$0.70** |

---

## Open Questions

1. **Fictional protocol naming:** Some PI fiction features unnamed protocols (characters just do them). Should Haiku invent descriptive names for these, or leave them description-only? Recommendation: invent short descriptive names in brackets, e.g., "[the Lumina rotation protocol]" — clearly provisional.

2. **Fiction/theory boundary:** Several posts are hybrid — framed as fiction but with explicit analytical commentary. Should these run through both passes or just one? Recommendation: run through the fiction pass only; the analytical content will surface naturally as memetic concepts.

3. **Protocol observations format:** The observation output is less structured than term definitions. Worth trying both structured JSON and free-text paragraph formats to see which Haiku handles more accurately. Lean toward structured for now; can convert to prose later.

4. **Curation workflow:** The magazine fiction likely produces 100–150 fictional protocols/concepts. That's too many for the resource page without curation. Need a second-pass curation session — same as the current state of the PDF lexicon.

5. **System prompt fictional block size:** 10–15 fictional terms is a guess. After curation, the right number depends on how resonant they are and whether users actually ask about them. May be zero in the first version; add when users demonstrate interest.
