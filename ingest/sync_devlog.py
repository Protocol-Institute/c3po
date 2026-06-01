"""
Ingest the C3PO development log into the Pinecone `meta` namespace.

One vector per session entry. Idempotent — skips sessions whose content hash
hasn't changed since the last run. Anchor URLs point to the public devlog page
at protocolized.io/resources/c3po-devlog#session-{id}.

State file: data/devlog_state.json  ({session_id} → content_hash)
Pinecone namespace: meta

Usage:
    python3 ingest/sync_devlog.py
    python3 ingest/sync_devlog.py --dry-run
    python3 ingest/sync_devlog.py --force
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
from utils import embed_chunks, get_voyage_client, get_pinecone_index, clean_text

load_dotenv(Path(__file__).parent.parent / ".env")

DEVLOG_PATH  = Path(__file__).parent.parent / "data" / "devlog.json"
STATE_PATH   = Path(__file__).parent.parent / "data" / "devlog_state.json"
NAMESPACE    = "meta"
PUBLIC_URL   = "https://protocolized.io/resources/c3po-devlog"


# ── HTML → plain text ─────────────────────────────────────────────────────────

def strip_html(html: str) -> str:
    text = re.sub(r'<strong>(.*?)</strong>', r'\1', html, flags=re.DOTALL)
    text = re.sub(r'<em>(.*?)</em>', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'<code>(.*?)</code>', r'`\1`', text, flags=re.DOTALL)
    text = re.sub(r'<a[^>]*>(.*?)</a>', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r' {2,}', ' ', text)
    return text.strip()


# ── Session → embed text + metadata ──────────────────────────────────────────

def session_text(s: dict) -> str:
    """Plain text representation of one session for embedding."""
    parts = []

    label = s.get("label", "")
    title = s.get("title", "")
    heading = f"{label}: {title}" if label else title
    parts.append(f"C3PO Development Log — {heading}")

    date = s.get("date", "")
    if date:
        parts.append(f"Date: {date}")

    tracks = s.get("tracks", [])
    if tracks:
        parts.append(f"Tracks: {', '.join(tracks)}")

    costs = s.get("costs_usd", {})
    if costs:
        cost_str = ", ".join(f"{k}: ${v:.2f}" for k, v in costs.items())
        parts.append(f"Session costs: {cost_str}")

    vec_counts = s.get("vector_counts", {})
    if vec_counts:
        vc_str = ", ".join(f"{ns}: {n:,}" for ns, n in vec_counts.items())
        parts.append(f"Vectors upserted: {vc_str}")

    parts.append("")
    for item in s.get("items", []):
        html = item.get("html", "")
        text = strip_html(html)
        if text:
            parts.append(f"- {text}")

    return clean_text("\n".join(parts))


def session_meta(s: dict, text: str) -> dict:
    sid    = s.get("id", s.get("sort_key", 0))
    label  = s.get("label", "")
    title  = s.get("title", "")
    tracks = s.get("tracks", [])
    return {
        "source":        "devlog",
        "chunk_type":    "devlog_session",
        "session_id":    int(sid) if isinstance(sid, (int, float)) else sid,
        "session_label": label,
        "title":         f"{label}: {title}" if label else title,
        "date":          s.get("date", ""),
        "tracks":        ", ".join(tracks),
        "url":           f"{PUBLIC_URL}#session-{int(sid)}",
        "text":          text[:1000],
    }


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2))


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force",   action="store_true", help="Re-embed all sessions")
    args = parser.parse_args()

    data     = json.loads(DEVLOG_PATH.read_text())
    sessions = sorted(data["sessions"], key=lambda s: s["sort_key"])
    state    = load_state()

    to_upsert = []
    skipped   = 0

    for s in sessions:
        sid  = str(s.get("id", s.get("sort_key", 0)))
        text = session_text(s)
        h    = content_hash(text)

        if not args.force and state.get(sid) == h:
            skipped += 1
            continue

        meta = session_meta(s, text)
        to_upsert.append((f"devlog__session_{sid}", text, meta, sid, h))

    print(f"Devlog: {len(sessions)} sessions — {len(to_upsert)} to embed, {skipped} unchanged")

    if not to_upsert:
        print("Nothing to do.")
        return

    if args.dry_run:
        for vid, text, meta, sid, h in to_upsert:
            print(f"  [dry-run] would upsert {vid}: {meta['title'][:60]}")
        return

    vo  = get_voyage_client()
    idx = get_pinecone_index()

    texts  = [t for _, t, _, _, _ in to_upsert]
    result = vo.embed(texts, model="voyage-3", input_type="document")
    embeds = result.embeddings

    vectors = []
    for i, (vid, text, meta, sid, h) in enumerate(to_upsert):
        vectors.append({"id": vid, "values": embeds[i], "metadata": meta})

    idx.upsert(vectors=vectors, namespace=NAMESPACE)

    for _, _, _, sid, h in to_upsert:
        state[sid] = h
    save_state(state)

    print(f"Upserted {len(vectors)} vectors → Pinecone namespace '{NAMESPACE}'")


if __name__ == "__main__":
    main()
