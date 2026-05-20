"""
One-shot analysis of the discord Pinecone namespace.

Fetches all vector metadata, prints statistics, then runs a Claude Haiku pass on
a stratified sample to assess noise and surface clustering intuitions.

Also verifies Discord deep-link fields (guild_id, channel_id, message_id).

Usage:
    python3 ingest/analyze_discord.py
    python3 ingest/analyze_discord.py --sample-size 300
"""

import argparse
import json
import os
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from pinecone import Pinecone
import anthropic


GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "")
DISCORD_LINK_NOTE = "(guild_id not in metadata — using env DISCORD_GUILD_ID)"


def discord_link(meta: dict) -> str:
    guild = GUILD_ID
    if meta.get("chunk_type") == "thread":
        thread_id = meta.get("thread_id", "")
        return f"https://discord.com/channels/{guild}/{thread_id}"
    else:
        channel_id = meta.get("channel_id", "")
        message_id = meta.get("message_id", "")
        return f"https://discord.com/channels/{guild}/{channel_id}/{message_id}"


def fetch_all_discord_metadata(idx) -> list[dict]:
    """List all IDs in the discord namespace, then fetch metadata in batches."""
    print("Listing all discord vector IDs...")
    all_ids = []
    for page in idx.list(namespace="discord"):
        all_ids.extend(item.id for item in page.vectors)
    print(f"  {len(all_ids)} IDs found")

    print("Fetching metadata in batches...")
    records = []
    batch_size = 100
    for i in range(0, len(all_ids), batch_size):
        batch_ids = all_ids[i:i + batch_size]
        fetched = idx.fetch(ids=batch_ids, namespace="discord")
        for vid, vec in fetched.vectors.items():
            meta = dict(vec.metadata)
            meta["_id"] = vid
            records.append(meta)
        if i % 500 == 0 and i > 0:
            print(f"  {i}/{len(all_ids)}...")
    print(f"  {len(records)} records fetched")
    return records


def print_stats(records: list[dict]):
    print("\n" + "═" * 56)
    print("DISCORD CORPUS STATS")
    print("═" * 56)

    types = Counter(r.get("chunk_type") for r in records)
    print(f"\nChunk types:")
    for t, n in types.most_common():
        print(f"  {t:12s}: {n:4d}")

    stars = [r for r in records if r.get("star_count", 0) > 0]
    has_url = [r for r in records if r.get("has_url")]
    print(f"\nStarred:  {len(stars)}")
    print(f"Has URL:  {len(has_url)}")

    authors = Counter(r.get("author") for r in records)
    print(f"\nTop authors ({len(authors)} unique):")
    for author, n in authors.most_common(10):
        print(f"  @{author:20s}: {n}")

    text_lengths = [len(r.get("text", "")) for r in records]
    print(f"\nText length (chars):")
    print(f"  median : {sorted(text_lengths)[len(text_lengths)//2]}")
    print(f"  mean   : {sum(text_lengths)//len(text_lengths)}")
    print(f"  max    : {max(text_lengths)}")

    # Deep-link check
    sample = records[0]
    print(f"\nDeep-link check (sample):")
    print(f"  chunk_type : {sample.get('chunk_type')}")
    print(f"  channel_id : {sample.get('channel_id')}")
    print(f"  message_id : {sample.get('message_id')}")
    print(f"  thread_id  : {sample.get('thread_id', 'n/a')}")
    print(f"  guild_id   : {GUILD_ID} {DISCORD_LINK_NOTE}")
    print(f"  sample link: {discord_link(sample)}")


def stratified_sample(records: list[dict], n: int) -> list[dict]:
    """Sample across chunk types and star/url status."""
    buckets = defaultdict(list)
    for r in records:
        key = (r.get("chunk_type"), bool(r.get("star_count", 0)), bool(r.get("has_url")))
        buckets[key].append(r)

    sample = []
    per_bucket = max(1, n // len(buckets))
    for key, bucket in buckets.items():
        sample.extend(random.sample(bucket, min(per_bucket, len(bucket))))

    random.shuffle(sample)
    return sample[:n]


def claude_analysis(sample: list[dict], client: anthropic.Anthropic) -> str:
    """Run Claude Haiku on the sample for noise assessment + clustering."""
    # Format messages for the prompt
    lines = []
    for i, r in enumerate(sample):
        chunk_type = r.get("chunk_type", "?")
        stars = r.get("star_count", 0)
        has_url = r.get("has_url", False)
        text = r.get("text", "")
        # Strip the header prefix to show just the content
        if "\n\n" in text:
            content = text.split("\n\n", 1)[1][:300]
        else:
            content = text[:300]
        flag = ("⭐" if stars else "") + ("🔗" if has_url else "")
        lines.append(f"[{i+1}] ({chunk_type}{' '+flag if flag else ''})\n{content}\n")

    messages_text = "\n".join(lines)

    prompt = f"""You are analyzing a corpus of {len(sample)} messages from a Protocol Institute Discord channel called "idle-protocol-musings". Members share thoughts, links, and discussions about protocol science, governance, and related ideas.

Here are the messages (sample of the full corpus of ~2,400):

{messages_text}

Please provide:

1. NOISE ASSESSMENT
   - What patterns of genuine noise did you find (if any)? Give examples.
   - What fraction of this sample seems like noise vs. signal?
   - Are there borderline categories we should review our filter for?

2. TOPIC CLUSTERS
   - Identify 6–10 recurring topic clusters you see across these messages.
   - For each cluster: give it a name, a 1-sentence description, and 2–3 example message numbers.

3. QUALITY OBSERVATIONS
   - What makes the high-signal messages distinctive in this corpus?
   - Any surprising content types or authors worth noting?

4. FILTER RECOMMENDATIONS
   - Based on this sample, would you adjust the current filter (originals ≥20 chars, replies ≥150 chars)?
   - Any other filter suggestions?

Be specific and cite message numbers where helpful."""

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-size", type=int, default=250)
    args = parser.parse_args()

    pc = Pinecone(api_key=os.environ["PINECONE_API_KEY"])
    idx = pc.Index(host=os.environ["PINECONE_C3PO_HOST"])
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    records = fetch_all_discord_metadata(idx)
    print_stats(records)

    print(f"\n{'═'*56}")
    print(f"CLAUDE HAIKU ANALYSIS (sample n={args.sample_size})")
    print("═" * 56)
    sample = stratified_sample(records, args.sample_size)
    print(f"Sample: {Counter(r.get('chunk_type') for r in sample)}")
    print("Running Claude Haiku pass...\n")

    analysis = claude_analysis(sample, client)
    print(analysis)

    # Save output
    out_path = Path(__file__).parent.parent / "sources" / "discord_analysis.md"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(
        f"# Discord Corpus Analysis\n\n"
        f"Sample: {args.sample_size} of {len(records)} vectors\n\n"
        f"## Stats\n\nSee terminal output.\n\n"
        f"## Claude Haiku Analysis\n\n{analysis}\n"
    )
    print(f"\nSaved to {out_path}")


if __name__ == "__main__":
    main()
