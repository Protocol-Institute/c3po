"""
Ingest public web chat conversations into Pinecone transcripts namespace.

Polls GET /api/chats (with ADMIN_KEY) for public conversations, fetches
each full conversation, embeds the Q+A text, and upserts to the transcripts
namespace as chunk_type=web_conversation.

Quality gate: status == "public" (auto-moderated or admin-approved).
State file: data/web_chats_state.json — tracks ingested chatIds.
Vector ID: web_conv:{chatId}

Usage:
    python3 ingest/sync_web_chats.py
    python3 ingest/sync_web_chats.py --dry-run
    python3 ingest/sync_web_chats.py --force     # re-embed already-ingested chats
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
from utils import embed_chunks, get_voyage_client, get_pinecone_index

load_dotenv(Path(__file__).parent.parent / ".env")

import os

WORKER_BASE = "https://c3po.protocolized.io"
NAMESPACE   = "transcripts"
STATE_PATH  = Path(__file__).parent.parent / "data" / "web_chats_state.json"
SLEEP_SECS  = 0.3


# ── HTTP ──────────────────────────────────────────────────────────────────────

def _get(url: str, admin_key: str) -> dict:
    req = urllib.request.Request(url, headers={
        "X-Admin-Key":  admin_key,
        "Accept":       "application/json",
        "User-Agent":   "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def fetch_chat_list(admin_key: str, limit: int = 100) -> list[dict]:
    data = _get(f"{WORKER_BASE}/api/chats?limit={limit}", admin_key)
    return data.get("submissions", [])


def fetch_chat(chat_id: str, admin_key: str) -> dict | None:
    try:
        return _get(f"{WORKER_BASE}/api/chat/{chat_id}", admin_key)
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):
            return None
        raise


# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# ── Embedding ─────────────────────────────────────────────────────────────────

def build_embed_text(turns: list[dict]) -> str:
    parts = []
    for t in turns:
        q = (t.get("q") or t.get("query") or "").strip()
        a = (t.get("answer") or "").strip()
        if q and a:
            parts.append(f"Q: {q}\nA: {a}")
    text = "\n\n".join(parts)
    return text[:3000]  # keep under voyage-3 context limit comfortably


def vec_id(chat_id: str) -> str:
    return f"web_conv:{chat_id}"


# ── Main ──────────────────────────────────────────────────────────────────────

def run(dry_run: bool = False, force: bool = False) -> int:
    admin_key = os.environ.get("ADMIN_KEY", "")
    if not admin_key:
        print("ERROR: ADMIN_KEY not set in environment")
        return 1

    state = load_state()
    vc    = None if dry_run else get_voyage_client()
    idx   = None if dry_run else get_pinecone_index()

    print("Fetching chat list from Worker…")
    try:
        submissions = fetch_chat_list(admin_key)
    except Exception as exc:
        print(f"ERROR fetching chat list: {exc}")
        return 1

    eligible = [s for s in submissions if s.get("status") == "public"]
    print(f"  Total: {len(submissions)}  Public: {len(eligible)}")

    new_count = updated_count = skipped_count = errors = 0

    for slim in eligible:
        chat_id = slim.get("chatId")
        if not chat_id:
            continue

        already = chat_id in state
        if already and not force:
            skipped_count += 1
            continue

        time.sleep(SLEEP_SECS)
        try:
            entry = fetch_chat(chat_id, admin_key)
        except Exception as exc:
            print(f"  ERROR fetching {chat_id}: {exc}")
            errors += 1
            continue

        if not entry:
            skipped_count += 1
            continue

        turns = entry.get("turns") or []
        if not turns:
            skipped_count += 1
            continue

        text = build_embed_text(turns)
        if not text.strip():
            skipped_count += 1
            continue

        vid  = vec_id(chat_id)
        meta = {
            "chunk_type":  "web_conversation",
            "bot_id":      "c3po_web",
            "chat_id":     chat_id,
            "ts":          entry.get("ts", ""),
            "text":        text[:1500],
            "question":    (turns[0].get("q") or "")[:500],
            "turn_count":  len(turns),
            "rating":      entry.get("rating") or 0,
            "user_name":   (entry.get("userName") or "")[:100],
        }

        status = "UPDATED" if already else "NEW"
        tag    = "DRY-RUN" if dry_run else status
        print(f"  [{tag}]  {chat_id}  turns={meta['turn_count']}  q={meta['question'][:60]}")

        if not dry_run:
            vectors = embed_chunks([text], vc)
            idx.upsert(
                vectors=[{"id": vid, "values": vectors[0], "metadata": meta}],
                namespace=NAMESPACE,
            )
            state[chat_id] = {
                "ts":          entry.get("ts", ""),
                "ingested_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "turn_count":  len(turns),
            }
            save_state(state)

        if already:
            updated_count += 1
        else:
            new_count += 1

    print(f"\n── Summary ──")
    print(f"  New: {new_count}  Updated: {updated_count}  Skipped: {skipped_count}  Errors: {errors}")
    if dry_run:
        print("  (dry run — nothing written)")
    return errors


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest public web chats into Pinecone transcripts")
    parser.add_argument("--dry-run", action="store_true", help="Report without writing to Pinecone")
    parser.add_argument("--force",   action="store_true", help="Re-embed already-ingested chats")
    args = parser.parse_args()
    sys.exit(run(dry_run=args.dry_run, force=args.force))
