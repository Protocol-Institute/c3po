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

# Stored text is what the model reads; the embedding uses the full chunk. The
# shared chunker targets 512 tokens (~2.3K chars), so the old 1,600-char cap was
# cutting roughly the last third off nearly every slide chunk. See the same
# constant in sync_symposium.py.
MAX_META_TEXT = 6000
ROOT        = Path(__file__).parent.parent
STATE_PATH  = ROOT / "data" / "symposium_decks_state.json"
REVIEW_PATH = ROOT / "data" / "symposium_decks_review.json"
MAP_PATH    = ROOT / "config" / "symposium_deck_map.json"

FOLDER_MIME = "application/vnd.google-apps.folder"
GSLIDES     = "application/vnd.google-apps.presentation"
GSLIDES_ALT = "application/vnd.google-apps.punch"     # legacy mime, still served
GDOC        = "application/vnd.google-apps.document"
SHORTCUT    = "application/vnd.google-apps.shortcut"
UA          = {"User-Agent": "Mozilla/5.0 (compatible; c3po-ingest/1.0)"}

# Some entries in the folder are pointers rather than documents: a Drive
# shortcut, or a one-slide deck whose only content is a link to where the real
# slides live. Both are followed exactly one hop (VGR, session 55) — never
# further, so a linked page that links onward cannot walk us into the open web.
POINTER_THIN_CHARS = 400        # under this, a URL in the text IS the document
POINTER_MAX_CHARS  = 60_000     # cap on what one hop may bring back

# A zip is a bundle, and bundles repeat themselves: the AI Kitcraft archive held
# the same deck as .pptx twice plus an HTML export plus the notes as Markdown.
ZIP_MEMBER_LIMIT = 12
ZIP_TEXT_LIMIT   = 200_000

# Two files under one talk are usually the same material twice (a deck and its
# own export). Above this token overlap the richer one is kept and the rest are
# dropped, because duplicate chunks compete for the same retrieval slots. Below
# it they are treated as genuinely different documents — a deck and a companion
# paper, say — and both are ingested.
DUP_OVERLAP = 0.60

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


def resolve_shortcuts(files: list[dict]) -> list[dict]:
    """Replace each Drive shortcut with the file it points at — one hop.

    A shortcut whose target is already in the folder is dropped rather than
    followed: it is the same document reached twice, and ingesting it again
    would put duplicate chunks in the index.
    """
    known = {f["id"] for f in files}
    out = []
    for f in files:
        if f["mime"] != SHORTCUT:
            out.append(f)
            continue
        target = shortcut_target(f["id"])
        if not target:
            out.append(f)        # unresolvable: let it fail loudly downstream
            continue
        tid, tmime = target
        if tid in known:
            print(f"  LINK  {f['name'][:50]:<52} shortcut to a file already in "
                  f"this folder — not ingested twice")
            continue
        print(f"  LINK  {f['name'][:50]:<52} shortcut -> {tid}")
        out.append({**f, "id": tid, "mime": tmime or "", "via_shortcut": f["id"]})
    return out


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


def extract_zip(blob: bytes, name: str) -> str:
    """Extract every supported member, keeping one copy of repeated material.

    The members are renderings of each other as often as not, so the richest of
    any near-duplicate set wins and the others are dropped — the same rule
    applied across files that resolve to one talk.
    """
    import zipfile
    z = zipfile.ZipFile(io.BytesIO(blob))
    members = [n for n in z.namelist()
               if not n.endswith("/") and not n.startswith("__MACOSX")][:ZIP_MEMBER_LIMIT]

    got: list[tuple[str, str]] = []
    for member in members:
        try:
            text = extract_bytes(member, z.read(member))
        except Exception:                                        # noqa: BLE001
            continue                      # an unsupported member is not a failure
        if len(text.strip()) >= 200:
            got.append((member, text))

    kept: list[tuple[str, str]] = []
    for member, text in sorted(got, key=lambda mt: -len(mt[1])):
        twin = next((k for k in kept if token_overlap(k[1], text) >= DUP_OVERLAP), None)
        if twin:
            print(f"    zip: {member[:44]:<46} skipped — same material as {twin[0][:34]}")
            continue
        kept.append((member, text))

    if not kept:
        raise RuntimeError(f"zip {name} holds no extractable document")
    out = "\n\n".join(f"[{member}]\n{text}" for member, text in kept)
    return out[:ZIP_TEXT_LIMIT]


def extract_bytes(name: str, data: bytes) -> str:
    """Extraction dispatched on filename alone — for zip members, which have no mime."""
    low = name.lower()
    if low.endswith(".pptx"):
        return clean_text(extract_pptx(data))
    if low.endswith(".docx"):
        return clean_text(extract_docx(data))
    if low.endswith(".pdf"):
        return clean_text(extract_pdf(data))
    if low.endswith((".html", ".htm")):
        return clean_text(extract_html(data))
    if low.endswith((".md", ".txt", ".markdown", ".csv")):
        return clean_text(data.decode("utf-8", "replace"))
    raise RuntimeError(f"no extractor for {name}")


def token_overlap(a: str, b: str) -> float:
    """Jaccard over 4+ letter words — enough to tell a re-export from a new document."""
    ta = set(re.findall(r"[a-z]{4,}", a.lower()))
    tb = set(re.findall(r"[a-z]{4,}", b.lower()))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# ── One-hop pointers ──────────────────────────────────────────────────────────

DRIVE_LINK_RE = re.compile(
    r"https?://(?:docs|drive)\.google\.com/(document|presentation|spreadsheets|file)/d/([A-Za-z0-9_-]{20,})")
ANY_LINK_RE = re.compile(r"https?://[^\s<>\"')\]]+")


def shortcut_target(fid: str) -> tuple[str, str] | None:
    """(target id, mime) behind a Drive shortcut, read from where /view lands."""
    try:
        resp = urllib.request.urlopen(
            urllib.request.Request(f"https://drive.google.com/file/d/{fid}/view", headers=UA),
            timeout=60)
        html = resp.read().decode("utf-8", "replace")
    except Exception:                                            # noqa: BLE001
        return None
    m = re.search(r"/(?:document|presentation|spreadsheets|file)/d/([A-Za-z0-9_-]{20,})", resp.geturl())
    if not m or m.group(1) == fid:
        return None
    mime = (re.search(r'"(application/vnd\.google-apps\.[a-z]+)"', html) or [None, ""])[1]
    if mime == GSLIDES_ALT:
        mime = GSLIDES
    return m.group(1), mime


def fetch_linked_document(url: str) -> str:
    """Retrieve one linked document — a Drive file, or an ordinary web page."""
    m = DRIVE_LINK_RE.match(url)
    if m:
        kind, fid = m.groups()
        mime = {"document": GDOC, "presentation": GSLIDES}.get(kind, "")
        return extract({"id": fid, "name": url, "mime": mime}, download({"id": fid, "mime": mime}))
    blob = _get(url, timeout=60)
    return clean_text(extract_html(blob))


def follow_pointer(text: str, name: str) -> tuple[str, str] | None:
    """A thin document whose payload is a link: fetch what it points at, once.

    Returns (text, url) or None. Only thin documents are followed: in a real
    deck a URL is a citation, and chasing citations would pull the open web into
    the symposium namespace.
    """
    if len(text) >= POINTER_THIN_CHARS:
        return None
    url = (DRIVE_LINK_RE.search(text) or ANY_LINK_RE.search(text))
    if not url:
        return None
    url = url.group(0).rstrip(".,);")
    try:
        linked = fetch_linked_document(url)
    except Exception as e:                                       # noqa: BLE001
        print(f"  POINT {name[:50]:<52} pointer {url[:40]} unreachable: {str(e)[:40]}")
        return None
    if len(linked.strip()) < 200:
        return None
    print(f"  POINT {name[:50]:<52} followed -> {url[:44]} ({len(linked)} chars)")
    return (f"{text}\n\n[Retrieved from {url}]\n{linked}"[:POINTER_MAX_CHARS], url)


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
    if mime in ("application/zip", "application/x-zip-compressed") or name.endswith(".zip"):
        return extract_zip(blob, f["name"])
    if mime.startswith("text/") or name.endswith((".md", ".txt", ".markdown")):
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


def collapse_duplicates(todo, duplicates, files, force=False):
    """Split (file, proposal, why) triples into keepers and same-talk duplicates.

    Returns (keep, dropped). Keepers carry a fourth element: text already
    extracted during the comparison, or None when the file was never contested
    and should go through the ordinary download gate. Dropped entries carry the
    id of the file that beat them.

    A settled verdict is remembered in the state file, so a contested group is
    only re-downloaded when one of its files actually changes — otherwise the
    AI Kitcraft trio alone would pull ~30MB from Drive on every cycle to
    re-derive the same answer.
    """
    from collections import defaultdict
    groups = defaultdict(list)
    for f, prop, why in todo:
        groups[prop.get("slug") or prop["title"]].append((f, prop, why))

    keep, dropped = [], []
    for slug, members in groups.items():
        if len(members) == 1:
            f, prop, why = members[0]
            keep.append((f, prop, why, None))
            continue

        settled = [f for f, _, _ in members if "duplicate_of" in files.get(f["id"], {})]
        unchanged = all(files.get(f["id"], {}).get("mtime") == f["mtime"]
                        and files.get(f["id"], {}).get("size") == f["size"]
                        for f, _, _ in members)
        if settled and unchanged and not force:
            for f, prop, why in members:
                rec = files.get(f["id"], {})
                if "duplicate_of" not in rec:
                    keep.append((f, prop, why, None))
                    continue
                # Report it again so the review file keeps describing the whole
                # pile, not just whatever was re-decided this run.
                winner = files.get(rec["duplicate_of"], {}).get("name", rec["duplicate_of"])
                duplicates.append({
                    "id": f["id"], "name": f["name"], "matched_to": prop["title"],
                    "reason": f"near-duplicate of {winner}; the richer file was kept "
                              f"(verdict carried from an earlier run)",
                })
            continue

        extracted = []
        for f, prop, why in members:
            try:
                extracted.append((f, prop, why, extract(f, download(f))))
            except Exception as e:                               # noqa: BLE001
                print(f"  FAIL  {f['name'][:50]:<52} {str(e)[:60]}")
        for cand in sorted(extracted, key=lambda t: -len(t[3])):
            twin = next((k for k in keep if k[3] is not None
                         and (k[1].get("slug") or k[1]["title"]) == slug
                         and token_overlap(k[3], cand[3]) >= DUP_OVERLAP), None)
            if twin:
                overlap = token_overlap(twin[3], cand[3])
                print(f"  DUP   {cand[0]['name'][:50]:<52} {len(cand[3]):>7} chars — "
                      f"{overlap:.2f} overlap with {twin[0]['name'][:28]}, kept that one")
                duplicates.append({
                    "id": cand[0]["id"], "name": cand[0]["name"],
                    "matched_to": cand[1]["title"],
                    "reason": f"near-duplicate of {twin[0]['name']} "
                              f"(token overlap {overlap:.2f}); the richer file was kept",
                })
                dropped.append((cand[0], cand[1], cand[2], twin[0]["id"]))
                continue
            keep.append(cand)
    return keep, dropped


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
    drive = resolve_shortcuts(walk_folder(ROOT_FOLDER))
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
    duplicates: list[dict] = []

    # Where several files land on one talk, they are usually one document in
    # several wrappers. Extract the contested ones up front so the richest can
    # be chosen; uncontested files keep the cheap no-download gate below.
    todo, dup_drop = collapse_duplicates(todo, duplicates, files, force)
    for f, p, why, winner_id in dup_drop:
        # A file demoted to duplicate may have been ingested on an earlier run.
        prev = files.get(f["id"], {})
        for vid in prev.get("vector_ids", []):
            if not report:
                idx.delete(ids=[vid], namespace=NAMESPACE)
        # Remember the verdict so the group is not re-downloaded next cycle.
        files[f["id"]] = {"mtime": f["mtime"], "size": f["size"],
                          "name": f["name"], "duplicate_of": winner_id}
    if dup_drop and not report:
        save_json(STATE_PATH, state)

    for f, p, why, pre in todo:
        prev = files.get(f["id"], {})
        # Cheap gate first: no download unless Drive says the file moved.
        if pre is None and not force and prev.get("mtime") == f["mtime"] and prev.get("size") == f["size"]:
            skipped += 1
            continue
        try:
            text = pre if pre is not None else extract(f, download(f))
        except Exception as e:                                    # noqa: BLE001
            print(f"  FAIL  {f['name'][:50]:<52} {str(e)[:60]}")
            failed += 1
            continue
        # A document whose whole content is a link to the real document.
        hop = follow_pointer(text, f["name"])
        if hop:
            text = hop[0]
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
                "text": c[:MAX_META_TEXT],
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
    if thin or duplicates:
        rev = load_json(REVIEW_PATH, {})
        if thin:
            rev["not_ingestable"] = thin
        # Deliberately dropped, not awaiting a decision — recorded so the pile of
        # near-identical files under one talk is visible rather than mysterious.
        rev["duplicates_dropped"] = duplicates
        save_json(REVIEW_PATH, rev)

    print(f"\n── Summary ──  embedded {upserted}   unchanged {skipped}   "
          f"failed {failed}   duplicates {len(duplicates)}   "
          f"not ingestable {len(thin)}   needs review {len(review)}")


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
