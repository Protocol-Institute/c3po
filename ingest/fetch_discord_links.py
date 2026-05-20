"""
Fetch external URLs shared in Discord channels and ingest content into the
`discord_links` Pinecone namespace.

Maintains a registry at data/discord_links_registry.json:
  - Records EVERY URL seen across all discord channels (even unfetchable ones)
  - Tracks fetch status: pending / fetched / failed / skipped
  - Deduplicates: a URL shared in multiple channels is fetched once

Fetch strategy:
  - Attempts to fetch all URLs including Twitter/X and YouTube
  - Skips: discord.com (internal), tenor.com (GIFs), docs.google.com (private)
  - Marks as failed: paywalled, bot-blocked, timeout, SSL errors
  - Content < 300 chars after extraction = treated as failed (paywall/bot wall)

Usage:
    python3 ingest/fetch_discord_links.py           # fetch all pending
    python3 ingest/fetch_discord_links.py --dry-run
    python3 ingest/fetch_discord_links.py --limit 100
    python3 ingest/fetch_discord_links.py --refresh-failed  # retry previously failed
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
from utils import clean_text, chunk_text, embed_chunks, chunk_id, get_voyage_client, get_pinecone_index, PINECONE_BATCH

load_dotenv(Path(__file__).parent.parent / ".env")

NAMESPACE     = "discord_links"
SOURCE_NS     = ["discord", "sig"]   # namespaces to harvest URLs from
REGISTRY_PATH = Path(__file__).parent.parent / "data" / "discord_links_registry.json"

# ── Prompt injection filter ────────────────────────────────────────────────────
# These patterns detect imperative commands that could manipulate LLM context
# when the extracted text is injected as a retrieval chunk.
# Legitimate AI-safety articles discussing these topics use quoted/third-person
# framing; raw imperatives targeted at a model are the actual risk.

_INJECTION_PATTERNS = [
    # Direct override commands
    re.compile(r'\bignore\s+(all\s+)?(previous|prior|above|your|the\s+above)\s+(instructions?|rules?|guidelines?|constraints?|system\s+prompt)', re.I),
    re.compile(r'\bdisregard\s+(all\s+)?(previous|prior|above|your)\s+(instructions?|rules?|guidelines?|directives?)', re.I),
    re.compile(r'\bforget\s+(all\s+)?(previous|prior|your|everything|the\s+above)', re.I),
    re.compile(r'\boverride\s+(your\s+)?(instructions?|system\s+prompt|rules?|guidelines?|constraints?)', re.I),

    # Persona / role-swap injections
    re.compile(r'\byou\s+are\s+now\s+(a\s+|an\s+)?(?!the\s+Protocol|C3PO|an?\s+AI\s+assistant)', re.I),
    re.compile(r'\bact\s+as\s+if\s+(you\s+(have\s+no|are\s+not|don\'?t\s+have)|there\s+(are|were)\s+no)\s+(restrictions?|rules?|guidelines?|filters?|limits?|constraints?)', re.I),
    re.compile(r'\bpretend\s+(you\s+are|you\'?re|to\s+be)\s+.{0,40}(no\s+restrictions?|unrestricted|without\s+(guidelines?|rules?|constraints?))', re.I),

    # System prompt / instruction injection markers
    re.compile(r'(?m)^(SYSTEM|NEW\s+INSTRUCTIONS?|UPDATED\s+INSTRUCTIONS?|ASSISTANT|USER)\s*:\s*\n', re.I),
    re.compile(r'(?m)^#{2,}\s*(system|instructions?|rules?)\s*$', re.I),

    # LLM token boundary attacks (model-specific control tokens)
    re.compile(r'<\|im_start\|>|<\|im_end\|>|<\|endoftext\|>'),
    re.compile(r'\[INST\]|\[/INST\]|\[SYS\]|\[/SYS\]'),
    re.compile(r'<s>\s*(You are|SYSTEM|INST)', re.I),

    # DAN / jailbreak invocations
    re.compile(r'\bDAN\s+(mode|prompt|jailbreak)\b', re.I),
    re.compile(r'\bjailbreak\b.{0,60}\b(instructions?|rules?|guidelines?|system)', re.I),

    # Zero-width / invisible-character payload markers (after stripping, these
    # indicate the page tried to hide injection text from human readers)
    re.compile(r'[​‌‍‎‏﻿]{3,}'),  # 3+ invisible chars in a row
]

# Minimum fraction of a 500-char window that must be printable ASCII/common Unicode
# for the page to be considered non-adversarial text.
_MIN_PRINTABLE_RATIO = 0.80


def _strip_invisible(text: str) -> str:
    """Remove zero-width and other invisible Unicode characters."""
    return re.sub(r'[​‌‍‎‏﻿­]', '', text)


def is_injection_attempt(text: str) -> tuple[bool, str]:
    """
    Returns (True, pattern_description) if the text contains prompt injection
    markers, (False, "") otherwise.

    Scans the full extracted text, not individual chunks, so context-stripping
    attacks that hide intent across chunk boundaries are also caught.
    """
    clean = _strip_invisible(text)

    # Invisible-char density check (adversarial steganography)
    if len(text) > 100:
        invisible = sum(1 for c in text if ord(c) in (0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0xFEFF, 0x00AD))
        if invisible / len(text) > 0.01:  # >1% invisible chars
            return True, f"high invisible-char density ({invisible}/{len(text)})"

    for pat in _INJECTION_PATTERNS:
        m = pat.search(clean)
        if m:
            snippet = clean[max(0, m.start()-30):m.end()+30].replace('\n', ' ')
            return True, f"pattern '{pat.pattern[:60]}' matched: «{snippet}»"

    return False, ""

MIN_CONTENT_CHARS = 300   # below this = paywall / bot wall / empty page
REQUEST_TIMEOUT   = 15
INTER_REQUEST_SLEEP = 0.5

# Never attempt to fetch these domains — no meaningful text content
SKIP_DOMAINS = {
    "discord.com", "discordapp.com",
    "tenor.com", "giphy.com",
    "docs.google.com", "drive.google.com",
    "chatgpt.com", "claude.ai",
    "www.amazon.com", "amazon.com",
    "farcaster.xyz",
    "t.me",            # Telegram
    "mailto:",
}

# Deferred for a separate YouTube transcript fetch pass
YOUTUBE_DOMAINS = {
    "youtube.com", "www.youtube.com",
    "youtu.be", "m.youtube.com",
}

# Deferred for a paid Twitter/X API run
TWITTER_DOMAINS = {
    "twitter.com", "www.twitter.com",
    "x.com", "www.x.com",
}


# ── URL normalization ──────────────────────────────────────────────────────────

def normalize_url(url: str) -> str:
    """Stable key for dedup: lowercase scheme+netloc+path, strip tracking params."""
    try:
        p = urllib.parse.urlparse(url.strip())
        # Keep query string only for sites where it's part of the identity
        keep_query_domains = {"youtube.com", "youtu.be", "m.youtube.com",
                               "www.youtube.com", "open.spotify.com"}
        netloc = p.netloc.lower().removeprefix("www.")
        query = p.query if netloc in keep_query_domains else ""
        normalized = urllib.parse.urlunparse((
            p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"),
            "", query, ""
        ))
        return normalized
    except Exception:
        return url.strip()


def domain_of(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return ""


# ── Registry ───────────────────────────────────────────────────────────────────

def load_registry() -> dict:
    if REGISTRY_PATH.exists():
        return json.loads(REGISTRY_PATH.read_text())
    return {}


def save_registry(registry: dict):
    REGISTRY_PATH.write_text(json.dumps(registry, indent=2, ensure_ascii=False))


def register_url(registry: dict, url: str, channel_name: str,
                 channel_id: str, message_id: str, author: str):
    """Add a URL to the registry if not already present."""
    key = normalize_url(url)
    if key not in registry:
        registry[key] = {
            "url": url,
            "domain": domain_of(url),
            "first_seen": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sources": [],
            "fetch_status": "pending",
        }
    # Record each Discord source that shared this URL
    source_entry = {"channel_name": channel_name, "channel_id": channel_id,
                    "message_id": message_id, "author": author}
    if source_entry not in registry[key]["sources"]:
        registry[key]["sources"].append(source_entry)


# ── Fetch and parse ────────────────────────────────────────────────────────────

def fetch_url(url: str) -> str | None:
    """Fetch a URL and return clean text, or None on failure."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            content_type = resp.headers.get("Content-Type", "")
            if "html" not in content_type and "text" not in content_type:
                return None  # binary, PDF, etc.
            raw = resp.read(500_000)  # cap at 500KB
            encoding = resp.headers.get_content_charset("utf-8")
            return raw.decode(encoding, errors="replace")
    except Exception:
        return None


def extract_text(html: str) -> str:
    """Extract clean readable text from HTML."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("Install beautifulsoup4")
        sys.exit(1)

    soup = BeautifulSoup(html, "html.parser")

    # Extract title
    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else ""

    # Remove boilerplate
    for tag in soup.find_all(["script", "style", "nav", "header", "footer",
                               "aside", "figure", "figcaption", "button",
                               "form", "svg", "noscript"]):
        tag.decompose()

    # Try to find main content area
    main = (soup.find("main") or soup.find("article") or
            soup.find(id=re.compile(r"content|main|article", re.I)) or
            soup.find(class_=re.compile(r"content|main|article|post|entry", re.I)) or
            soup.body or soup)

    body = re.sub(r"\s+", " ", main.get_text(separator=" ")).strip()

    if title and not body.startswith(title):
        return f"{title}\n\n{body}"
    return body


# ── Ingest ─────────────────────────────────────────────────────────────────────

def ingest_page(url: str, text: str, entry: dict, vc, index) -> int:
    """Chunk, embed, and upsert a fetched page. Returns vector count.
    Raises ValueError if injection patterns are detected."""
    flagged, reason = is_injection_attempt(text)
    if flagged:
        raise ValueError(f"injection_filter: {reason}")

    domain = entry.get("domain", domain_of(url))
    sources = entry.get("sources", [])
    primary_source = sources[0] if sources else {}

    prefix = f"Source: {url}\nDomain: {domain}\n\n"
    chunks = chunk_text(clean_text(text))
    if not chunks:
        return 0

    prefixed = [prefix + c for c in chunks]
    vectors = embed_chunks(prefixed, vc)

    records = []
    for i, (chunk, vector) in enumerate(zip(chunks, vectors)):
        meta = {
            "source": "discord_links",
            "namespace": NAMESPACE,
            "chunk_type": "web_content",
            "url": url,
            "domain": domain,
            "channel_name": primary_source.get("channel_name", ""),
            "channel_id": primary_source.get("channel_id", ""),
            "message_id": primary_source.get("message_id", ""),
            "author": primary_source.get("author", ""),
            "source_count": len(sources),     # how many discord msgs shared this
            "fetch_date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "chunk_index": i,
            "chunk_total": len(chunks),
            "text": chunk[:1000],
        }
        records.append({"id": f"discord_link__{chunk_id(url + str(i))}", "values": vector, "metadata": meta})

    for i in range(0, len(records), PINECONE_BATCH):
        index.upsert(vectors=records[i:i + PINECONE_BATCH], namespace=NAMESPACE)

    return len(records)


# ── Harvest URLs from Pinecone discord + sig namespaces ───────────────────────

def harvest_urls_from_namespace(idx, namespace: str) -> dict:
    """Pull all URLs from a Pinecone namespace's `urls` metadata field."""
    all_ids = []
    for page in idx.list(namespace=namespace):
        all_ids.extend(item.id for item in page.vectors)

    additions = {}
    for i in range(0, len(all_ids), 99):
        batch = idx.fetch(ids=all_ids[i:i+99], namespace=namespace)
        for vid, vec in batch.vectors.items():
            m = vec.metadata
            raw_urls = m.get("urls", "[]")
            try:
                urls = json.loads(raw_urls) if isinstance(raw_urls, str) else (raw_urls or [])
            except Exception:
                urls = []
            for url in urls:
                if not url or not isinstance(url, str):
                    continue
                # Strip trailing punctuation that gets caught in URL extraction
                url = url.rstrip(").,;")
                key = normalize_url(url)
                if key not in additions:
                    additions[key] = {
                        "url": url,
                        "domain": domain_of(url),
                        "first_seen": m.get("timestamp", m.get("meeting_date", "")),
                        "sources": [],
                        "fetch_status": "pending",
                        "source_namespace": namespace,
                    }
                src = {
                    "channel_name": m.get("channel_name", ""),
                    "channel_id": m.get("channel_id", ""),
                    "message_id": m.get("message_id", m.get("thread_id", "")),
                    "author": m.get("author", m.get("sig_display", "")),
                }
                if src not in additions[key]["sources"]:
                    additions[key]["sources"].append(src)
    return additions


def harvest_urls_from_pinecone(idx) -> dict:
    """Pull all URLs from discord + sig namespaces, merge, return registry additions."""
    additions: dict = {}
    for ns in SOURCE_NS:
        print(f"  Harvesting from {ns} namespace...")
        ns_additions = harvest_urls_from_namespace(idx, ns)
        print(f"    {len(ns_additions)} unique URLs")
        for key, entry in ns_additions.items():
            if key not in additions:
                additions[key] = entry
            else:
                for src in entry["sources"]:
                    if src not in additions[key]["sources"]:
                        additions[key]["sources"].append(src)
    print(f"  Total: {len(additions)} unique URLs across all namespaces")
    return additions


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, help="Max URLs to fetch this run")
    parser.add_argument("--refresh-failed", action="store_true",
                        help="Retry URLs previously marked as failed")
    parser.add_argument("--rescan-fetched", action="store_true",
                        help="Re-run injection filter on already-fetched URLs (text-only, no re-fetch or re-embed)")
    args = parser.parse_args()

    pc_index = get_pinecone_index()
    vc = None if args.dry_run else get_voyage_client()

    # Step 1: harvest URLs from discord namespace and merge into registry
    registry = load_registry()
    new_urls = harvest_urls_from_pinecone(pc_index)

    added = 0
    for key, entry in new_urls.items():
        if key not in registry:
            registry[key] = entry
            added += 1
        else:
            # Merge sources
            existing_srcs = registry[key].get("sources", [])
            for src in entry["sources"]:
                if src not in existing_srcs:
                    existing_srcs.append(src)
            registry[key]["sources"] = existing_srcs

    print(f"  {added} new URLs added to registry ({len(registry)} total)")
    if not args.dry_run:
        save_registry(registry)

    # Optional: re-scan already-fetched entries with injection filter
    # (text is not stored in registry; this re-fetches the page, text-only, no re-embed)
    if args.rescan_fetched:
        print("\nRe-scanning fetched URLs for injection patterns...")
        already_fetched = [k for k, e in registry.items() if e.get("fetch_status") == "fetched"]
        flagged_count = 0
        for key in already_fetched:
            url = registry[key].get("url", key)
            html = fetch_url(url)
            if not html:
                continue
            text = extract_text(html)
            flagged, reason = is_injection_attempt(text)
            if flagged:
                print(f"  ⚠ FLAGGED (already ingested): {url[:80]}")
                print(f"    Reason: {reason[:100]}")
                registry[key]["injection_flag"] = reason
                flagged_count += 1
        save_registry(registry)
        print(f"  Scanned {len(already_fetched)} fetched URLs, flagged {flagged_count}")
        if flagged_count:
            print(f"  NOTE: flagged entries are still in Pinecone — manual review + delete required")
        return

    # Step 2: determine what to fetch
    if args.refresh_failed:
        to_fetch = [k for k, e in registry.items() if e["fetch_status"] in ("pending", "failed")]
    else:
        to_fetch = [k for k, e in registry.items() if e["fetch_status"] == "pending"]

    # Classify skip/defer domains
    skipped      = [k for k in to_fetch if domain_of(registry[k]["url"]) in SKIP_DOMAINS]
    deferred_yt  = [k for k in to_fetch if k not in skipped
                    and domain_of(registry[k]["url"]) in YOUTUBE_DOMAINS]
    deferred_tw  = [k for k in to_fetch if k not in skipped and k not in deferred_yt
                    and domain_of(registry[k]["url"]) in TWITTER_DOMAINS]
    to_fetch     = [k for k in to_fetch
                    if k not in skipped and k not in deferred_yt and k not in deferred_tw]

    if not args.dry_run:
        for key in skipped:
            registry[key]["fetch_status"] = "skipped"
            registry[key]["skip_reason"] = "domain blocklist"
        for key in deferred_yt:
            registry[key]["fetch_status"] = "deferred"
            registry[key]["defer_reason"] = "youtube — run --youtube-only for transcript fetch"
        for key in deferred_tw:
            registry[key]["fetch_status"] = "deferred"
            registry[key]["defer_reason"] = "twitter/x — run --twitter-only for paid API fetch"
        save_registry(registry)

    print(f"\n  Pending fetch    : {len(to_fetch)}")
    print(f"  Skipped          : {len(skipped)}")
    print(f"  Deferred (YT)    : {len(deferred_yt)}")
    print(f"  Deferred (X)     : {len(deferred_tw)}")

    if args.limit is not None:
        to_fetch = to_fetch[:args.limit]
        print(f"  Capped at     : {len(to_fetch)}")

    if args.dry_run:
        print("\n[DRY RUN] Would fetch:")
        for key in to_fetch[:20]:
            print(f"  {registry[key]['url'][:90]}")
        if len(to_fetch) > 20:
            print(f"  ... and {len(to_fetch)-20} more")
        return

    # Step 3: fetch and ingest
    fetched_ok    = 0
    fetched_fail  = 0
    injections    = 0
    total_vectors = 0

    for i, key in enumerate(to_fetch):
        entry = registry[key]
        url = entry["url"]
        domain = entry["domain"]

        print(f"[{i+1}/{len(to_fetch)}] {domain} — {url[:70]}")

        html = fetch_url(url)
        if html is None:
            registry[key]["fetch_status"] = "failed"
            registry[key]["fail_reason"] = "fetch error / timeout"
            fetched_fail += 1
            save_registry(registry)
            time.sleep(INTER_REQUEST_SLEEP)
            continue

        text = extract_text(html)
        if len(text) < MIN_CONTENT_CHARS:
            registry[key]["fetch_status"] = "failed"
            registry[key]["fail_reason"] = f"content too short ({len(text)} chars) — likely paywalled"
            fetched_fail += 1
            save_registry(registry)
            time.sleep(INTER_REQUEST_SLEEP)
            continue

        try:
            n = ingest_page(url, text, entry, vc, pc_index)
        except ValueError as e:
            reason = str(e)
            print(f"  ⚠ INJECTION FILTER: {reason[:120]}")
            registry[key]["fetch_status"] = "rejected"
            registry[key]["fail_reason"] = reason
            injections += 1
            save_registry(registry)
            time.sleep(INTER_REQUEST_SLEEP)
            continue

        registry[key]["fetch_status"] = "fetched"
        registry[key]["fetched_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        registry[key]["vector_count"] = n
        fetched_ok += 1
        total_vectors += n
        save_registry(registry)

        print(f"  ✓ {len(text):,} chars → {n} vectors")
        time.sleep(INTER_REQUEST_SLEEP)

    print(f"\n── Done ─────────────────────────────────────────")
    print(f"  Fetched OK        : {fetched_ok}")
    print(f"  Failed            : {fetched_fail}")
    print(f"  Injection rejected: {injections}")
    print(f"  Vectors added     : {total_vectors}")
    print(f"  Registry          : {len(registry)} total URLs")


if __name__ == "__main__":
    main()
