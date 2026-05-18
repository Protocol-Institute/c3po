"""
Mine bibliographic references from Protocol Institute PDFs.

Two modes:
  --mode explicit  Extract from formal References/Bibliography sections (fast, ~6 PDFs)
  --mode inline    Extract all cited works from full PDF text (slower, ~71 PDFs)

Both modes append to the same sources/bibliography/raw_refs.json and then run
a Haiku relevance-scoring pass to produce sources/bibliography/scored_refs.json.

Usage:
    python3 ingest/mine_bibliography.py --mode explicit
    python3 ingest/mine_bibliography.py --mode inline
    python3 ingest/mine_bibliography.py --mode explicit --score-only
    python3 ingest/mine_bibliography.py --mode explicit --dry-run
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import pdfplumber
import anthropic
from dotenv import load_dotenv

load_dotenv()

PDF_DIR = Path("data/pdfs")
OUT_DIR = Path("sources/bibliography")
RAW_REFS_PATH = OUT_DIR / "raw_refs.json"
SCORED_REFS_PATH = OUT_DIR / "scored_refs.json"

# Markers that introduce a formal reference/bibliography section.
# Matched as standalone lines (stripped), case-insensitive.
REF_MARKERS = [
    "references", "bibliography", "works cited",
    "notes", "endnotes", "footnotes", "sources", "further reading",
]

# ---------------------------------------------------------------------------
# PDF helpers
# ---------------------------------------------------------------------------

def extract_full_text(path: Path) -> str:
    with pdfplumber.open(path) as doc:
        return "\n".join(page.extract_text() or "" for page in doc.pages)


def find_ref_section(text: str) -> tuple[str, str] | None:
    """
    Return (marker_label, section_text) for the last standalone ref-section
    header in the second half of the document, or None.
    """
    cutoff = len(text) * 0.35
    lines = text.split("\n")
    best_line_idx = -1
    best_marker = ""
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.lower() in REF_MARKERS:
            char_pos = sum(len(l) + 1 for l in lines[:i])
            if char_pos >= cutoff and i > best_line_idx:
                best_line_idx = i
                best_marker = stripped
    if best_line_idx < 0:
        return None
    section = "\n".join(lines[best_line_idx:])
    return best_marker, section


# ---------------------------------------------------------------------------
# Haiku extraction prompts
# ---------------------------------------------------------------------------

EXTRACT_SYSTEM = """You are a bibliographic extraction assistant.
Given raw text from a PDF reference section (possibly garbled by two-column layout),
extract every cited work you can identify.

Return a JSON array. Each element has these fields (use null when unknown):
- "title": string — full title of the work
- "authors": array of strings — author names as written
- "year": integer or null
- "venue": string or null — journal, conference, book, website, etc.
- "doi": string or null
- "url": string or null

Rules:
- Include ALL entries you can identify, even partial ones.
- If two-column layout has interleaved text, do your best to reconstruct each entry.
- Do not include section headers or captions as references.
- Return ONLY the JSON array, no other text."""

INLINE_EXTRACT_SYSTEM = """You are a bibliographic extraction assistant.
Given the full text of an academic or research paper, extract every work cited,
referenced, or mentioned as a source — including inline citations, footnotes,
parenthetical references, and any works cited by title.

Return a JSON array. Each element has these fields (use null when unknown):
- "title": string — full title of the work
- "authors": array of strings — author names as written (empty array if not stated)
- "year": integer or null
- "venue": string or null — journal, conference, book, website, etc.
- "doi": string or null
- "url": string or null
- "context": string — one sentence describing how/why this work is cited

Rules:
- Include ALL cited works, even those only mentioned by title.
- Do not invent titles. If you can only identify an author, skip it.
- Return ONLY the JSON array, no other text."""

SCORE_SYSTEM = """You are a relevance scoring assistant for the Protocol Institute research corpus.
Protocol studies covers: formal/informal rule systems, coordination protocols,
standards bodies, infrastructure design, governance mechanisms, social norms as
protocols, communication protocols, protocol theory and design, commons governance,
institutional design, protocol fiction, and related fields.

For each cited work, assign a relevance score:
  3 = Core: directly about protocol theory, design, governance, or standards
  2 = Relevant: substantially contributes to protocol thinking (coordination, institutions, infrastructure, norms)
  1 = Adjacent: useful background but not centrally about protocols (general social theory, history of technology, etc.)
  0 = Incidental: cited for a specific fact unrelated to protocol studies (medical statistics, regional geography, etc.)

Return a JSON array of objects: {"id": <same id as input>, "score": <0-3>, "rationale": "<one sentence>"}
Return ONLY the JSON array, no other text."""


def extract_refs_haiku(client: anthropic.Anthropic, text: str, mode: str) -> list[dict]:
    system = EXTRACT_SYSTEM if mode == "explicit" else INLINE_EXTRACT_SYSTEM
    # Limit context: explicit sections are short; inline uses first+last of full text
    if mode == "explicit":
        content = text[:8000]
    else:
        # Send beginning + end to catch intro citations and footnotes
        half = 3000
        content = text[:half] + "\n...\n" + text[-half:] if len(text) > half * 2 else text
        content = content[:8000]

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": content}],
    )
    raw = resp.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw)


def score_refs_haiku(client: anthropic.Anthropic, refs: list[dict]) -> list[dict]:
    """Score a batch of references for protocol relevance."""
    batch_input = [{"id": r["_id"], "title": r["title"], "authors": r.get("authors", []),
                    "year": r.get("year"), "venue": r.get("venue")} for r in refs]
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        system=SCORE_SYSTEM,
        messages=[{"role": "user", "content": json.dumps(batch_input)}],
    )
    raw = resp.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def normalize_title(title: str) -> str:
    t = title.lower()
    t = re.sub(r"[^a-z0-9 ]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def make_ref_id(title: str) -> str:
    import hashlib
    return hashlib.sha256(normalize_title(title).encode()).hexdigest()[:16]


def merge_into_registry(registry: dict, new_refs: list[dict], source_pdf: str) -> int:
    """Merge new_refs into registry dict (keyed by ref_id). Returns count added."""
    added = 0
    for ref in new_refs:
        title = (ref.get("title") or "").strip()
        if not title or len(title) < 5:
            continue
        rid = make_ref_id(title)
        if rid not in registry:
            registry[rid] = {
                "_id": rid,
                "title": title,
                "authors": ref.get("authors") or [],
                "year": ref.get("year"),
                "venue": ref.get("venue"),
                "doi": ref.get("doi"),
                "url": ref.get("url"),
                "context": ref.get("context"),
                "cited_by": [],
                "citation_count": 0,
                "extraction_mode": [],
            }
            added += 1
        # Always track which PDFs cite this work
        if source_pdf not in registry[rid]["cited_by"]:
            registry[rid]["cited_by"].append(source_pdf)
            registry[rid]["citation_count"] = len(registry[rid]["cited_by"])
        # Fill in missing fields from later extractions
        for field in ("authors", "year", "venue", "doi", "url"):
            if not registry[rid].get(field) and ref.get(field):
                registry[rid][field] = ref[field]
        mode = "explicit" if source_pdf else "inline"
        if mode not in registry[rid].get("extraction_mode", []):
            registry[rid].setdefault("extraction_mode", []).append(mode)
    return added


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["explicit", "inline"], default="explicit")
    parser.add_argument("--score-only", action="store_true", help="Skip extraction, only run scoring pass")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--pdf", help="Process a single PDF by basename")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Load existing registries
    raw_registry: dict = {}
    if RAW_REFS_PATH.exists():
        raw_registry = json.loads(RAW_REFS_PATH.read_text())

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    # -----------------------------------------------------------------------
    # Extraction pass
    # -----------------------------------------------------------------------
    if not args.score_only:
        pdfs = sorted(PDF_DIR.glob("*.pdf"))
        if args.pdf:
            pdfs = [p for p in pdfs if p.name == args.pdf]

        eligible = []
        for path in pdfs:
            try:
                text = extract_full_text(path)
            except Exception as e:
                print(f"  SKIP {path.name}: {e}")
                continue
            if len(text.strip()) < 100:
                continue

            if args.mode == "explicit":
                result = find_ref_section(text)
                if result:
                    eligible.append((path, result[0], result[1]))
            else:
                eligible.append((path, "full-text", text))

        print(f"Mode={args.mode}: {len(eligible)} PDFs to process\n")

        for path, marker, section_text in eligible:
            print(f"  {path.name} [{marker}]...")
            if args.dry_run:
                print(f"    (dry run — skipping Haiku call)")
                continue
            try:
                refs = extract_refs_haiku(client, section_text, args.mode)
                added = merge_into_registry(raw_registry, refs, path.name)
                print(f"    Extracted {len(refs)} refs, {added} new to registry (total {len(raw_registry)})")
                RAW_REFS_PATH.write_text(json.dumps(raw_registry, indent=2, ensure_ascii=False))
            except Exception as e:
                print(f"    ERROR: {e}")
            time.sleep(0.5)

        if not args.dry_run:
            RAW_REFS_PATH.write_text(json.dumps(raw_registry, indent=2, ensure_ascii=False))
            print(f"\nRaw refs saved: {len(raw_registry)} unique references")

    # -----------------------------------------------------------------------
    # Scoring pass — score all refs not yet scored
    # -----------------------------------------------------------------------
    if args.dry_run:
        print("(dry run — skipping scoring)")
        return

    # Load scored registry
    scored: dict = {}
    if SCORED_REFS_PATH.exists():
        scored = json.loads(SCORED_REFS_PATH.read_text())

    unscored = [r for r in raw_registry.values() if r["_id"] not in scored]
    print(f"\nScoring {len(unscored)} unscored references (keeping ALL, scoring 0-3)...")

    SCORE_BATCH = 20
    for i in range(0, len(unscored), SCORE_BATCH):
        batch = unscored[i:i + SCORE_BATCH]
        print(f"  Scoring batch {i//SCORE_BATCH + 1}/{(len(unscored)+SCORE_BATCH-1)//SCORE_BATCH} ({len(batch)} refs)...")
        try:
            scores = score_refs_haiku(client, batch)
            score_map = {s["id"]: s for s in scores}
            for ref in batch:
                rid = ref["_id"]
                score_info = score_map.get(rid, {"score": 1, "rationale": "unscored"})
                scored[rid] = {
                    **raw_registry[rid],
                    "relevance_score": score_info.get("score", 1),
                    "relevance_rationale": score_info.get("rationale", ""),
                }
            SCORED_REFS_PATH.write_text(json.dumps(scored, indent=2, ensure_ascii=False))
        except Exception as e:
            print(f"    ERROR in scoring: {e}")
        time.sleep(0.5)

    SCORED_REFS_PATH.write_text(json.dumps(scored, indent=2, ensure_ascii=False))

    # Summary
    from collections import Counter
    score_counts = Counter(r["relevance_score"] for r in scored.values())
    print(f"\nScored {len(scored)} total references:")
    for s in sorted(score_counts.keys(), reverse=True):
        label = {3: "core", 2: "relevant", 1: "adjacent", 0: "incidental"}.get(s, str(s))
        print(f"  Score {s} ({label}): {score_counts[s]}")
    print(f"\nSaved to {SCORED_REFS_PATH}")


if __name__ == "__main__":
    main()
