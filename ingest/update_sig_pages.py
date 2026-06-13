"""
Update SIG pages on protocol-institute.org following CONVENTIONS.md.

For each meeting JSON in data/sigs/meetings/ that doesn't yet have a detail page
on the website, this script:
  1. Creates sigs/<slug>/<date-path_slug>/index.html (detail page)
  2. Patches sigs/<slug>/index.html to add a link in the meeting-title for that entry

Detection: skips if ANY directory starting with the date prefix exists (handles
cases where the website agent used a slightly different title slug).

Usage:
    python3 ingest/update_sig_pages.py
    python3 ingest/update_sig_pages.py --dry-run
    python3 ingest/update_sig_pages.py --sig sigfpt
"""

import argparse
import html as htmlmod
import json
import re
from datetime import datetime, timezone
from pathlib import Path

MEETINGS_DIR = Path(__file__).parent.parent / "data" / "sigs" / "meetings"
WEBSITE_DIR  = Path(__file__).parent.parent.parent / "website"
SIGS_DIR     = WEBSITE_DIR / "sigs"

DISCORD_EPOCH = 1420070400000

SIG_INFO = {
    "SIGFPT":    {"slug": "sigfpt",    "name": "Formal Protocol Theory"},
    "MRG":       {"slug": "mrg",       "name": "Memory Research Group"},
    "SIGPfB":    {"slug": "sigpfb",    "name": "Protocols for Business"},
    "ProtFiSIG": {"slug": "protfisig", "name": "Protocol Fiction"},
    "SIGPSY":    {"slug": "sigpsy",    "name": "Special Interest Group in Psychohistory"},
    "DRG":       {"slug": "drg",       "name": "Distributed Robotics Group"},
}

SKIP_LINK_DOMAINS = {
    "discord.com", "discordapp.com", "docs.google.com", "drive.google.com",
    "tenor.com", "giphy.com", "calendar.app.google", "calendar.google.com",
    "twitter.com", "x.com",
}

DETAIL_CSS = """  <style>
    .meeting-list { list-style: none; }
    .meeting-item { padding: 2.5rem 0; border-bottom: 1px solid #E0DDD8; }
    .meeting-item:first-child { border-top: 1px solid #E0DDD8; }
    .meeting-date { font-size: 0.8rem; letter-spacing: 0.04em; color: #8A8A8A; margin-bottom: 0.4rem; }
    .meeting-title { font-family: 'Cormorant Garamond', Georgia, serif; font-size: 1.35rem; font-weight: 600; color: #1A1A1A; margin-bottom: 0.5rem; line-height: 1.3; }
    .meeting-title a { color: inherit; text-decoration: none; }
    .meeting-title a:hover { text-decoration: underline; }
    .meeting-topics { display: flex; flex-wrap: wrap; gap: 0.35rem; margin: 0.6rem 0 0.9rem; }
    .meeting-topic { font-size: 0.7rem; font-weight: 500; letter-spacing: 0.06em; text-transform: uppercase; color: #2A6B6B; border: 1px solid #2A6B6B; padding: 0.15em 0.6em; white-space: nowrap; }
    .meeting-summary { color: #3A3A3A; margin: 0.8rem 0; line-height: 1.7; }
    .meeting-abstract { color: #3A3A3A; margin: 0.5rem 0 0; line-height: 1.65; font-size: 0.9rem; }
    .meeting-insights { margin: 0.8rem 0; padding-left: 1.2rem; }
    .meeting-insights li { color: #3A3A3A; margin-bottom: 0.4rem; font-size: 0.9rem; line-height: 1.6; }
    .meeting-links { margin: 0.8rem 0; }
    .meeting-links-label { font-size: 0.75rem; font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase; color: #8A8A8A; margin-bottom: 0.4rem; }
    .meeting-links-list { list-style: none; padding: 0; }
    .meeting-links-list li { font-size: 0.85rem; margin-bottom: 0.2rem; }
    .meeting-links-list a { color: #2A6B6B; word-break: break-all; }
    .meeting-links-list a:hover { text-decoration: underline; }
    .meeting-participants { font-size: 0.8rem; color: #8A8A8A; margin: 0.5rem 0; }
    .meeting-discord { display: inline-block; margin-top: 0.6rem; font-size: 0.85rem; color: #2A6B6B; }
    .meeting-discord:hover { text-decoration: underline; }
    .section-label { font-family: 'Cormorant Garamond', Georgia, serif; font-size: 1.1rem; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; color: #8A8A8A; margin: 2.5rem 0 0; border-top: 1px solid #E0DDD8; padding-top: 2rem; }
    .back-link { font-size: 0.85rem; color: #2A6B6B; }
    .back-link:hover { text-decoration: underline; }
  </style>"""


def esc(s: str) -> str:
    return htmlmod.escape(str(s))


def title_slug(title: str) -> str:
    t = htmlmod.unescape(title)
    t = re.sub(r'^SIGFPT\s+\w*\s*\d*\s*[:\-–.\|]\s*', '', t, flags=re.I)
    t = re.sub(r'^SIGFPT\s*[:\-–#]?\s*', '', t, flags=re.I)
    t = re.sub(r'^(SigPfB|MRG|ProtfiSIG)\s*[:\-–]\s*', '', t, flags=re.I)
    t = re.sub(r'^PFW Session\s+\d+\s*[:\-–]?\s*', '', t, flags=re.I)
    t = re.sub(r'^(Week|Session)\s+\d+\s*(Discussion\s+Thread)?\s*[:\-–]?\s*', '', t, flags=re.I)
    t = t.strip().lower()
    t = re.sub(r'[^\w\s-]', ' ', t)
    t = re.sub(r'\s+', '-', t.strip())
    t = re.sub(r'-+', '-', t)
    return t[:55].rstrip('-').lstrip('-') or 'session'


def date_from_snowflake(thread_id: str) -> str:
    try:
        ms = (int(thread_id) >> 22) + DISCORD_EPOCH
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime('%Y-%m-%d')
    except Exception:
        return 'unknown'


def format_date(date_str: str) -> str:
    if not date_str or date_str == 'unknown':
        return ''
    try:
        return datetime.strptime(date_str, '%Y-%m-%d').strftime('%B %-d, %Y')
    except Exception:
        return date_str


def is_substantive_link(url: str) -> bool:
    try:
        from urllib.parse import urlparse
        domain = urlparse(url).netloc.lower().removeprefix('www.')
        return domain not in SKIP_LINK_DOMAINS
    except Exception:
        return True


def render_detail_page(r: dict, slug: str, sig_name: str, path_slug: str) -> str:
    title       = r.get('title', '')
    date        = r.get('date', '') or date_from_snowflake(r.get('thread_id', ''))
    date_fmt    = format_date(date)
    topics      = r.get('topics', [])
    summary     = r.get('summary', '')
    insights    = r.get('key_insights', [])
    participants= r.get('participants', [])
    links       = [l for l in r.get('links', []) if is_substantive_link(l)]
    discord_url = r.get('discord_url', '')

    today = datetime.now(tz=timezone.utc).strftime('%Y-%m-%d')
    is_future = date > today if date and date != 'unknown' else False

    topics_html = ''.join(f'<span class="meeting-topic">{esc(t)}</span>' for t in topics)
    topics_block = f'\n        <div class="meeting-topics">{topics_html}</div>' if topics else ''

    participants_block = ''
    if participants:
        participants_block = f'\n        <p class="meeting-participants">Participants: {esc(", ".join(participants[:12]))}</p>'

    if is_future:
        summary_block = '\n        <p class="meeting-summary">Summary to be added after the session.</p>'
        insights_block = ''
    else:
        summary_block = ''
        if summary:
            for para in summary.strip().split('\n\n'):
                para = para.strip()
                if para:
                    summary_block += f'\n        <p class="meeting-summary">{esc(para)}</p>'
        insights_block = ''
        if insights:
            items = ''.join(f'\n<li>{esc(i)}</li>' for i in insights)
            insights_block = f'\n        <ul class="meeting-insights">{items}\n        </ul>'

    links_block = ''
    if links and not is_future:
        items = ''.join(
            f'\n<li><a href="{esc(l)}" rel="noopener noreferrer" target="_blank">'
            f'{esc(l.split("/")[2] if "//" in l else l)}</a></li>'
            for l in links[:8]
        )
        links_block = f'''
        <div class="meeting-links">
<div class="meeting-links-label">Links discussed</div>
<ul class="meeting-links-list">{items}
</ul>
</div>'''

    discord_block = ''
    if discord_url:
        discord_block = f'\n        <a class="meeting-discord" href="{esc(discord_url)}" rel="noopener noreferrer" target="_blank">View discussion in Discord →</a>'

    date_line = f'\n        <p class="meeting-date" style="margin-top:0.25rem;font-size:0.9rem">{esc(date_fmt)}</p>' if date_fmt else ''

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{esc(title)} — {esc(sig_name)}</title>
  <link rel="icon" href="/assets/logo-static.png" type="image/png">
  <link rel="stylesheet" href="/css/style.css">
{DETAIL_CSS}
</head>
<body>

<div class="interior-wrapper">

  <header id="site-header"></header>

  <main class="interior-main">
    <div class="container">

      <div class="about-body">
        <p style="margin-bottom:1.5rem"><a href="/sigs/{slug}" class="back-link">&#8592; {esc(sig_name)}</a></p>
      </div>

      <div class="page-header">{date_line}
        <h1>{esc(title)}</h1>
      </div>

      <div class="meeting-detail">{topics_block}{participants_block}{summary_block}{insights_block}{links_block}{discord_block}
      </div>

    </div>
  </main>

  <footer class="site-footer"></footer>

</div>

<script src="/js/main.js"></script>
</body>
</html>
"""


def patch_index_title_link(index_html: str, title: str, slug: str, path_slug: str) -> tuple[str, bool]:
    """
    Find the meeting-title div for this title and wrap the text in an <a> link.
    Returns (updated_html, changed).
    Only patches if the title is currently unlinked.
    """
    title_esc = esc(title)
    # Already linked? Skip.
    href = f'/sigs/{slug}/{path_slug}'
    if href in index_html:
        return index_html, False

    # Find the exact unlinked div and add a link
    pattern = re.compile(
        r'(<div class="meeting-title">)(' + re.escape(title_esc) + r')(</div>)',
        re.DOTALL
    )
    replacement = rf'\1<a href="{href}">\2</a>\3'
    updated, count = pattern.subn(replacement, index_html)
    return updated, count > 0


def date_prefix_exists(sig_dir: Path, date: str) -> bool:
    """Return True if any subdirectory starting with date already exists."""
    if not date or date == 'unknown':
        return False
    return any(d.name.startswith(date) for d in sig_dir.iterdir() if d.is_dir())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--sig', help='Only process this SIG slug (e.g. sigfpt)')
    args = parser.parse_args()

    created, patched, skipped = [], [], []

    for f in sorted(MEETINGS_DIR.glob('*.json')):
        r = json.loads(f.read_text())
        sig = r.get('sig', '')
        if sig not in SIG_INFO:
            continue
        info = SIG_INFO[sig]
        slug = info['slug']
        sig_name = info['name']

        if args.sig and args.sig != slug:
            continue

        date = r.get('date', '') or ''
        if not date or date == 'unknown':
            date = date_from_snowflake(r.get('thread_id', ''))

        ts       = title_slug(r.get('title', ''))
        path_slug = f'{date}-{ts}' if date and date != 'unknown' else ts
        sig_dir   = SIGS_DIR / slug
        detail_dir = sig_dir / path_slug

        # Skip if a directory with this date prefix already exists (different slug naming)
        if date_prefix_exists(sig_dir, date) and not detail_dir.exists():
            print(f"  SKIP (date exists under different slug) {sig} {date}: {r['title'][:50]}")
            skipped.append(path_slug)
            continue

        if detail_dir.exists():
            skipped.append(path_slug)
            continue

        title = r.get('title', '')
        print(f"  CREATE {slug}/{path_slug}")

        if not args.dry_run:
            detail_dir.mkdir(parents=True, exist_ok=True)
            html = render_detail_page(r, slug, sig_name, path_slug)
            (detail_dir / 'index.html').write_text(html)
        created.append(path_slug)

        # Patch the index to link this meeting-title
        index_path = sig_dir / 'index.html'
        if index_path.exists():
            index_html = index_path.read_text()
            updated, changed = patch_index_title_link(index_html, title, slug, path_slug)
            if changed:
                print(f"  PATCH  {slug}/index.html (link for: {title[:50]})")
                if not args.dry_run:
                    index_path.write_text(updated)
                patched.append(f'{slug}: {title[:50]}')
            else:
                print(f"  NOTE   {slug}/index.html — title not found or already linked: {title[:50]}")

    print(f"\nSummary: {len(created)} created, {len(patched)} index entries linked, {len(skipped)} skipped")


if __name__ == '__main__':
    main()
