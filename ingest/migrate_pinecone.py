#!/usr/bin/env python3
"""
migrate_pinecone.py — Copy all vectors from personal Pinecone account to PI org account.

Uses Pinecone list+fetch (no re-embedding). Runs namespace by namespace, batched.
Safe to re-run: upserts are idempotent.

Usage:
    python3 ingest/migrate_pinecone.py [--dry-run] [--namespace <ns>]

Reads from: PINECONE_PERSONAL_API_KEY + PINECONE_PERSONAL_C3PO_HOST (source)
Writes to:  PINECONE_PI_API_KEY + PINECONE_PI_C3PO_HOST (destination)
"""

import os
import sys
import argparse
import time
from dotenv import load_dotenv
from pinecone import Pinecone

load_dotenv()

SRC_KEY  = os.environ.get("PINECONE_PERSONAL_API_KEY") or os.environ.get("PINECONE_API_KEY")
SRC_HOST = os.environ.get("PINECONE_PERSONAL_C3PO_HOST") or os.environ.get("PINECONE_C3PO_HOST")
DST_KEY  = os.environ["PINECONE_PI_API_KEY"]
DST_HOST = os.environ["PINECONE_PI_C3PO_HOST"]

FETCH_BATCH  = 50    # IDs per fetch call (large URLs cause 414; keep well under limit)
UPSERT_BATCH = 100   # vectors per upsert call

def migrate_namespace(src_idx, dst_idx, namespace: str, dry_run: bool) -> int:
    print(f"\n  Namespace: {namespace!r}")

    # Collect all IDs via paginated list; list() yields ListItem objects — extract .id
    all_ids = []
    for id_batch in src_idx.list(namespace=namespace):
        all_ids.extend(item.id if hasattr(item, "id") else str(item) for item in id_batch)
    print(f"    {len(all_ids):,} vectors to migrate")

    if not all_ids or dry_run:
        return len(all_ids)

    migrated = 0
    for i in range(0, len(all_ids), FETCH_BATCH):
        batch_ids = all_ids[i : i + FETCH_BATCH]
        fetch_result = src_idx.fetch(ids=batch_ids, namespace=namespace)
        vectors = fetch_result.get("vectors") or fetch_result.vectors

        # Build upsert payload preserving id, values, metadata
        upsert_payload = [
            {
                "id": vid,
                "values": v.values,
                **({"metadata": dict(v.metadata)} if v.metadata else {}),
            }
            for vid, v in vectors.items()
        ]

        if upsert_payload:
            for j in range(0, len(upsert_payload), UPSERT_BATCH):
                dst_idx.upsert(vectors=upsert_payload[j : j + UPSERT_BATCH], namespace=namespace)
            migrated += len(upsert_payload)

        pct = (i + len(batch_ids)) / len(all_ids) * 100
        print(f"    {min(i + FETCH_BATCH, len(all_ids)):,}/{len(all_ids):,} ({pct:.0f}%)", end="\r")
        time.sleep(0.1)   # gentle rate-limit headroom

    print(f"    {migrated:,} vectors upserted" + " " * 20)
    return migrated


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Count vectors only, no writes")
    parser.add_argument("--namespace", help="Migrate one namespace only")
    args = parser.parse_args()

    print("Source:", SRC_HOST)
    print("Dest:  ", DST_HOST)
    if args.dry_run:
        print("DRY RUN — no vectors will be written\n")

    src_pc = Pinecone(api_key=SRC_KEY)
    dst_pc = Pinecone(api_key=DST_KEY)

    src_idx = src_pc.Index(host=SRC_HOST)
    dst_idx = dst_pc.Index(host=DST_HOST)

    src_stats = src_idx.describe_index_stats()
    namespaces = list(src_stats.namespaces.keys())

    if args.namespace:
        if args.namespace not in namespaces:
            print(f"Namespace {args.namespace!r} not found. Available: {namespaces}")
            sys.exit(1)
        namespaces = [args.namespace]

    # Skip humboldt — owned by humboldt project
    if "humboldt" in namespaces and not args.namespace:
        print("Skipping 'humboldt' namespace (owned by humboldt project, not c3po).")
        namespaces = [ns for ns in namespaces if ns != "humboldt"]

    print(f"Namespaces to migrate: {namespaces}")
    total_src = sum(src_stats.namespaces[ns].vector_count for ns in namespaces)
    print(f"Total source vectors: {total_src:,}")

    grand_total = 0
    for ns in namespaces:
        grand_total += migrate_namespace(src_idx, dst_idx, ns, args.dry_run)

    print(f"\nMigration {'preview' if args.dry_run else 'complete'}.")
    print(f"Total vectors {'counted' if args.dry_run else 'migrated'}: {grand_total:,}")

    if not args.dry_run:
        print("\nVerifying destination counts...")
        time.sleep(3)   # allow index to settle
        dst_stats = dst_idx.describe_index_stats()
        print(f"Source total:      {total_src:,}")
        print(f"Destination total: {dst_stats.total_vector_count:,}")
        if dst_stats.total_vector_count >= total_src:
            print("✅ Counts match — migration successful.")
        else:
            diff = total_src - dst_stats.total_vector_count
            print(f"⚠️  {diff:,} vectors missing — re-run to fill gaps (upserts are idempotent).")


if __name__ == "__main__":
    main()
