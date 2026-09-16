"""
Ingest Protocol Symposium 2026 slide decks and speaker documents from the public
Drive folder into the Pinecone `symposium` namespace as chunk_type=symposium_slides.

Phase B of plans/symposium-ingest.md. The folder is world-readable, so no
credential is used or needed: the folder page embeds its own inventory, and
public files export over docs.google.com / drive.google.com directly.

Matching decks to talks is the hard part, not extraction — filenames carry
neither slug nor, often, the title ("TMDTS-Speaker-Notes.pdf" is Lohse's "Time
Moves Down the Stack"). Resolution order is: explicit override -> exact slug ->
fuzzy title -> speaker surname. Anything ambiguous or unmatched goes to a review
queue and is NEVER attached on a guess (VGR, session 54): a deck filed against
the wrong talk is worse than an absent one, because the bot answers from it
confidently.

State: data/symposium_decks_state.json, keyed on the Drive FILE ID — ids survive
renames and moves, filenames do not.
Overrides: config/symposium_deck_map.json  {"<drive file id>": "<proposal slug>"}
Review queue: data/symposium_decks_review.json

Usage:
    python3 ingest/sync_symposium_decks.py --dry-run     # match report, no downloads
    python3 ingest/sync_symposium_decks.py --report      # match report + extraction probe
    python3 ingest/sync_symposium_decks.py               # ingest resolved decks
    python3 ingest/sync_symposium_decks.py --prune       # also drop vanished files
"""

import argparse
import codecs
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.request
from difflib import SequenceMatcher
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
from utils import embed_chunks, get_voyage_client, get_pinecone_index, clean_text, chunk_text
from sync_symposium import fetch_json, hosts_of, track_label, EVENT_NAME, BASE_URL, EVENT_PATH

load_dotenv(Path(__file__).parent.parent / ".env")

ROOT_FOLDER = "1lyX7G4sKcZRNmHNN7EULT29SSwxK8J69"
NAMESPACE   = "symposium"
ROOT        = Path(__file__).parent.parent
STATE_PATH  = ROOT / "data" / "symposium_decks_state.json"
REVIEW_PATH = ROOT / "data" / "symposium_decks_review.json"
MAP_PATH    = ROOT / "config" / "symposium_deck_map.json"

FOLDER_MIME = "application/vnd.google-apps.folder"
GSLIDES     = "application/vnd.google-apps.presentation"
GDOC        = "application/vnd.google-apps.document"
UA          = {"User-Agent": "Mozilla/5.0 (compatible; c3po-ingest/1.0)"}

# A filename must clear this to be accepted on title similarity alone.
TITLE_THRESHOLD   = 0.60
# With a speaker-surname hit as corroboration, a weaker title match is enough.
SURNAME_THRESHOLD = 0.35
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


# ── Drive (no credential; the folder is public) ───────────────────────────────

def _get(url: str, timeout: int = 120) -> bytes:
    for attempt in range(1, 4):
        try:
            return urllib.request.urlopen(
                urllib.request.Request(url, headers=UA), timeout=timeout).read()
        except Exception as e:                                   # noqa: BLE001
            if attempt == 3:
                raise RuntimeError(f"GET {url[:80]} failed: {e}") from e
            time.sleep(2 * attempt)
    raise AssertionError("unreachable")


def folder_listing(folder_id: str) -> list[dict]:
    """Parse the inventory the public folder page embeds in window['_DRIVE_ivd']."""
    html = _get(f"https://drive.google.com/drive/folders/{folder_id}", timeout=60).decode("utf-8", "replace")
    m = re.search(r"window\['_DRIVE_ivd'\]\s*=\s*'([^']+)'", html)
    if not m:
        raise RuntimeError(f"no inventory in folder page {folder_id} — is it still public?")
    # The blob is JS-escaped. Drive escapes forward slashes as \/ , which is
    # valid JS but an invalid Python escape — unicode_escape warns on it today and
    # is documented to fail in a future release. Neutralise it before decoding.
    raw = codecs.decode(m.group(1).replace(r"\/", "/"), "unicode_escape")
    # unicode_escape decodes as latin-1, so any UTF-8 in a filename comes back
    # mojibaked ("Archival Time \xe2\x80\x94 Sachin" for an em dash). Round-trip it
    # back to real UTF-8; the filename lands in chunk metadata as source_file.
    try:
        raw = raw.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass  # already clean
    return [
        {"id": e[0], "name": e[2], "mime": e[3], "mtime": e[9], "size": e[13]}
        for e in json.loads(raw)[0]
    ]


def walk_folder(folder_id: str, path: str = "") -> list[dict]:
    files = []
    for e in folder_listing(folder_id):
        if e["mime"] == FOLDER_MIME:
            time.sleep(0.4)
            files += walk_folder(e["id"], f"{path}/{e['name']}")
        else:
            files.append({**e, "path": path})
    return files


def download(f: dict) -> bytes:
    """Native Google formats are exported; uploaded files download as-is.

    Slides export to .pptx rather than .txt deliberately — python-pptx then gets
    the speaker notes too, which are often more substantive than the slide text.
    """
    fid = f["id"]
    if f["mime"] == GSLIDES:
        return _get(f"https://docs.google.com/presentation/d/{fid}/export/pptx")
    if f["mime"] == GDOC:
        return _get(f"https://docs.google.com/document/d/{fid}/export?format=docx")
    return _get(f"https://drive.google.com/uc?export=download&id={fid}")


# ── Extraction ────────────────────────────────────────────────────────────────

def extract_pptx(blob: bytes) -> str:
    from pptx import Presentation
    prs, out = Presentation(io.BytesIO(blob)), []
    for i, slide in enumerate(prs.slides, 1):
        bits = [sh.text_frame.text.strip()
                for sh in slide.shapes
                if getattr(sh, "has_text_frame", False) and sh.text_frame.text.strip()]
        notes = ""
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
            notes = slide.notes_slide.notes_text_frame.text.strip()
        if bits or notes:
            out.append(f"[Slide {i}]\n" + "\n".join(bits)
                       + (f"\nSpeaker notes: {notes}" if notes else ""))
    return "\n\n".join(out)


def extract_docx(blob: bytes) -> str:
    import docx
    d = docx.Document(io.BytesIO(blob))
    return "\n".join(p.text for p in d.paragraphs if p.text.strip())


def extract_pdf(blob: bytes) -> str:
    import pdfplumber
    out = []
    with pdfplumber.open(io.BytesIO(blob)) as pdf:
        for i, page in enumerate(pdf.pages, 1):
            t = (page.extract_text() or "").strip()
            if t:
                out.append(f"[Page {i}]\n{t}")
    return "\n\n".join(out)


# Slides exported as images yield zero text from any PDF text layer. Claude reads
# the PDF directly as a document block — it does vision on the pages — so no
# rasteriser or OCR engine is needed, and the deck's own structure survives.
VISION_MODEL = "claude-sonnet-5"   # VGR's standing choice for dense material
VISION_MAX_BYTES = 28 * 1024 * 1024   # request cap is 32MB; base64 inflates ~4/3

VISION_PROMPT = (
    "This PDF is a slide deck exported as images, so it has no text layer. "
    "Transcribe it slide by slide for a research archive.\n\n"
    "For each slide output '[Slide N]' then the slide's text verbatim — titles, "
    "bullets, labels, captions, quotes. Where a slide is a diagram, chart or "
    "image with little text, add one line beginning 'Visual:' describing what it "
    "depicts and what it is arguing.\n\n"
    "Transcribe only what is on the slides. Do not add commentary, interpretation "
    "or anything not visible. If a slide is blank or purely decorative, say so."
)


def extract_pdf_via_vision(blob: bytes, name: str) -> str:
    """Transcribe an image-only PDF by handing the whole file to Claude."""
    import base64
    import anthropic
    from cost_logger import check_budget, log_api_call

    if len(blob) > VISION_MAX_BYTES:
        raise RuntimeError(f"{len(blob)/1e6:.1f}MB exceeds the vision request cap")

    check_budget()
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    resp = client.messages.create(
        model=VISION_MODEL,
        max_tokens=16000,
        messages=[{"role": "user", "content": [
            {"type": "document", "source": {
                "type": "base64", "media_type": "application/pdf",
                "data": base64.standard_b64encode(blob).decode("ascii")}},
            {"type": "text", "text": VISION_PROMPT},
        ]}],
    )
    cost = log_api_call("sync_symposium_decks", VISION_MODEL, resp.usage)
    text = "\n".join(b.text for b in resp.content if b.type == "text")
    print(f"  VISION {name[:44]:<46} {len(text):>6} chars transcribed  (${cost:.4f})")
    return clean_text(text)


def extract_html(blob: bytes) -> str:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(blob.decode("utf-8", "replace"), "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text("\n")


def extract(f: dict, blob: bytes) -> str:
    mime, name = f["mime"], f["name"].lower()
    if mime == GSLIDES or name.endswith(".pptx") or "presentationml" in mime:
        return clean_text(extract_pptx(blob))
    if mime == GDOC or name.endswith(".docx") or "wordprocessingml" in mime:
        return clean_text(extract_docx(blob))
    if mime == "application/pdf" or name.endswith(".pdf"):
        text = clean_text(extract_pdf(blob))
        if len(text) < 200:
            return extract_pdf_via_vision(blob, f["name"])
        return text
    if mime == "text/html" or name.endswith((".html", ".htm")):
        return clean_text(extract_html(blob))
    if mime.startswith("text/"):
        return clean_text(blob.decode("utf-8", "replace"))
    raise RuntimeError(f"no extractor for {mime}")


# ── Matching ──────────────────────────────────────────────────────────────────

def norm(s: str) -> str:
    s = re.sub(r"\.(pptx|pdf|docx|html?|txt|key)$", "", s.strip(), flags=re.I)
    s = re.sub(r"[_\-]+", " ", s)
    s = re.sub(r"\b(v\d+[\w.]*|final|draft|slides?|deck|talk|presentation|speaker notes?)\b", " ", s, flags=re.I)
    s = re.sub(r"[^a-z0-9 ]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def surnames(p: dict) -> list[str]:
    out = []
    for person in hosts_of(p):
        for part in re.split(r"[\s,]+", person):
            if len(part) > 2:
                out.append(part.lower())
    return out


def candidates(fname: str, proposals: list[dict]) -> list[tuple[float, dict, str]]:
    """Return (score, proposal, why) for every plausible match, best first."""
    n, hits = norm(fname), []
    for p in proposals:
        title_score = SequenceMatcher(None, n, norm(p["title"])).ratio()
        # A distinctive title fragment appearing verbatim is stronger than the
        # whole-string ratio, which short filenames always lose on.
        frag = max((SequenceMatcher(None, n, norm(w)).ratio()
                    for w in [p["title"][:40]] if w), default=0)
        surname = next((s for s in surnames(p) if re.search(rf"\b{re.escape(s)}\b", n)), None)
        score, why = max(title_score, frag), "title"
        if surname:
            score, why = max(score, SURNAME_THRESHOLD + 0.01), f"surname:{surname}"
            if title_score >= SURNAME_THRESHOLD:
                score, why = max(score, (title_score + 1.0) / 2), f"title+surname:{surname}"
        thresh = SURNAME_THRESHOLD if surname else TITLE_THRESHOLD
        if score >= thresh:
            hits.append((round(score, 3), p, why))
    return sorted(hits, key=lambda x: -x[0])


def resolve(f: dict, proposals: list[dict], overrides: dict):
    """-> (proposal, why) or (None, reason). Never guesses between near-equals."""
    if f["id"] in overrides:
        want = overrides[f["id"]]
        slugs = want if isinstance(want, list) else [want]
        found = [p for s in slugs for p in proposals if p.get("slug") == s]
        if len(found) != len(slugs):
            missing = [s for s in slugs if not any(p.get("slug") == s for p in proposals)]
            return None, f"override points at unknown slug(s) {missing}"
        # A deck covering two talks is attached once, naming both, rather than
        # embedded twice — duplicate chunks would crowd retrieval for no gain.
        if len(found) > 1:
            merged = dict(found[0])
            merged["title"] = " / ".join(p["title"] for p in found)
            merged["_extra_slugs"] = [p.get("slug") for p in found[1:]]
            merged["_covers"] = [p["title"] for p in found]
            return merged, f"override -> {len(found)} talks"
        return found[0], "override"

    by_slug = {p.get("slug"): p for p in proposals if p.get("slug")}
    stem = norm(f["name"]).replace(" ", "-")
    if stem in by_slug:
        return by_slug[stem], "exact-slug"

    # A single file can legitimately cover two talks — the combined
    # "Creating New Genres ... & Making Monsters ..." deck names both in full.
    # The long filename dilutes the similarity ratio so only one clears the
    # threshold, and silently taking that one loses the other talk's material.
    n = norm(f["name"])
    named = [p for p in proposals if len(norm(p["title"])) > 12 and norm(p["title"]) in n]
    if len(named) > 1:
        return None, "covers multiple talks: " + "; ".join(p["title"][:38] for p in named)

    hits = candidates(f["name"], proposals)
    if not hits:
        return None, "no candidate above threshold"
    if len(hits) > 1 and hits[0][0] - hits[1][0] < 0.08:
        others = "; ".join(f"{h[1]['title'][:40]} ({h[0]})" for h in hits[:3])
        return None, f"ambiguous between {len(hits)}: {others}"
    return hits[0][1], f"{hits[0][2]} @ {hits[0][0]}"


# ── State ─────────────────────────────────────────────────────────────────────

def load_json(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def vector_ids(fid: str, n: int) -> list[str]:
    return [f"symposium__deck-{fid}__{i}" for i in range(n)]


# ── Main ──────────────────────────────────────────────────────────────────────

# VGR, session 54: "no need to be super on time in catching up with edits."
# So the daemon may call this every 30-minute cycle and it self-throttles. The
# alternative — running the folder scrape 48x a day for a folder that changes a
# few times a week — is pointless traffic against Google and a good way to look
# like a scraper.
MIN_INTERVAL_HOURS = 6


def due(state, min_hours: float) -> bool:
    last = state.get("last_sync")
    if not last:
        return True
    try:
        prev = datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - prev).total_seconds() >= min_hours * 3600


def run(dry_run=False, report=False, force=False, prune=False, limit=None,
        min_interval_hours: float = 0.0):
    if min_interval_hours and not due(load_json(STATE_PATH, {}), min_interval_hours):
        print(f"Last sync under {min_interval_hours}h ago — skipping "
              f"(slide decks do not need minute-level freshness).")
        return
    proposals = fetch_json("/api/symposium/proposals").get("proposals", [])
    overrides = load_json(MAP_PATH, {})
    state     = load_json(STATE_PATH, {"files": {}})
    files     = state["files"]

    print(f"Listing Drive folder {ROOT_FOLDER} …")
    drive = walk_folder(ROOT_FOLDER)
    print(f"  {len(drive)} files across the folder tree\n")

    resolved, review = [], []
    for f in drive:
        p, why = resolve(f, proposals, overrides)
        (resolved if p else review).append((f, p, why))

    print(f"── Matching ──  resolved {len(resolved)}   needs review {len(review)}\n")
    for f, p, why in resolved:
        print(f"  OK    {f['name'][:52]:<54} -> {p['title'][:44]:<46} [{why}]")
    if review:
        print()
        for f, _, why in review:
            print(f"  ??    {f['name'][:52]:<54} -> {why[:80]}")

    save_json(REVIEW_PATH, {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "unresolved": [{"id": f["id"], "name": f["name"], "path": f.get("path", ""),
                        "reason": why} for f, _, why in review],
        "how_to_fix": f"Add \"<drive file id>\": \"<proposal slug>\" to {MAP_PATH.relative_to(ROOT)}",
    })
    if review:
        print(f"\n  Review queue written to {REVIEW_PATH.relative_to(ROOT)} "
              f"({len(review)} file(s) — not attached to any talk).")

    if dry_run:
        print("\n(dry run — nothing downloaded or written)")
        return

    todo = resolved[:limit] if limit else resolved
    vc = idx = None
    if not report:
        vc, idx = get_voyage_client(), get_pinecone_index()

    print(f"\n── Extracting {len(todo)} deck(s) ──")
    upserted = skipped = failed = 0
    thin: list[dict] = []
    for f, p, why in todo:
        prev = files.get(f["id"], {})
        # Cheap gate first: no download unless Drive says the file moved.
        if not force and prev.get("mtime") == f["mtime"] and prev.get("size") == f["size"]:
            skipped += 1
            continue
        try:
            blob = download(f)
            text = extract(f, blob)
        except Exception as e:                                    # noqa: BLE001
            print(f"  FAIL  {f['name'][:50]:<52} {str(e)[:60]}")
            failed += 1
            continue
        if len(text) < 200:
            # Two distinct cases, both "not ingestable", worth telling apart:
            # a stub deck the speaker has not filled in yet, and a PDF that is
            # images all the way down and would need OCR.
            kind = "no extractable text (image-only PDF? needs OCR)" if not text.strip() \
                   else f"stub — only {len(text)} chars, speaker has not filled it in"
            print(f"  THIN  {f['name'][:50]:<52} {kind}")
            thin.append({"id": f["id"], "name": f["name"], "reason": kind,
                         "matched_to": p["title"]})
            continue

        thash = hashlib.sha256(text.encode()).hexdigest()[:16]
        if not force and prev.get("text_hash") == thash:
            # Drive timestamp moved but the content did not (autosave, a nudged
            # text box). Record the new timestamp; do not pay to re-embed.
            files[f["id"]] = {**prev, "mtime": f["mtime"], "size": f["size"]}
            save_json(STATE_PATH, state)
            skipped += 1
            continue

        covers = p.get("_covers")
        subject = (" and ".join(f'"{t}"' for t in covers) if covers
                   else f"\"{p['title']}\"")
        header = (f"{EVENT_NAME} - presentation material for {subject}\n"
                  f"Speakers: {', '.join(hosts_of(p)) or 'Protocol Institute'}\n"
                  f"Track: {track_label(p.get('track'))}\n"
                  f"Source file: {f['name']}\n")
        chunks = [f"{header}\n{c}" for c in chunk_text(text)]
        if EMAIL_RE.search(" ".join(chunks)):
            # Decks are authored by speakers and can carry contact slides.
            chunks = [EMAIL_RE.sub("[email removed]", c) for c in chunks]
            print(f"  NOTE  {f['name'][:50]:<52} email(s) redacted from deck text")

        print(f"  {'(probe)' if report else 'EMBED  '} {f['name'][:48]:<50} "
              f"{len(text):>7} chars -> {len(chunks)} chunk(s)")
        if report:
            continue

        for vid in prev.get("vector_ids", []):
            idx.delete(ids=[vid], namespace=NAMESPACE)
        vecs, ids = embed_chunks(chunks, vc), vector_ids(f["id"], len(chunks))
        idx.upsert(vectors=[{
            "id": vid,
            "values": vec,
            "metadata": {
                "chunk_type": "symposium_slides", "event": EVENT_NAME,
                "title": p["title"], "slug": p.get("slug") or "",
                **({"also_slugs": ", ".join(p["_extra_slugs"])} if p.get("_extra_slugs") else {}),
                "track": (track_label(p.get("track")) or "").split(" (")[0],
                "speakers": ", ".join(hosts_of(p)),
                "scheduled_date": p.get("scheduled_date") or "",
                "source_file": f["name"], "drive_id": f["id"],
                "chunk_index": i, "chunk_total": len(chunks),
                "url": f"{BASE_URL}{EVENT_PATH}/talks#p-{p['id']}",
                "text": c[:1600],
            }} for i, (vid, vec, c) in enumerate(zip(ids, vecs, chunks))],
            namespace=NAMESPACE)
        files[f["id"]] = {"mtime": f["mtime"], "size": f["size"], "text_hash": thash,
                          "vector_ids": ids, "slug": p.get("slug"), "name": f["name"]}
        save_json(STATE_PATH, state)
        upserted += 1

    seen = {f["id"] for f in drive}
    gone = [k for k in files if k not in seen]
    if gone and prune and not report:
        for k in gone:
            ids = files[k].get("vector_ids", [])
            if ids:
                idx.delete(ids=ids, namespace=NAMESPACE)
            print(f"  PRUNED {files[k].get('name', k)[:60]}")
            del files[k]
        save_json(STATE_PATH, state)
    elif gone:
        print(f"\n  {len(gone)} file(s) no longer in the folder; --prune removes their chunks.")

    state["last_sync"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    save_json(STATE_PATH, state)
    if thin:
        rev = load_json(REVIEW_PATH, {})
        rev["not_ingestable"] = thin
        save_json(REVIEW_PATH, rev)

    print(f"\n── Summary ──  embedded {upserted}   unchanged {skipped}   "
          f"failed {failed}   not ingestable {len(thin)}   needs review {len(review)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Ingest symposium slide decks from the public Drive folder")
    ap.add_argument("--dry-run", action="store_true", help="Match report only; no downloads")
    ap.add_argument("--report",  action="store_true", help="Download and extract, but do not embed")
    ap.add_argument("--force",   action="store_true", help="Re-extract and re-embed everything")
    ap.add_argument("--prune",   action="store_true", help="Delete chunks for files gone from the folder")
    ap.add_argument("--limit",   type=int, help="Process at most N decks (testing)")
    ap.add_argument("--daemon",  action="store_true",
                    help=f"Self-throttle: no-op unless {MIN_INTERVAL_HOURS}h since the last sync")
    a = ap.parse_args()
    run(dry_run=a.dry_run, report=a.report, force=a.force, prune=a.prune, limit=a.limit,
        min_interval_hours=MIN_INTERVAL_HOURS if a.daemon else 0.0)
