"""
Discord attachment capture utilities.

Downloads attachments from Discord messages to data/attachments/{channel_id}/{msg_id}/
before the 24-hour CDN expiry. Extracts text from PDFs for embedding.

Storage is local only for now; gitignored. Future: move to R2 when volume warrants.
"""

import urllib.request
from pathlib import Path

ATTACHMENTS_DIR = Path(__file__).parent.parent / "data" / "attachments"

# Content types we'll attempt to extract text from
_PDF_TYPES  = {"application/pdf"}
_TEXT_TYPES = {"text/plain", "text/markdown", "text/csv", "application/json"}
_PDF_EXTS   = {".pdf"}
_TEXT_EXTS  = {".txt", ".md", ".markdown", ".csv"}


def _is_pdf(filename: str, content_type: str) -> bool:
    return content_type in _PDF_TYPES or Path(filename).suffix.lower() in _PDF_EXTS


def _is_text(filename: str, content_type: str) -> bool:
    base_ct = content_type.split(";")[0].strip()
    return base_ct in _TEXT_TYPES or Path(filename).suffix.lower() in _TEXT_EXTS


def download_attachment(url: str, dest: Path) -> bool:
    """Download a Discord CDN URL to dest. Idempotent — skips if already exists."""
    if dest.exists():
        return True
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "C3PO-Ingest/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(r.read())
        return True
    except Exception:
        return False


def extract_pdf_text(path: Path) -> str | None:
    """Extract plain text from a PDF using pdfplumber. Returns None on failure."""
    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            pages = [p.extract_text() or "" for p in pdf.pages]
        text = "\n\n".join(p for p in pages if p.strip())
        return text.strip() or None
    except Exception:
        return None


def process_attachments(msg: dict, channel_id: str) -> list[dict]:
    """
    Download all attachments for a message. Returns list of info dicts for saved files.

    Each info dict has: filename, content_type, path (str), size, is_pdf, is_text.
    Skips zero-size files and Discord-internal URLs that have already expired.
    """
    msg_id = msg.get("id", "unknown")
    results = []
    for att in msg.get("attachments", []):
        url  = att.get("url", "")
        name = att.get("filename") or att.get("id", "unknown")
        ct   = (att.get("content_type") or "").split(";")[0].strip()
        if not url:
            continue
        dest = ATTACHMENTS_DIR / channel_id / msg_id / name
        if download_attachment(url, dest) and dest.exists() and dest.stat().st_size > 0:
            results.append({
                "filename":     name,
                "content_type": ct,
                "path":         str(dest),
                "size":         dest.stat().st_size,
                "is_pdf":       _is_pdf(name, ct),
                "is_text":      _is_text(name, ct),
            })
    return results


def attachment_meta_fields(att_infos: list[dict]) -> dict:
    """Return the metadata fields to merge into a Pinecone message record."""
    if not att_infos:
        return {}
    return {
        "has_attachment":      True,
        "attachment_count":    len(att_infos),
        "attachment_filenames": ",".join(a["filename"] for a in att_infos),
        "attachment_types":    ",".join(
            sorted({("pdf" if a["is_pdf"] else "text" if a["is_text"] else "other")
                    for a in att_infos})
        ),
    }
