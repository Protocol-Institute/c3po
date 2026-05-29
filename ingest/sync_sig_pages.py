"""
Ingest Protocol Institute SIG meeting pages from protocol-institute.org.

Fetches the published meeting summaries (title, date, participants, key
insights, topics, resources) and upserts them into the Pinecone `sig`
namespace as chunk_type=sig_meeting_page.  One vector per meeting page.

State file: data/sig_pages_state.json  (url → content_hash)
Pinecone namespace: sig

Usage:
    python3 ingest/sync_sig_pages.py
    python3 ingest/sync_sig_pages.py --dry-run
    python3 ingest/sync_sig_pages.py --sig sigpfb        # single SIG
    python3 ingest/sync_sig_pages.py --force             # re-embed unchanged pages
"""

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

from bs4 import BeautifulSoup
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
from utils import embed_chunks, get_voyage_client, get_pinecone_index, clean_text, chunk_id

load_dotenv(Path(__file__).parent.parent / ".env")

BASE_URL     = "https://protocol-institute.org"
NAMESPACE    = "sig"
STATE_PATH   = Path(__file__).parent.parent / "data" / "sig_pages_state.json"
SLEEP_SECS   = 0.6   # polite crawl delay

SIG_CONFIG = {
    "sigfpt":    {"display": "SIGFPT",    "path": "/sigs/sigfpt/"},
    "mrg":       {"display": "MRG",       "path": "/sigs/mrg/"},
    "sigpfb":    {"display": "SIGPfB",    "path": "/sigs/sigpfb/"},
    "protfisig": {"display": "ProtFiSIG", "path": "/sigs/protfisig/"},
}

MEETING_URL_RE = re.compile(r"^/sigs/[^/]+/\d{4}-\d{2}-\d{2}-")


# ── HTTP ──────────────────────────────────────────────────────────────────────

def fetch_html(url: str) -> str:
    req = Request(url, headers={"User-Agent": "c3po-ingest/1.0 (protocol-institute.org)"})
    with urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ── Discovery ─────────────────────────────────────────────────────────────────

def discover_meeting_urls(sig_key: str) -> list[str]:
    """Return all absolute meeting page URLs for a SIG, in page order."""
    cfg  = SIG_CONFIG[sig_key]
    html = fetch_html(BASE_URL + cfg["path"])
    soup = BeautifulSoup(html, "html.parser")
    seen = set()
    urls = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if MEETING_URL_RE.match(href) and href not in seen:
            seen.add(href)
            urls.append(BASE_URL + href)
    return urls


# ── Parsing ───────────────────────────────────────────────────────────────────

def parse_meeting_page(url: str, html: str, sig_display: str) -> dict | None:
    """Parse a meeting page and return a metadata+text dict, or None on failure.

    Page structure (flat, no section headings):
      <p>  ← back link
      <p>  date
      <h1> title
      <p>  participants
      <p>  summary paragraph(s)
      <ul> key insights (li items, no <a> links)
      <ul> resources    (li items with <a href>)
    """
    soup = BeautifulSoup(html, "html.parser")

    # Title
    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else ""

    # All top-level block elements in document order
    all_p   = soup.find_all("p")
    all_ul  = soup.find_all("ul")

    # Date — second <p> (first is the back-link "← SIG name")
    date_str = ""
    if len(all_p) >= 2:
        candidate = all_p[1].get_text(strip=True)
        if re.match(r"[A-Za-z]+ \d{1,2},?\s+\d{4}", candidate):
            date_str = candidate
    if not date_str:
        # fallback: extract YYYY-MM-DD from URL slug
        dm = re.search(r"/(\d{4}-\d{2}-\d{2})-", url)
        if dm:
            date_str = dm.group(1)
    meeting_date = _normalize_date(date_str)

    # Participants — first <p> starting with "Participants:"
    participants: list[str] = []
    for p in all_p:
        txt = p.get_text(separator=" ", strip=True)
        if re.match(r"participants?:", txt, re.I):
            raw = re.sub(r"^participants?:\s*", "", txt, flags=re.I)
            participants = [n.strip() for n in re.split(r",\s*|\n", raw) if n.strip()]
            break

    # Summary — <p> elements that are not back-link, not date, not participants
    summary_parts = []
    for p in all_p:
        txt = p.get_text(separator=" ", strip=True)
        if not txt:
            continue
        if txt.startswith("←"):
            continue
        if re.match(r"participants?:", txt, re.I):
            continue
        if re.match(r"[A-Za-z]+ \d{1,2},?\s+\d{4}$", txt):
            continue
        if txt.startswith("©"):
            continue
        summary_parts.append(txt)
    summary = clean_text(" ".join(summary_parts))

    # Key insights — ul elements whose li items have no <a href> links
    insights: list[str] = []
    # Resources — ul elements where li items have <a href> links
    resources: list[str] = []
    for ul in all_ul:
        lis = ul.find_all("li")
        has_links = any(li.find("a", href=True) for li in lis)
        if has_links:
            resources = [li.find("a")["href"] for li in lis if li.find("a", href=True)]
        else:
            insights = [li.get_text(separator=" ", strip=True) for li in lis]

    if not title and not summary:
        return None

    # Build embedding text
    parts = [f"{sig_display} — {title}"]
    if meeting_date:
        parts.append(meeting_date)
    if participants:
        parts.append("Participants: " + ", ".join(participants))
    if summary:
        parts.append("\nSummary: " + summary)
    if insights:
        parts.append("\nKey Insights:\n" + "\n".join(f"- {i}" for i in insights))
    if resources:
        parts.append("\nResources: " + ", ".join(resources[:10]))
    text = clean_text("\n".join(parts))

    return {
        "text":          text,
        "meeting_title": title,   # matches normalizeSig convention
        "meeting_date":  meeting_date,
        "participants":  json.dumps(participants),
        "sig_display":   sig_display,
        "url":           url,
        "chunk_type":    "sig_meeting_page",
        "source":        "sig_page",
    }


def _normalize_date(raw: str) -> str:
    """Convert various date strings to YYYY-MM-DD. Returns raw on failure."""
    raw = raw.strip()
    # Already YYYY-MM-DD
    if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
        return raw
    from datetime import datetime
    for fmt in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return raw


# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def save_state(state: dict):
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def content_hash(html: str) -> str:
    return hashlib.sha256(html.encode()).hexdigest()[:16]


# ── Main ──────────────────────────────────────────────────────────────────────

def run(sig_filter: str | None = None, dry_run: bool = False, force: bool = False):
    state  = load_state()
    vc     = None if dry_run else get_voyage_client()
    idx    = None if dry_run else get_pinecone_index()

    sigs = [sig_filter] if sig_filter else list(SIG_CONFIG.keys())
    total_new = total_updated = total_skipped = 0

    for sig_key in sigs:
        cfg = SIG_CONFIG[sig_key]
        print(f"\n── {cfg['display']} ({sig_key}) ──")
        try:
            urls = discover_meeting_urls(sig_key)
        except URLError as e:
            print(f"  Error fetching landing page: {e}")
            continue
        print(f"  Found {len(urls)} meeting URLs")
        time.sleep(SLEEP_SECS)

        for url in urls:
            try:
                html = fetch_html(url)
            except URLError as e:
                print(f"  ERROR fetching {url}: {e}")
                continue
            time.sleep(SLEEP_SECS)

            chash   = content_hash(html)
            prev    = state.get(url, {})
            changed = (prev.get("hash") != chash)

            if not changed and not force:
                total_skipped += 1
                continue

            parsed = parse_meeting_page(url, html, cfg["display"])
            if not parsed:
                print(f"  WARN: parse failed for {url}")
                continue

            status = "NEW" if not prev else "UPDATED"
            print(f"  {status}  {parsed['meeting_date']}  {parsed['meeting_title'][:60]}")

            if not dry_run:
                vectors = embed_chunks([parsed["text"]], vc)
                vec_id  = chunk_id(url)  # stable ID: hash of URL
                meta = {k: v for k, v in parsed.items() if k != "text"}
                meta["text"] = parsed["text"][:1000]
                idx.upsert(
                    vectors=[{"id": vec_id, "values": vectors[0], "metadata": meta}],
                    namespace=NAMESPACE,
                )
                state[url] = {"hash": chash, "date": parsed["meeting_date"], "title": parsed["meeting_title"]}
                save_state(state)

            if not prev:
                total_new += 1
            else:
                total_updated += 1

    print(f"\n── Summary ──")
    print(f"  New:     {total_new}")
    print(f"  Updated: {total_updated}")
    print(f"  Skipped: {total_skipped}")
    if dry_run:
        print("  (dry run — nothing written)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync SIG meeting pages from protocol-institute.org")
    parser.add_argument("--dry-run", action="store_true", help="Parse and report without writing to Pinecone")
    parser.add_argument("--force",   action="store_true", help="Re-embed all pages even if unchanged")
    parser.add_argument("--sig",     choices=list(SIG_CONFIG.keys()), help="Sync only one SIG")
    args = parser.parse_args()
    run(sig_filter=args.sig, dry_run=args.dry_run, force=args.force)
