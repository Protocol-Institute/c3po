"""
Generate the C3PO Monitoring Dashboard page for protocol-institute.org.

Reads:
  - data/sync_log.json        — run history from all sync scripts
  - data/channel_manifest.json — channel inventory
  - data/discord_links_registry.json — link farming stats
  - data/sigs/meetings/*.json — SIG meeting records
  - Pinecone live stats       — namespace vector counts

Writes:
  - ../website/monitoring.html

Usage:
    python3 ingest/generate_monitoring_page.py
"""

import json
import os
import sys
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

DATA_DIR     = Path(__file__).parent.parent / "data"
CONFIG_DIR   = Path(__file__).parent.parent / "config"
WEBSITE_DIR  = Path(__file__).parent.parent.parent / "website"
LOG_PATH     = DATA_DIR / "sync_log.json"
MANIFEST_PATH = DATA_DIR / "channel_manifest.json"
REGISTRY_PATH = DATA_DIR / "discord_links_registry.json"
BOT_REGISTRY_PATH = CONFIG_DIR / "bot_registry.json"
COST_LOG_PATH    = DATA_DIR / "cost_log.jsonl"
BOT_SESSION_LOG  = Path.home() / "Library" / "Logs" / "c3po" / "bot_sessions.jsonl"
OUT_PATH     = Path(__file__).parent.parent / "monitoring.html"

WORKER_STATS_URL    = "https://c3po.protocolized.io/stats"
WORKER_LAUNCHED     = "2026-05-15"   # Worker first deployed
DISCORD_BOT_STARTED = "2026-05-27"   # Bot went live (session 21)
COST_TRACKING_DATE  = "2026-06-25"   # cost_log.jsonl started recording

# Historical cost estimates (pre-instrumentation, ±40%)
# Major one-time ingest runs that used Claude before cost_logger.py was wired:
#   enrich_discord_links: ~1,400 URLs × $0.001/URL = $1.40
#   rebuild_sig_summaries: ~5 full rebuilds × ~20 summaries × $0.004 = $0.40
#   enrich_pdfs: 82 PDFs × $0.002/PDF = $0.16
#   enrich_substack: 129 posts × $0.002/post = $0.26
#   onboard_channel: ~19 channels × $0.010/channel (Sonnet) = $0.19
#   misc sync_sig / sync_discord_channels pre-tracking: ~$0.10
HISTORICAL_INGEST_ESTIMATE_USD = 2.51

MEETINGS_DIR = DATA_DIR / "sigs" / "meetings"

NAMESPACE_DESCRIPTIONS = {
    "discord_links": "External URLs shared across Discord, harvested & scored",
    "discord":       "General + forum channels; starred msgs weighted 1.0×, unstarred 0.70×",
    "sig":           "SIG Discord messages/summaries + .org meeting pages",
    "videos":        "YouTube talks (PI corpus)",
    "substack":      "Protocolized magazine posts",
    "definitions":   "PI lexicon (914 terms, triage a/b/c)",
    "pdfs":          "Papers/essays from PI corpus",
    "bibliography":  "External works cited by PI corpus",
    "discord_guide": "All active guild channels; Haiku-described; SIG cadence",
    "meta":          "C3PO self-knowledge: devlog sessions",
    "transcripts":   "Bot conversation self-memory: web + Discord Q&A",
}

SIG_DISPLAY = {
    "SIGFPT":    "Formal Protocol Theory",
    "MRG":       "Memory Research Group",
    "SIGPfB":    "Protocols for Business",
    "ProtFiSIG": "Protocol Fiction",
    "SIGPSY":    "Psychohistory (SIGPSY)",
    "DRG":       "Distributed Robotics Group",
}

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


def pinecone_section() -> tuple[str, int]:
    """Query live Pinecone stats. Returns (html, total_vectors)."""
    try:
        from pinecone import Pinecone
        pc  = Pinecone(api_key=os.environ["PINECONE_API_KEY"])
        idx = pc.Index(host=os.environ["PINECONE_C3PO_HOST"])
        stats = idx.describe_index_stats()
        total = stats.total_vector_count
        ns_map = {ns: info.vector_count for ns, info in stats.namespaces.items()}
    except Exception as e:
        return f"    <p class='muted'>Could not fetch Pinecone stats: {he(str(e))}</p>", 0

    rows = []
    for ns in sorted(ns_map, key=lambda n: -ns_map[n]):
        desc = he(NAMESPACE_DESCRIPTIONS.get(ns, ""))
        rows.append(
            f'      <tr><td><code>{he(ns)}</code></td>'
            f'<td class="number">{ns_map[ns]:,}</td>'
            f'<td class="muted-desc">{desc}</td></tr>'
        )
    rows.append(
        f'      <tr class="total-row"><td><strong>Total</strong></td>'
        f'<td class="number"><strong>{total:,}</strong></td><td></td></tr>'
    )
    table = (
        '    <table class="monitor-table">\n'
        '      <thead><tr><th>Namespace</th><th class="number">Vectors</th><th>Description</th></tr></thead>\n'
        '      <tbody>\n'
        + "\n".join(rows)
        + '\n      </tbody>\n    </table>'
    )
    return table, total


def sig_meetings_section(log: dict) -> str:
    """Per-SIG meeting counts, last meeting date, last sync from log."""
    if not MEETINGS_DIR.exists():
        return "    <p class='muted'>No meeting records found.</p>"

    # Count meetings and find last date per SIG
    counts: dict[str, int]  = defaultdict(int)
    last_date: dict[str, str] = {}
    for f in MEETINGS_DIR.glob("*.json"):
        try:
            r   = json.loads(f.read_text())
            sig = r.get("sig", "")
            if sig not in SIG_DISPLAY:
                continue
            counts[sig] += 1
            d = r.get("date", "") or ""
            if d and d != "unknown":
                if d > last_date.get(sig, ""):
                    last_date[sig] = d
        except Exception:
            pass

    # Last SIG sync timestamp from log
    last_sync: dict[str, str] = {}
    for r in log.get("runs", []):
        if r.get("script") == "sync_sig":
            for ch in r.get("channels", []):
                sig = ch.get("sig", "")
                ts  = r.get("ts", "")
                if sig and ts > last_sync.get(sig, ""):
                    last_sync[sig] = ts

    rows = []
    for sig, name in SIG_DISPLAY.items():
        n       = counts.get(sig, 0)
        ld      = last_date.get(sig, "")
        ld_fmt  = fmt_ts(ld + "T00:00:00Z") if ld else '<span class="muted">—</span>'
        ls      = last_sync.get(sig, "")
        ls_fmt  = fmt_ts(ls) if ls else '<span class="muted">—</span>'
        rows.append(
            f'      <tr><td>{he(name)}</td>'
            f'<td class="number">{n}</td>'
            f'<td>{ld_fmt}</td>'
            f'<td>{ls_fmt}</td></tr>'
        )

    return (
        '    <table class="monitor-table">\n'
        '      <thead><tr>'
        '<th>SIG</th><th class="number">Meetings</th>'
        '<th>Last meeting</th><th>Last synced</th>'
        '</tr></thead>\n'
        '      <tbody>\n'
        + "\n".join(rows)
        + '\n      </tbody>\n    </table>'
    )


def _fetch_worker_stats() -> dict | None:
    try:
        req = urllib.request.Request(WORKER_STATS_URL, headers={"User-Agent": "c3po-monitor/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def _ingest_cost_summary() -> dict:
    total = 0.0
    calls = 0
    by_script: dict[str, dict] = defaultdict(lambda: {"cost_usd": 0.0, "calls": 0})
    for rec in _read_jsonl(COST_LOG_PATH):
        c = rec.get("cost_usd", 0.0)
        total += c
        calls += 1
        s = by_script[rec.get("script", "unknown")]
        s["cost_usd"] += c
        s["calls"]    += 1
    return {"total": round(total, 4), "calls": calls, "by_script": dict(by_script)}


def _bot_event_counts() -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for rec in _read_jsonl(BOT_SESSION_LOG):
        counts[rec.get("event", "unknown")] += 1
    return dict(counts)


def cost_section() -> str:
    worker   = _fetch_worker_stats()
    ingest   = _ingest_cost_summary()
    bot_evts = _bot_event_counts()

    worker_ok = worker is not None

    # Worker lifetime totals
    wl_cost = worker.get("lifetime",     {}).get("cost_usd", 0.0) if worker_ok else 0.0
    wl_reqs = worker.get("lifetime",     {}).get("reqs",     0)   if worker_ok else 0
    dl_cost = worker.get("discord_lifetime", {}).get("cost_usd", 0.0) if worker_ok else 0.0
    dl_reqs = worker.get("discord_lifetime", {}).get("reqs",     0)   if worker_ok else 0
    ml_cost = worker.get("mcp_lifetime", {}).get("cost_usd", 0.0) if worker_ok else 0.0
    sess_lt = worker.get("sessions",     {}).get("lifetime",  0)  if worker_ok else 0

    # Web cost = all Worker requests minus Discord minus MCP
    web_cost = max(0.0, wl_cost - dl_cost - ml_cost)
    web_reqs = max(0, wl_reqs - dl_reqs - worker.get("mcp_lifetime", {}).get("reqs", 0)) if worker_ok else 0

    # Discord estimate for pre-tracking period
    # Events logged in bot session log (before Worker discord tracking was added)
    pre_discord_events = (bot_evts.get("conversation", 0) + bot_evts.get("introduction", 0))
    # Rough cost per Discord event: ~$0.018 (short response, system prompt cached)
    PRE_DISCORD_EST_PER_EVENT = 0.018
    discord_pre_est = round(pre_discord_events * PRE_DISCORD_EST_PER_EVENT, 2)

    # Cloudflare flat fee: $5/month; estimate months since launch
    try:
        launched = datetime.strptime(WORKER_LAUNCHED, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        months_live = max(1, round((datetime.now(timezone.utc) - launched).days / 30))
    except Exception:
        months_live = 1
    cf_total = months_live * 5.0

    # Grand total: tracked + CF + historical estimates
    grand_actual  = wl_cost + ingest["total"] + cf_total
    grand_with_est = grand_actual + HISTORICAL_INGEST_ESTIMATE_USD
    # discord pre-estimate is already partially inside wl_cost (pre-tracking discord went to lifetime bucket)
    # so don't double-count

    now_str = datetime.now(timezone.utc).strftime("%-d %b %Y %H:%M UTC")

    def fmt_cost(c: float, bold: bool = False) -> str:
        s = f"${c:.2f}"
        return f"<strong>{s}</strong>" if bold else s

    def fmt_reqs(n: int) -> str:
        return f"{n:,} req{'s' if n != 1 else ''}"

    rows = []

    # Row 1: Web UI
    web_detail = f"{fmt_reqs(web_reqs)}, {sess_lt:,} sessions" if worker_ok else "stats unavailable"
    rows.append(
        f'<tr>'
        f'<td>Web UI serving</td>'
        f'<td><code>c3po_web</code></td>'
        f'<td>Cloudflare Worker — Claude Sonnet + Voyage AI</td>'
        f'<td>Since {WORKER_LAUNCHED}</td>'
        f'<td class="number cost-actual">{fmt_cost(web_cost)}</td>'
        f'<td class="muted-desc">{web_detail}</td>'
        f'</tr>'
    )

    # Row 2: Discord bot (Worker portion)
    disc_detail = (
        f"{fmt_reqs(dl_reqs)} tracked"
        + (f"; est. {pre_discord_events} pre-tracking events ≈ {fmt_cost(discord_pre_est)}" if pre_discord_events else "")
    )
    rows.append(
        f'<tr>'
        f'<td>Discord bot serving</td>'
        f'<td><code>c3po_bot</code></td>'
        f'<td>via Worker — Claude Sonnet + Voyage AI</td>'
        f'<td>Since {COST_TRACKING_DATE}<span class="muted-desc"> (tracking)</span></td>'
        f'<td class="number cost-actual">{fmt_cost(dl_cost)}'
        + (f'<br><span class="cost-est">+{fmt_cost(discord_pre_est)} est.</span>' if pre_discord_events else '')
        + f'</td>'
        f'<td class="muted-desc">{disc_detail}</td>'
        f'</tr>'
    )

    # Row 3: MCP
    if ml_cost > 0 or (worker_ok and worker.get("mcp_lifetime", {}).get("reqs", 0) > 0):
        ml_reqs = worker.get("mcp_lifetime", {}).get("reqs", 0) if worker_ok else 0
        rows.append(
            f'<tr>'
            f'<td>MCP server</td>'
            f'<td><code>c3po_web</code></td>'
            f'<td>via Worker — Claude Sonnet + Voyage AI</td>'
            f'<td>Since {WORKER_LAUNCHED}</td>'
            f'<td class="number cost-actual">{fmt_cost(ml_cost)}</td>'
            f'<td class="muted-desc">{fmt_reqs(ml_reqs)}</td>'
            f'</tr>'
        )

    # Row 4: Ingest pipeline
    ingest_scripts = "; ".join(
        f"{s}: {d['calls']} calls" for s, d in sorted(ingest["by_script"].items())
    ) if ingest["by_script"] else "—"
    rows.append(
        f'<tr>'
        f'<td>Ingest pipeline</td>'
        f'<td><code>c3po_listener</code></td>'
        f'<td>Local scripts — Claude Haiku (link scoring, SIG summaries)</td>'
        f'<td>Since {COST_TRACKING_DATE}</td>'
        f'<td class="number cost-actual">{fmt_cost(ingest["total"])}'
        f'<br><span class="cost-est">+{fmt_cost(HISTORICAL_INGEST_ESTIMATE_USD)} hist. est.</span>'
        f'</td>'
        f'<td class="muted-desc">{ingest["calls"]:,} calls tracked · {he(ingest_scripts)}</td>'
        f'</tr>'
    )

    # Row 5: Cloudflare flat fee
    rows.append(
        f'<tr>'
        f'<td>Infrastructure</td>'
        f'<td>Cloudflare Workers</td>'
        f'<td>$5/mo Workers Paid plan</td>'
        f'<td>Since {WORKER_LAUNCHED} ({months_live} mo)</td>'
        f'<td class="number cost-actual">{fmt_cost(cf_total)}</td>'
        f'<td class="muted-desc">flat fee, not per-request</td>'
        f'</tr>'
    )

    # Total row
    rows.append(
        f'<tr class="total-row">'
        f'<td colspan="3"><strong>Total (tracked)</strong></td>'
        f'<td></td>'
        f'<td class="number">{fmt_cost(grand_actual, bold=True)}</td>'
        f'<td class="muted-desc">+ ~{fmt_cost(HISTORICAL_INGEST_ESTIMATE_USD + discord_pre_est)} estimated pre-tracking</td>'
        f'</tr>'
    )

    rows_html = "\n".join(f"      {r}" for r in rows)
    fetch_note = f'<p class="muted">Worker stats fetched live at {now_str}.</p>' if worker_ok \
                 else '<p class="muted" style="color:#c00">⚠ Worker stats unavailable — showing cached/estimated values only.</p>'

    return f"""    <table class="monitor-table cost-table">
      <thead><tr>
        <th>Component</th><th>Node</th><th>What</th><th>Period</th>
        <th class="number">Cost</th><th>Notes</th>
      </tr></thead>
      <tbody>
{rows_html}
      </tbody>
    </table>
{fetch_note}
    <p class="muted cost-legend">
      <span class="cost-actual-dot"></span> Actual tracked &nbsp;
      <span class="cost-est">+$X est.</span> estimated (pre-tracking, ±40%)
    </p>"""


def last_sync_summary(log: dict) -> str:
    """Most recent run per script — compact status table."""
    runs = log.get("runs", [])
    latest: dict[str, dict] = {}
    for r in runs:
        s = r.get("script", "?")
        if r.get("ts", "") > latest.get(s, {}).get("ts", ""):
            latest[s] = r

    if not latest:
        return "    <p class='muted'>No sync history yet.</p>"

    rows = []
    for script, r in sorted(latest.items(), key=lambda x: -ord(x[0][0])):
        label  = he(SCRIPT_LABELS.get(script, script))
        ts     = fmt_ts(r.get("ts", ""))
        rows.append(f'      <tr><td>{label}</td><td>{he(ts)}</td></tr>')

    return (
        '    <table class="monitor-table narrow">\n'
        '      <thead><tr><th>Script</th><th>Last run</th></tr></thead>\n'
        '      <tbody>\n'
        + "\n".join(rows)
        + '\n      </tbody>\n    </table>'
    )


# ── Page assembly ──────────────────────────────────────────────────────────────

def build_page(log: dict, manifest: dict, registry: dict, bot_registry: dict) -> str:
    runs = log.get("runs", [])
    last_run_ts = runs[-1].get("ts", "") if runs else ""
    generated   = datetime.now(timezone.utc).strftime("%-d %b %Y %H:%M UTC")
    n_channels  = len(manifest.get("channels", {}))
    n_active    = sum(1 for c in manifest.get("channels", {}).values() if c.get("status") == "active")

    pinecone_html, total_vectors = pinecone_section()
    sig_html       = sig_meetings_section(log)
    last_sync_html = last_sync_summary(log)
    cost_html      = cost_section()

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
    .muted-desc {{ color: #888; font-size: 0.82rem; }}
    .total-row td {{ border-top: 2px solid #ccc; padding-top: 0.6rem; }}
    .cost-table th, .cost-table td {{ font-size: 0.85rem; }}
    .cost-actual {{ color: #1a1a1a; }}
    .cost-est {{ color: #888; font-size: 0.78rem; }}
    .cost-actual-dot::before {{ content: "●"; color: #1a1a1a; margin-right: 0.25rem; }}
    .cost-legend {{ font-size: 0.8rem; margin-top: 0.25rem; }}
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
      {('&nbsp;·&nbsp; ' + f'{total_vectors:,} vectors') if total_vectors else ''}
    </p>

    <section class="monitor-section">
      <h2>API Costs</h2>
      <p>Claude Sonnet (serving) and Claude Haiku (ingest) API spend, plus Cloudflare infrastructure. Discord tracking started {COST_TRACKING_DATE}; ingest tracking started {COST_TRACKING_DATE}. Pre-tracking amounts are estimates.</p>
{cost_html}
    </section>

    <section class="monitor-section">
      <h2>Corpus — Pinecone Namespaces</h2>
      <p>Live vector counts across all namespaces in the <code>c3po</code> Pinecone index (PI org account).</p>
{pinecone_html}
    </section>

    <section class="monitor-section">
      <h2>SIG Meetings</h2>
      <p>Meeting records ingested from Discord into <code>data/sigs/meetings/</code>.</p>
{sig_html}
    </section>

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
      <h2>Last Sync per Script</h2>
{last_sync_html}
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
    website_out = WEBSITE_DIR / "monitoring.html"
    website_out.write_text(html, encoding="utf-8")
    runs = len(log.get("runs", []))
    channels = len(manifest.get("channels", {}))
    print(f"✓ monitoring.html written to c3po/ and website/ ({runs} log entries, {channels} channels, {len(registry)} links)")


if __name__ == "__main__":
    main()
