"""
Generate the C3PO Monitoring Dashboard page for protocol-institute.org.

Reads:
  - data/sync_log.json        — run history from all sync scripts
  - data/channel_manifest.json — channel inventory
  - data/discord_links_registry.json — link farming stats

Writes:
  - ../website/monitoring.html

Usage:
    python3 ingest/generate_monitoring_page.py
"""

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA_DIR     = Path(__file__).parent.parent / "data"
CONFIG_DIR   = Path(__file__).parent.parent / "config"
WEBSITE_DIR  = Path(__file__).parent.parent.parent / "website"
LOG_PATH     = DATA_DIR / "sync_log.json"
MANIFEST_PATH = DATA_DIR / "channel_manifest.json"
REGISTRY_PATH = DATA_DIR / "discord_links_registry.json"
BOT_REGISTRY_PATH = CONFIG_DIR / "bot_registry.json"
OUT_PATH     = WEBSITE_DIR / "monitoring.html"

SCRIPT_LABELS = {
    "sync_discord":       "Discord harvest",
    "sync_sig":           "SIG harvest",
    "fetch_links":        "Link fetch",
    "fetch_links_youtube":"YouTube transcripts",
    "enrich_links":       "Link enrichment",
}


# ── Data loaders ───────────────────────────────────────────────────────────────

def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return default


def registry_stats(registry: dict) -> dict:
    counts: dict[str, int] = defaultdict(int)
    for entry in registry.values():
        counts[entry.get("fetch_status", "unknown")] += 1
    return dict(counts)


# ── HTML helpers ───────────────────────────────────────────────────────────────

def he(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def fmt_ts(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%-d %b %Y %H:%M UTC")
    except Exception:
        return ts


def nav_html() -> str:
    return """  <header>
    <nav class="site-nav" aria-label="Site navigation">
      <a href="index.html" class="nav-brand"><img src="assets/logo-static.png" alt="" class="nav-logo">The Protocol Institute</a>
      <button class="nav-toggle" id="nav-toggle" aria-controls="nav-links" aria-expanded="false" aria-label="Toggle navigation">&#8801;</button>
      <ul class="nav-links" id="nav-links" role="list">
        <li><a href="projects.html">Initiatives</a></li>
        <li><a href="https://protocolized.io" target="_blank" rel="noopener noreferrer">Magazine</a></li>
        <li><a href="about.html">About</a></li>
        <li><a href="support.html">Support Us</a></li>
        <li><a href="contact.html">Contact</a></li>
      </ul>
    </nav>
  </header>"""


def footer_html() -> str:
    return """  <footer class="site-footer">
    <nav class="footer-nav" aria-label="Footer navigation">
      <a href="team.html">Team</a>
      <a href="network.html">Network</a>
      <a href="consulting.html">Consulting</a>
      <a href="symposium-2026.html">Symposium</a>
      <a href="contact.html">Contact</a>
    </nav>
    <p class="footer-copy">&copy; 2025 The Protocol Institute</p>
  </footer>"""


# ── Section builders ───────────────────────────────────────────────────────────

def channel_table(manifest: dict) -> str:
    channels = manifest.get("channels", {})
    rows = []
    for ch in sorted(channels.values(), key=lambda c: (c.get("status") != "active", c.get("type"), c.get("name", ""))):
        name   = he(ch.get("display", ch.get("name", "?")))
        ctype  = he(ch.get("type", "?"))
        status = ch.get("status", "?")
        added  = he(ch.get("added", ""))
        bf     = "✓" if ch.get("backfill_complete") else "—"
        status_cls = "status-active" if status == "active" else "status-archived"
        rows.append(
            f'      <tr>'
            f'<td>{name}</td>'
            f'<td>{ctype}</td>'
            f'<td><span class="{status_cls}">{status}</span></td>'
            f'<td>{added}</td>'
            f'<td class="center">{bf}</td>'
            f'</tr>'
        )
    return f"""    <table class="monitor-table">
      <thead><tr>
        <th>Channel</th><th>Type</th><th>Status</th><th>Added</th><th>Backfill</th>
      </tr></thead>
      <tbody>
{chr(10).join(rows)}
      </tbody>
    </table>"""


def run_history(log: dict, days: int = 14) -> str:
    runs = log.get("runs", [])
    if not runs:
        return "    <p class='muted'>No run history recorded yet.</p>"

    # Group by date
    by_date: dict[str, list] = defaultdict(list)
    for r in runs:
        date = r.get("ts", "")[:10]
        by_date[date].append(r)

    sorted_dates = sorted(by_date.keys(), reverse=True)[:days]

    blocks = []
    for date in sorted_dates:
        day_runs = sorted(by_date[date], key=lambda r: r.get("ts", ""))
        try:
            label = datetime.strptime(date, "%Y-%m-%d").strftime("%-d %b %Y")
        except Exception:
            label = date

        items = []
        for r in day_runs:
            script = r.get("script", "?")
            slabel = SCRIPT_LABELS.get(script, script)
            ts     = fmt_ts(r.get("ts", ""))

            if script == "sync_discord":
                ch_parts = [
                    f"{he(c['name'])} +{c['new_vectors']}v"
                    for c in r.get("channels", []) if c.get("new_vectors")
                ]
                detail = f"+{r.get('total_new_vectors', 0)} vectors, +{r.get('total_new_urls', 0)} URLs"
                if ch_parts:
                    detail += f" ({', '.join(ch_parts)})"

            elif script == "sync_sig":
                ch_parts = [
                    f"{he(c['sig'])} +{c['new_vectors']}v/{c['new_meetings']}mtg"
                    for c in r.get("channels", []) if c.get("new_vectors") or c.get("new_meetings")
                ]
                detail = f"+{r.get('total_new_vectors', 0)} vectors, {r.get('total_new_meetings', 0)} meetings"
                if ch_parts:
                    detail += f" ({', '.join(ch_parts)})"

            elif script in ("fetch_links", "fetch_links_youtube"):
                detail = (
                    f"{r.get('fetched_ok', 0)} fetched, {r.get('failed', 0)} failed, "
                    f"+{r.get('vectors_added', 0)} vectors"
                )

            elif script == "enrich_links":
                dist = r.get("score_dist", {})
                detail = (
                    f"{r.get('scored', 0)} scored, {r.get('pruned_irrelevant', 0)} pruned — "
                    f"dist {dist}"
                )

            else:
                detail = json.dumps({k: v for k, v in r.items() if k not in ("ts", "script")})

            items.append(f'<li><span class="run-label">{slabel}</span> <span class="run-detail">{he(detail)}</span> <span class="run-ts">{ts}</span></li>')

        blocks.append(
            f'    <div class="run-day">\n'
            f'      <h3 class="run-date">{label}</h3>\n'
            f'      <ul class="run-list">\n'
            + "\n".join(f"        {item}" for item in items)
            + "\n      </ul>\n    </div>"
        )

    return "\n".join(blocks)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass
    return records


def bot_node_status(bot_registry: dict) -> str:
    """Build a bot node status table from session logs."""
    nodes = bot_registry.get("nodes", {})
    if not nodes:
        return "    <p class='muted'>No bot registry found.</p>"

    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")
    rows = []

    for node in nodes.values():
        nid      = node["id"]
        display  = he(node["display"])
        ntype    = he(node["type"])
        slog_raw = node.get("session_log")

        if slog_raw is None:
            # c3po_web — managed by Cloudflare, no local session log
            rows.append(
                f'<tr><td>{display}</td><td>{ntype}</td>'
                f'<td><span class="status-active">managed</span></td>'
                f'<td class="muted">Cloudflare Workers</td>'
                f'<td class="center muted">—</td>'
                f'<td class="center muted">—</td></tr>'
            )
            continue

        slog_path = Path(slog_raw.replace("~", str(Path.home())))
        records   = _read_jsonl(slog_path)

        if not records:
            rows.append(
                f'<tr><td>{display}</td><td>{ntype}</td>'
                f'<td><span class="status-archived">no data</span></td>'
                f'<td class="muted">—</td>'
                f'<td class="center muted">—</td>'
                f'<td class="center muted">—</td></tr>'
            )
            continue

        # Last active timestamp
        ts_field = "ts_start" if nid == "c3po_listener" else "ts"
        last_ts = max((r.get(ts_field, "") for r in records), default="")
        last_label = fmt_ts(last_ts) if last_ts else "—"

        # Is it fresh? (within last 2 hours = listener cycle, within 24h = bot)
        try:
            last_dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
            age_h   = (now - last_dt).total_seconds() / 3600
            if nid == "c3po_listener":
                fresh = age_h < 2
            else:
                fresh = age_h < 24
        except Exception:
            fresh = False
        status_html = '<span class="status-active">running</span>' if fresh else '<span class="status-archived">stale</span>'

        # Counts today
        if nid == "c3po_listener":
            today_cycles = sum(1 for r in records if r.get("event") == "cycle" and r.get("ts_start", "")[:10] == today_str)
            today_label  = f"{today_cycles} cycle{'s' if today_cycles != 1 else ''}"
            vectors_today = sum(
                sum(s.get("vectors_added", 0) for s in r.get("steps_detail", {}).values() if isinstance(s, dict))
                for r in records if r.get("event") == "cycle" and r.get("ts_start", "")[:10] == today_str
            )
            detail = f"+{vectors_today}v" if vectors_today else "—"
        else:
            today_convs = sum(1 for r in records if r.get("event") == "conversation" and r.get("ts", "")[:10] == today_str)
            today_label = f"{today_convs} conv{'s' if today_convs != 1 else ''}"
            detail = "—"

        rows.append(
            f'<tr><td>{display}</td><td>{ntype}</td>'
            f'<td>{status_html}</td>'
            f'<td>{he(last_label)}</td>'
            f'<td class="center">{he(today_label)}</td>'
            f'<td class="center">{he(detail)}</td></tr>'
        )

    rows_html = "\n".join(f"      {r}" for r in rows)
    return f"""    <table class="monitor-table">
      <thead><tr>
        <th>Node</th><th>Type</th><th>Status</th><th>Last active</th><th>Today</th><th>Vectors added</th>
      </tr></thead>
      <tbody>
{rows_html}
      </tbody>
    </table>"""


def link_stats(registry: dict) -> str:
    stats = registry_stats(registry)
    total = sum(stats.values())
    rows = [
        ("Total registered", total),
        ("Fetched OK", stats.get("fetched", 0)),
        ("Enriched — irrelevant (pruned)", stats.get("filtered_irrelevant", 0)),
        ("Failed (paywall / timeout)", stats.get("failed", 0)),
        ("Deferred — YouTube", stats.get("deferred", 0)),
        ("Skipped (internal / GIFs)", stats.get("skipped", 0)),
        ("Rejected (injection filter)", stats.get("rejected", 0)),
    ]
    row_html = "\n".join(
        f'      <tr><td>{he(label)}</td><td class="number">{he(n)}</td></tr>'
        for label, n in rows
    )
    return f"""    <table class="monitor-table narrow">
      <tbody>
{row_html}
      </tbody>
    </table>"""


# ── Page assembly ──────────────────────────────────────────────────────────────

def build_page(log: dict, manifest: dict, registry: dict, bot_registry: dict) -> str:
    runs = log.get("runs", [])
    last_run_ts = runs[-1].get("ts", "") if runs else ""
    generated   = datetime.now(timezone.utc).strftime("%-d %b %Y %H:%M UTC")
    n_channels  = len(manifest.get("channels", {}))
    n_active    = sum(1 for c in manifest.get("channels", {}).values() if c.get("status") == "active")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>C3PO Monitoring Dashboard — Protocol Institute</title>
  <link rel="stylesheet" href="assets/style.css">
  <style>
    .monitor-section {{ margin: 2.5rem 0; }}
    .monitor-table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; margin: 1rem 0; }}
    .monitor-table th, .monitor-table td {{ text-align: left; padding: 0.45rem 0.75rem; border-bottom: 1px solid #e0e0e0; }}
    .monitor-table th {{ background: #f5f5f5; font-weight: 600; }}
    .monitor-table.narrow {{ max-width: 480px; }}
    .monitor-table .center {{ text-align: center; }}
    .monitor-table .number {{ text-align: right; font-variant-numeric: tabular-nums; }}
    .status-active {{ color: #2a7a2a; font-weight: 600; }}
    .status-archived {{ color: #888; }}
    .run-day {{ margin: 1.5rem 0; }}
    .run-date {{ font-size: 0.95rem; font-weight: 700; margin: 0 0 0.4rem; color: #333; }}
    .run-list {{ list-style: none; padding: 0; margin: 0; }}
    .run-list li {{ padding: 0.25rem 0; border-bottom: 1px solid #f0f0f0; font-size: 0.875rem; }}
    .run-label {{ display: inline-block; min-width: 160px; font-weight: 600; color: #444; }}
    .run-detail {{ color: #333; }}
    .run-ts {{ color: #999; font-size: 0.8rem; margin-left: 0.5rem; }}
    .muted {{ color: #888; font-style: italic; }}
    .meta-bar {{ font-size: 0.85rem; color: #666; margin: 0.5rem 0 2rem; }}
  </style>
</head>
<body>
{nav_html()}
  <main class="page-content">
    <h1>C3PO Monitoring Dashboard</h1>
    <p class="meta-bar">
      Generated {generated}
      {('&nbsp;·&nbsp; Last run: ' + he(fmt_ts(last_run_ts))) if last_run_ts else ''}
      &nbsp;·&nbsp; {n_active} active channels / {n_channels} total
    </p>

    <section class="monitor-section">
      <h2>Bot Nodes</h2>
      <p>All C3PO bot processes — listener daemon, Discord gateway, and web interface.</p>
{bot_node_status(bot_registry)}
    </section>

    <section class="monitor-section">
      <h2>Channel Registry</h2>
      <p>All channels in the ingestion manifest. Active channels are polled daily; archived channels were swept once.</p>
{channel_table(manifest)}
    </section>

    <section class="monitor-section">
      <h2>Recent Activity (last 14 days)</h2>
{run_history(log)}
    </section>

    <section class="monitor-section">
      <h2>Link Farming</h2>
      <p>External URLs shared across all Discord channels, harvested into the <code>discord_links</code> corpus namespace.</p>
{link_stats(registry)}
    </section>
  </main>
{footer_html()}
  <script src="assets/nav.js"></script>
</body>
</html>"""


def main():
    log          = load_json(LOG_PATH, {"runs": []})
    manifest     = load_json(MANIFEST_PATH, {"channels": {}})
    registry     = load_json(REGISTRY_PATH, {})
    bot_registry = load_json(BOT_REGISTRY_PATH, {})

    html = build_page(log, manifest, registry, bot_registry)
    WEBSITE_DIR.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(html, encoding="utf-8")
    runs = len(log.get("runs", []))
    channels = len(manifest.get("channels", {}))
    print(f"✓ monitoring.html written ({runs} log entries, {channels} channels, {len(registry)} links)")


if __name__ == "__main__":
    main()
