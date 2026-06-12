"""
Generate static SIG meeting archive pages for protocol-institute.org.

Reads pre-built meeting JSON from data/sigs/meetings/*.json
(produced by rebuild_sig_summaries.py) and writes:
  - ../website/sigs/{slug}.html  — individual SIG page
  - ../website/sigs.html         — updated stub with archive links

Run after rebuild_sig_summaries.py (or whenever sigs.html needs updating).

Usage:
    python3 ingest/generate_sig_pages.py
    python3 ingest/generate_sig_pages.py --no-index  # skip sigs.html update
"""

import argparse
import json
from datetime import datetime
from pathlib import Path

MEETINGS_DIR = Path(__file__).parent.parent / "data" / "sigs" / "meetings"
WEBSITE_DIR  = Path(__file__).parent.parent.parent / "website"
SIGS_OUT_DIR = WEBSITE_DIR / "sigs"

GUILD_ID = "1082444651946049567"

SIG_INFO = {
    "SIGFPT": {
        "slug":        "sigfpt",
        "name":        "Formal Protocol Theory",
        "description": "Mathematical and logical modeling of protocols, developing the underlying formal sciences with applications across fields including cryptography, distributed systems, and healthcare.",
        "lead":        "Venkatesh Rao and Patrick Nast",
        "schedule":    "Biweekly Fridays, 10am Pacific",
        "channel_id":  "1327337414175490160",
    },
    "MRG": {
        "slug":        "mrg",
        "name":        "Memory Research Group",
        "description": "Exploring analogies and metaphors for understanding memory and its relationship to protocols, at the intersection of cognitive science, infrastructure, and institutional design.",
        "lead":        "Kei Kreutler",
        "schedule":    "Biweekly Thursdays, 7:30am Pacific",
        "channel_id":  "1379992696114122832",
    },
    "SIGPfB": {
        "slug":        "sigpfb",
        "name":        "Protocols for Business",
        "description": "Business applications of protocols, including AI adoption and organizational coordination, with case studies and essays published in Protocolized.",
        "lead":        "Rafael Fernandez",
        "schedule":    "Biweekly Mondays, 8am Pacific",
        "channel_id":  "1333851496416153702",
    },
    "ProtFiSIG": {
        "slug":        "protfisig",
        "name":        "Protocol Fiction",
        "description": "An emerging genre exploration group developing protocol fiction — primarily for Protocolized magazine — as a mode of inquiry into how protocols shape worlds.",
        "lead":        "Spencer Nitkey and Sachin Benny",
        "schedule":    "Biweekly Thursdays, 8am Pacific",
        "channel_id":  "1106572787042238504",
    },
    "SIGPSY": {
        "slug":        "sigpsy",
        "name":        "SIGPSY — Special Interest Group in Psychohistory",
        "description": "Studying long-range historical modeling and prediction — drawing on quantitative history, complexity science, and protocol theory to develop frameworks for understanding civilizational-scale dynamics. The group maintains worldmachines.org, a collaborative platform for psychohistorical modeling.",
        "lead":        "Venkatesh Rao and Aneesh Sathe",
        "schedule":    "Biweekly Thursdays, 4pm UTC",
        "channel_id":  "1508205168661893180",
    },
    "DRG": {
        "slug":        "drg",
        "name":        "Distributed Robotics Group",
        "description": "The Distributed Robotics Group studies and develops protocols for onchain robotics — examining how decentralized coordination, blockchain infrastructure, and robots and physical AI intersect to create new classes of protocol design challenges. Building one robot is an engineering challenge; getting two or more to coordinate is a protocol problem.",
        "description_extra": "DRG is an applied research group: half its focus is on the protocols themselves, the other half on building the robots to test them. Protocols are engineered arguments, so DRG engineers and tests robot protocols on real robots.",
        "lead":        "Anuraj R. and Rafael Fernandez",
        "schedule":    "Biweekly Thursdays, 3:30pm UTC",
        "channel_id":  "1508175637020676259",
    },
}

SKIP_LINK_DOMAINS = {
    "discord.com", "discordapp.com", "docs.google.com", "drive.google.com",
    "tenor.com", "giphy.com", "calendar.app.google", "calendar.google.com",
    "twitter.com", "x.com",
}


def format_date(date_str: str) -> str:
    if not date_str or date_str == "unknown":
        return ""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        return d.strftime("%B %-d, %Y")
    except Exception:
        return date_str


def is_substantive_link(url: str) -> bool:
    try:
        from urllib.parse import urlparse
        domain = urlparse(url).netloc.lower().removeprefix("www.")
        return domain not in SKIP_LINK_DOMAINS
    except Exception:
        return True


def html_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def nav_html(depth: int = 1) -> str:
    return "  <header id=\"site-header\"></header>"


def footer_html(depth: int = 1) -> str:
    return "  <footer class=\"site-footer\"></footer>"


MEETING_EXTRA_CSS = """  <style>
    .meeting-list { list-style: none; }
    .meeting-item { padding: 2.5rem 0; border-bottom: 1px solid #E0DDD8; }
    .meeting-item:first-child { border-top: 1px solid #E0DDD8; }
    .meeting-date { font-size: 0.8rem; letter-spacing: 0.04em; color: #8A8A8A; margin-bottom: 0.4rem; }
    .meeting-title { font-family: 'Cormorant Garamond', Georgia, serif; font-size: 1.35rem; font-weight: 600; color: #1A1A1A; margin-bottom: 0.5rem; line-height: 1.3; }
    .meeting-topics { display: flex; flex-wrap: wrap; gap: 0.35rem; margin: 0.6rem 0 0.9rem; }
    .meeting-topic { font-size: 0.7rem; font-weight: 500; letter-spacing: 0.06em; text-transform: uppercase; color: #2A6B6B; border: 1px solid #2A6B6B; padding: 0.15em 0.6em; white-space: nowrap; }
    .meeting-summary { color: #3A3A3A; margin: 0.8rem 0; line-height: 1.7; }
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
    .no-meetings { color: #8A8A8A; font-style: italic; padding: 2rem 0; }
  </style>"""


def render_meeting_card(r: dict) -> str:
    date_fmt = format_date(r.get("date", ""))
    title = html_escape(r.get("title", ""))
    summary_paras = r.get("summary", "")
    insights = r.get("key_insights", [])
    topics = r.get("topics", [])
    links = [l for l in r.get("links", []) if is_substantive_link(l)]
    participants = r.get("participants", [])
    discord_url = r.get("discord_url", "")

    parts = ["<li class=\"meeting-item\">"]

    if date_fmt:
        parts.append(f'  <div class="meeting-date">{html_escape(date_fmt)}</div>')

    parts.append(f'  <div class="meeting-title">{title}</div>')

    if topics:
        pills = "".join(f'<span class="meeting-topic">{html_escape(t)}</span>' for t in topics)
        parts.append(f'  <div class="meeting-topics">{pills}</div>')

    if participants:
        parts.append(f'  <p class="meeting-participants">Participants: {html_escape(", ".join(participants[:12]))}</p>')

    if summary_paras:
        for para in summary_paras.strip().split("\n\n"):
            para = para.strip()
            if para:
                parts.append(f'  <p class="meeting-summary">{html_escape(para)}</p>')

    if insights:
        parts.append('  <ul class="meeting-insights">')
        for ins in insights:
            parts.append(f'    <li>{html_escape(ins)}</li>')
        parts.append('  </ul>')

    if links:
        parts.append('  <div class="meeting-links">')
        parts.append('    <div class="meeting-links-label">Links discussed</div>')
        parts.append('    <ul class="meeting-links-list">')
        for link in links[:8]:
            escaped = html_escape(link)
            domain = link.split("/")[2] if "//" in link else link
            parts.append(f'      <li><a href="{escaped}" target="_blank" rel="noopener noreferrer">{html_escape(domain)}</a></li>')
        parts.append('    </ul>')
        parts.append('  </div>')

    if discord_url:
        parts.append(f'  <a href="{html_escape(discord_url)}" class="meeting-discord" target="_blank" rel="noopener noreferrer">View discussion in Discord →</a>')

    parts.append("</li>")
    return "\n".join(parts)


def generate_sig_page(sig_key: str, meetings: list[dict]) -> str:
    info = SIG_INFO[sig_key]
    slug = info["slug"]
    name = info["name"]
    description = info["description"]
    description_extra = info.get("description_extra", "")
    lead = info["lead"]
    schedule = info["schedule"]

    # Sort by date descending (unknown dates go last)
    def sort_key(r):
        d = r.get("date", "")
        return ("0" if not d or d == "unknown" else "1") + d

    meetings_sorted = sorted(meetings, key=sort_key, reverse=True)

    cards = "\n\n".join(render_meeting_card(r) for r in meetings_sorted)
    if not cards:
        cards = '<p class="no-meetings">No meeting records yet.</p>'
    else:
        cards = f'<ul class="meeting-list" role="list">\n{cards}\n</ul>'

    total = len(meetings_sorted)
    dated = sum(1 for m in meetings_sorted if m.get("date") and m["date"] != "unknown")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{html_escape(name)} — The Protocol Institute</title>
  <meta name="description" content="{html_escape(description)}">
  <link rel="icon" href="../assets/logo-static.png" type="image/png">
  <link rel="stylesheet" href="../css/style.css">
{MEETING_EXTRA_CSS}
</head>
<body>

<div class="interior-wrapper">

{nav_html(depth=1)}

  <main class="interior-main">
    <div class="container">

      <div class="page-header">
        <h1>{html_escape(name)}</h1>
      </div>

      <div class="about-body">
        <p>{html_escape(description)}</p>
        {f'<p>{html_escape(description_extra)}</p>' if description_extra else ''}
        <p class="sig-meta">Led by {html_escape(lead)} &mdash; {html_escape(schedule)}</p>
        <p style="margin-top:1rem"><a href="../sigs.html" class="back-link">&#8592; All Special Interest Groups</a></p>
      </div>

      <h2 class="section-label">Meeting Archive &mdash; {total} sessions{f', {dated} dated' if dated < total else ''}</h2>

      {cards}

      <div class="sig-cta">
        <p>Interested in joining? <a href="https://discord.gg/Aj5FbGsNYV" target="_blank" rel="noopener noreferrer">Join the Discord community &#8594;</a></p>
      </div>

    </div>
  </main>

{footer_html(depth=1)}

</div>

<script src="/js/main.js"></script>
</body>
</html>
"""


def load_meetings_by_sig() -> dict[str, list[dict]]:
    by_sig: dict[str, list[dict]] = {k: [] for k in SIG_INFO}
    for f in MEETINGS_DIR.glob("*.json"):
        try:
            r = json.loads(f.read_text())
            sig = r.get("sig", "")
            if sig in by_sig:
                by_sig[sig].append(r)
        except Exception as e:
            print(f"  Warning: could not read {f.name}: {e}")
    return by_sig


def generate_index_page(by_sig: dict[str, list[dict]]) -> str:
    """Return updated sigs.html with archive links added to each SIG item."""
    sigs_html = (WEBSITE_DIR / "sigs.html").read_text()

    # Inject "Meeting archive →" link into each project-item for each SIG
    for sig_key, info in SIG_INFO.items():
        slug = info["slug"]
        count = len(by_sig.get(sig_key, []))
        archive_link = f'\n          <p><a href="sigs/{slug}.html" class="project-link">Meeting archive ({count} sessions) &#8594;</a></p>'

        # Find the sig-meta line for this SIG and insert after it
        # We identify each SIG by its lead name in the sig-meta line
        lead = info["lead"].split(" and ")[0].split(" ")[0]  # first name of first lead
        import re
        # Replace existing archive link if present, else insert after sig-meta
        existing = re.search(
            rf'(<p class="sig-meta">Led by {re.escape(info["lead"])}.*?</p>)\s*(<p><a href="sigs/{slug}\.html"[^<]*</p>)?',
            sigs_html, re.DOTALL
        )
        if existing:
            replacement = existing.group(1) + archive_link
            sigs_html = sigs_html[:existing.start()] + replacement + sigs_html[existing.end():]

    return sigs_html


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-index", action="store_true", help="Skip updating sigs.html")
    args = parser.parse_args()

    SIGS_OUT_DIR.mkdir(parents=True, exist_ok=True)

    by_sig = load_meetings_by_sig()
    total = sum(len(v) for v in by_sig.values())
    print(f"Loaded {total} meeting records across {len([k for k,v in by_sig.items() if v])} SIGs")

    for sig_key, info in SIG_INFO.items():
        meetings = by_sig.get(sig_key, [])
        slug = info["slug"]
        print(f"  Generating {slug} — {len(meetings)} meetings")
        html = generate_sig_page(sig_key, meetings)
        # Write {slug}/index.html (clean URL, absolute paths)
        index_html = html.replace('href="../assets/', 'href="/assets/') \
                         .replace('href="../css/', 'href="/css/') \
                         .replace('href="../js/', 'href="/js/') \
                         .replace('href="../sigs.html"', 'href="/sigs"') \
                         .replace('href="../', 'href="/')
        out_index = SIGS_OUT_DIR / slug / "index.html"
        out_index.parent.mkdir(parents=True, exist_ok=True)
        out_index.write_text(index_html)
        print(f"    → {out_index}")

    sigs_html_path = WEBSITE_DIR / "sigs.html"
    if not args.no_index and sigs_html_path.exists():
        print("\nUpdating sigs.html...")
        updated = generate_index_page(by_sig)
        sigs_html_path.write_text(updated)
        print(f"  → {sigs_html_path}")
    elif not args.no_index:
        print("\nSkipping sigs.html (not found — website uses sigs/index.html)")

    print("\nDone.")


if __name__ == "__main__":
    main()
