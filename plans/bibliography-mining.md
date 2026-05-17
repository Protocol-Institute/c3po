# Plan: PDF Bibliography Mining

**Status:** Planned (start after YouTube ingest complete)  
**Namespace:** `bibliography`  
**Scripts:** `ingest/mine_bibliography.py`, `ingest/fetch_refs.py`, `ingest/ingest_bibliography.py`

---

## Context

82 PDFs are in the corpus. 77 have extractable text; 16 have formal "References" sections. The rest have inline citations, footnotes, or endnotes. Each PI paper cites an academic literature that provides the theoretical grounding for protocol studies — Easterling's *Extrastatecraft*, Yates & Murphy, Alexander's *Pattern Language*, Bratton's *Stack*, etc. Mining these and surfacing their summaries extends the effective reach of the corpus without requiring new PI publications.

The key challenge is relevance filtering: a paper on coal mining safety will cite OSHA regulations, statistical methods, and industrial history — only a subset are actually illuminating for protocol studies. Haiku scores each reference.

---

## Pipeline (3 scripts)

### Script 1: `mine_bibliography.py`

Extract and score all references from PDFs.

**Input:** `data/pdfs/*.pdf` + `sources/pdfs/enriched_meta.json`  
**Output:** `sources/bibliography/raw_refs.json`, `sources/bibliography/scored_refs.json`

Steps:

**1a. Extract references with Haiku**  
For each PDF with extractable text (77 total):
- Extract full text with pdfplumber.
- Send last 4,000 chars (most likely to contain references/bibliography) + first 500 chars (title/authors context) to Haiku.
- Prompt: "Extract all cited works. For each return: `title`, `authors` (list), `year` (int or null), `venue` (journal/conference/book), `doi` (if visible), `url` (if visible). Return a JSON array."
- Append to `raw_refs.json` with `source_pdf` field.
- Deduplicate across PDFs by normalized title (lowercased, punctuation stripped). Track `cited_by: [pdf_list]` and `citation_count`.

Estimated: 77 PDFs × ~2,000 input tokens ≈ 154K tokens → ~$0.02 Haiku.

**1b. Score protocol relevance with Haiku**  
For each unique deduplicated reference (est. 400–800):
- Send `{title, authors, year, venue}` to Haiku.
- Prompt: "Score relevance to protocol studies (0–3): 0=incidental, 1=adjacent, 2=relevant, 3=core. Return JSON: `{score, rationale}`. Protocol studies covers: rule systems, coordination mechanisms, standards, infrastructure, governance, communication protocols, social norms as protocols, protocol theory and design."
- Save to `scored_refs.json` with score + rationale.
- **Threshold:** Score ≥ 2 advances to sourcing. Score 1 = store metadata only. Score 0 = discard.

Estimated: ~600 refs × ~200 tokens ≈ 120K tokens → ~$0.015 Haiku.

---

### Script 2: `fetch_refs.py`

Attempt to source documents for scored references (score ≥ 2).

**Input:** `sources/bibliography/scored_refs.json`  
**Output:** `sources/bibliography/sourced_refs.json`, `sources/bibliography/pdfs/{ref_id}.pdf`

For each relevant reference, try in order:

**2a. DOI → Unpaywall API** (free, no key needed)  
`https://api.unpaywall.org/v2/{doi}?email=vgururao@gmail.com`  
If `best_oa_location.url_for_pdf` exists → download PDF.

**2b. Semantic Scholar API** (free, 100 req/5min)  
Search by title: `https://api.semanticscholar.org/graph/v1/paper/search?query={title}&fields=title,authors,year,abstract,openAccessPdf,externalIds`  
- If `openAccessPdf.url` → download PDF.
- Always capture `abstract` for abstract-only vector.

**2c. arXiv** (for CS/AI papers)  
If venue contains "arxiv" or authors suggest CS: try `http://export.arxiv.org/find/all?search_query=ti:{title}`.

Record outcome in `sourced_refs.json`: `status` = `pdf_downloaded | abstract_only | not_found`.

---

### Script 3: `ingest_bibliography.py`

Embed and upsert to namespace `bibliography`.

**Input:** `sources/bibliography/sourced_refs.json`  
**Two vector types:**

**For PDF-sourced references:** Run through existing PDF ingest pattern (body chunks + doc_summary). These go into namespace `bibliography` not `pdfs` to keep provenance clear.

**For abstract-only references:**  
Single `ref_summary` vector per reference.  
Text: `"Title: {title}\nAuthors: {authors}\nYear: {year}\nVenue: {venue}\n\nAbstract: {abstract}\n\nRelevance: {rationale}"`  
Metadata: `source=bibliography`, `chunk_type=ref_summary`, `title`, `authors`, `year`, `venue`, `doi`, `cited_by` (list of PI PDFs), `citation_count`, `relevance_score`.  
ID: `ref__{normalized_title_hash}`

---

## Pinecone Impact

Estimates (wide range due to unknown sourcing success rate):
- Score ≥ 2 references: ~150–250
- PDF download rate: ~30–50% (open access varies widely)
- Abstract-only: ~100–175 vectors
- PDF body chunks (at ~10 chunks/paper avg): ~500–1,500 vectors

**Estimated: ~600–1,700 new vectors** in namespace `bibliography`.

---

## Sources Directory Layout

```
sources/bibliography/
  raw_refs.json       # all extracted references, with source_pdf and citation_count
  scored_refs.json    # with relevance scores from Haiku
  sourced_refs.json   # with download status, abstract, pdf path
  pdfs/               # downloaded reference PDFs (gitignored)
```

Add `sources/bibliography/pdfs/` to `.gitignore`.

---

## Key Design Decisions

**Why a separate `bibliography` namespace?** Keeps provenance clear — vectors from PI's own corpus (pdfs, substack, videos) vs. externally-sourced reference material. Allows retrieval weighting (PI-authored content gets higher tier than bibliography).

**Why Haiku for extraction, not regex?** Reference formats vary wildly across PI papers — Chicago, APA, numbered footnotes, MLA. Haiku handles all formats in a single pass; regex would need format-specific parsers for each PDF's style.

**Deduplication strategy:** Normalize title (lowercase, strip punctuation, collapse whitespace) → SHA-256 prefix as canonical ID. DOIs override title-based dedup when available.

**False positive risk:** Some PI papers cite works in fields like coal mining, clinical medicine, or electoral systems where the connection to protocol studies is tenuous. Haiku's score + rationale provides an auditable trail. Hand-review `score=2` cases after the scoring pass if quality seems off.
