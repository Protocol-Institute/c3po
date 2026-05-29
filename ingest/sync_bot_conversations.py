"""
Ingest Discord bot conversation spool into Pinecone transcripts namespace.

Reads JSON files from data/spool/bot_conversations/, embeds each Q&A
exchange as chunk_type=discord_conversation, upserts to Pinecone, then
deletes the processed spool file.

Vector ID: discord_conv:{thread_id}:{turn}  (stable, deduplicating)
Namespace: transcripts

Usage:
    python3 ingest/sync_bot_conversations.py
    python3 ingest/sync_bot_conversations.py --dry-run
"""

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
from utils import embed_chunks, get_voyage_client, get_pinecone_index

load_dotenv(Path(__file__).parent.parent / ".env")

NAMESPACE = "transcripts"
SPOOL_DIR = Path(__file__).parent.parent / "data" / "spool" / "bot_conversations"


def build_embed_text(record: dict) -> str:
    q = (record.get("question") or "").strip()
    a = (record.get("answer") or "").strip()
    return f"Q: {q}\nA: {a}"


def vec_id(record: dict) -> str:
    return f"discord_conv:{record['thread_id']}:{record.get('turn', 1)}"


def run(dry_run: bool = False) -> int:
    if not SPOOL_DIR.exists():
        print("Spool directory does not exist — nothing to do.")
        return 0

    files = sorted(SPOOL_DIR.glob("*.json"))
    if not files:
        print(f"No spool files in {SPOOL_DIR}")
        return 0

    print(f"Found {len(files)} spool file(s)")

    vc  = None if dry_run else get_voyage_client()
    idx = None if dry_run else get_pinecone_index()

    processed = 0
    errors = 0
    for fpath in files:
        try:
            record = json.loads(fpath.read_text())
        except Exception as exc:
            print(f"  ERROR reading {fpath.name}: {exc}")
            errors += 1
            continue

        text = build_embed_text(record)
        vid  = vec_id(record)
        meta = {
            "chunk_type":    "discord_conversation",
            "bot_id":        record.get("bot_id", "c3po_bot"),
            "ts":            record.get("ts", ""),
            "user_id_hash":  record.get("user_id_hash", ""),
            "channel_id":    record.get("channel_id", ""),
            "thread_id":     record.get("thread_id", ""),
            "text":          text[:1500],
            "question":      (record.get("question") or "")[:500],
            "turn":          record.get("turn", 1),
            "latency_ms":    record.get("latency_ms", 0),
            "sources_count": len(record.get("sources") or []),
        }

        tag = "DRY-RUN" if dry_run else "INGEST"
        tid = meta["thread_id"][:12] if meta["thread_id"] else "?"
        print(f"  [{tag}]  thread={tid} turn={meta['turn']}  q={meta['question'][:60]}")

        if not dry_run:
            vectors = embed_chunks([text], vc)
            idx.upsert(
                vectors=[{"id": vid, "values": vectors[0], "metadata": meta}],
                namespace=NAMESPACE,
            )
            fpath.unlink()

        processed += 1

    print(f"\n── Summary ──")
    print(f"  Processed: {processed}  Errors: {errors}")
    if dry_run:
        print("  (dry run — nothing written or deleted)")
    return errors


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest Discord bot conversation spool")
    parser.add_argument("--dry-run", action="store_true", help="Report without writing to Pinecone")
    args = parser.parse_args()
    sys.exit(run(dry_run=args.dry_run))
