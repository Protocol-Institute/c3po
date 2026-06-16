#!/usr/bin/env python3
"""
C3PO sync daemon.

Runs a full sync cycle on startup, then every INTERVAL seconds.
Designed to be kept alive by launchd (KeepAlive=true, RunAtLoad=true).
If the machine sleeps mid-sleep, the cycle resumes on wake — no missed runs.

Steps each cycle:
  1. sync_discord_channels.py — discover guild structure, auto-describe new channels, embed discord_guide
  2. sync_discord_events.py  — fetch scheduled events, update cadence/next_event_time in registry
  3. sync_discord.py        — general channels (REST poll, no gateway)
  4. fetch_discord_links.py — fetch up to LINK_FETCH_LIMIT pending URLs
  5. enrich_discord_links.py — score/prune with Claude
  6. sync_sig.py            — SIG channels
  7. rebuild_sig_summaries.py — build any new meeting summaries
  8. generate_sig_pages.py  — regenerate SIG HTML pages
  9. generate_monitoring_page.py — rebuild monitoring dashboard
 10. website push           — git commit+push .org website if pages changed
 11. sync_bot_conversations.py — spool → transcripts namespace
 12. sync_web_chats.py         — web KV → transcripts namespace
 13. sync_devlog.py            — devlog sessions → meta namespace
 14. generate_devlog_page.py   — publish devlog to protocolized.io D1
 15. sync_pdf_resources        — sync PDF enrichment to protocolized-website (if enriched_meta changed)
 16. sync_youtube_resources    — sync YouTube enrichment to protocolized-website (if enriched_meta changed)
 17. protocolized-website push — git commit+push if resource Markdown changed (triggers D1 GHA)
 18. publish_dashboard.py      — push corpus stats to c3po.protocolized.io/status

Usage (manual):
    /opt/homebrew/bin/python3 bin/daemon.py

Launchd keeps it alive automatically. Logs to stdout (captured by launchd).
"""

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import session_log as slog

# ── Config ────────────────────────────────────────────────────────────────────

INTERVAL = 30 * 60          # seconds between sync cycles
LINK_FETCH_LIMIT = 200      # URLs per cycle (cap API cost)
STEP_TIMEOUT = 10 * 60      # max seconds per subprocess step

C3PO_DIR         = Path(__file__).resolve().parent.parent
WEBSITE_DIR      = C3PO_DIR.parent / "website"
PROTOCOLIZED_DIR = C3PO_DIR.parent / "protocolized-website"
VENV_PY          = str(C3PO_DIR / ".venv" / "bin" / "python3")
SESSION_LOG      = Path.home() / "Library" / "Logs" / "c3po" / "daemon_sessions.jsonl"
ENRICHMENT_STATE = C3PO_DIR / "data" / "enrichment_sync_state.json"

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("c3po.daemon")

# ── Helpers ───────────────────────────────────────────────────────────────────

def log_cycle(record: dict) -> None:
    slog.append(SESSION_LOG, record)


def run_step(label: str, args: list[str]) -> bool:
    """Run one subprocess step. Returns True on success, False on failure."""
    log.info(f"→ {label}")
    try:
        result = subprocess.run(
            args,
            cwd=str(C3PO_DIR),
            timeout=STEP_TIMEOUT,
        )
        if result.returncode == 0:
            log.info(f"  ✓ {label} done")
            return True
        else:
            log.error(f"  ✗ {label} failed (rc={result.returncode})")
            return False
    except subprocess.TimeoutExpired:
        log.error(f"  ✗ {label} timed out after {STEP_TIMEOUT}s")
        return False
    except Exception as exc:
        log.error(f"  ✗ {label} error: {exc}")
        return False


def get_enrichment_mtime(source: str) -> float | None:
    """Return mtime of enriched_meta.json for the given source, or None if absent."""
    p = C3PO_DIR / "sources" / source / "enriched_meta.json"
    return p.stat().st_mtime if p.exists() else None


def load_enrichment_state() -> dict:
    if ENRICHMENT_STATE.exists():
        try:
            return json.loads(ENRICHMENT_STATE.read_text())
        except Exception:
            return {}
    return {}


def save_enrichment_state(state: dict) -> None:
    ENRICHMENT_STATE.write_text(json.dumps(state, indent=2))


def enrichment_changed(source: str, state: dict) -> bool:
    """Return True if enriched_meta.json is newer than last recorded mtime."""
    mtime = get_enrichment_mtime(source)
    return mtime is not None and state.get(source) != mtime


def push_protocolized_if_changed() -> bool:
    """Git commit + push protocolized-website if resource Markdown files changed."""
    if not PROTOCOLIZED_DIR.exists():
        log.warning("  protocolized-website dir not found — skipping push")
        return False
    check = subprocess.run(
        ["git", "status", "--porcelain", "src/content/resources/"],
        cwd=str(PROTOCOLIZED_DIR),
        capture_output=True,
        text=True,
    )
    if not check.stdout.strip():
        log.info("  Protocolized resources unchanged — skipping push")
        return False
    date_str = datetime.now().strftime("%Y-%m-%d")
    msg = f"Auto: resource enrichment sync from c3po {date_str}"
    try:
        subprocess.run(["git", "add", "src/content/resources/"],
                       cwd=str(PROTOCOLIZED_DIR), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", msg],
                       cwd=str(PROTOCOLIZED_DIR), check=True, capture_output=True)
        subprocess.run(["git", "push"],
                       cwd=str(PROTOCOLIZED_DIR), check=True, capture_output=True)
        log.info("  Protocolized push done")
        return True
    except subprocess.CalledProcessError as exc:
        log.error(f"  Protocolized push failed: {exc.stderr.decode()[:200]}")
        return False


def push_website_if_changed() -> None:
    """Git commit + push website if SIG/monitoring files changed."""
    check = subprocess.run(
        ["git", "status", "--porcelain", "sigs/", "sigs.html", "monitoring.html"],
        cwd=str(WEBSITE_DIR),
        capture_output=True,
        text=True,
    )
    if not check.stdout.strip():
        log.info("  Website unchanged — skipping push")
        return False

    date_str = datetime.now().strftime("%Y-%m-%d")
    msg = f"Auto: SIG pages + monitoring dashboard updated {date_str}"
    try:
        subprocess.run(["git", "add", "sigs/", "sigs.html", "monitoring.html"],
                       cwd=str(WEBSITE_DIR), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", msg],
                       cwd=str(WEBSITE_DIR), check=True, capture_output=True)
        subprocess.run(["git", "push"],
                       cwd=str(WEBSITE_DIR), check=True, capture_output=True)
        log.info("  Website push done")
        return True
    except subprocess.CalledProcessError as exc:
        log.error(f"  Website push failed: {exc.stderr.decode()[:200]}")
        return False


# ── Sync cycle ────────────────────────────────────────────────────────────────

def run_sync(cycle: int) -> None:
    ts_start = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    wall_start = time.monotonic()
    log.info(f"=== Sync cycle {cycle} starting [{ts_start}] ===")

    steps = [
        ("sync_discord_channels",    [VENV_PY, "ingest/sync_discord_channels.py"]),
        ("sync_discord_events",      [VENV_PY, "ingest/sync_discord_events.py"]),
        ("sync_discord",             [VENV_PY, "ingest/sync_discord.py"]),
        ("fetch_discord_links",      [VENV_PY, "ingest/fetch_discord_links.py", "--limit", str(LINK_FETCH_LIMIT)]),
        ("enrich_discord_links",     [VENV_PY, "ingest/enrich_discord_links.py"]),
        ("sync_sig",                 [VENV_PY, "ingest/sync_sig.py"]),
        ("rebuild_sig_summaries",    [VENV_PY, "ingest/rebuild_sig_summaries.py"]),
        ("generate_sig_pages",       [VENV_PY, "ingest/generate_sig_pages.py"]),
        ("generate_monitoring",      [VENV_PY, "ingest/generate_monitoring_page.py"]),
        ("sync_bot_conversations",   [VENV_PY, "ingest/sync_bot_conversations.py"]),
        ("sync_web_chats",           [VENV_PY, "ingest/sync_web_chats.py"]),
        ("sync_devlog",              [VENV_PY, "ingest/sync_devlog.py"]),
        ("generate_devlog_page",     [VENV_PY, "ingest/generate_devlog_page.py"]),
        ("publish_dashboard",        [VENV_PY, "ingest/publish_dashboard.py"]),
    ]

    step_results = {}
    ok = 0
    for label, args in steps:
        success = run_step(label, args)
        step_results[label] = success
        if success:
            ok += 1

    website_pushed = push_website_if_changed()

    # ── Protocolized-website enrichment sync (gated on enriched_meta changes) ──
    enrich_state = load_enrichment_state()
    protocolized_synced = False

    if enrichment_changed("pdfs", enrich_state):
        log.info("→ sync_pdf_resources (enriched_meta changed)")
        sync_script = str(PROTOCOLIZED_DIR / "scripts" / "sync-pdf-resources.py")
        ok_pdf = run_step("sync_pdf_resources", [VENV_PY, sync_script])
        step_results["sync_pdf_resources"] = ok_pdf
        if ok_pdf:
            enrich_state["pdfs"] = get_enrichment_mtime("pdfs")
            save_enrichment_state(enrich_state)
            protocolized_synced = True
            ok += 1

    if enrichment_changed("youtube", enrich_state):
        log.info("→ sync_youtube_resources (enriched_meta changed)")
        sync_script = str(PROTOCOLIZED_DIR / "scripts" / "sync-youtube-resources.py")
        ok_yt = run_step("sync_youtube_resources", [VENV_PY, sync_script, "--no-dates"])
        step_results["sync_youtube_resources"] = ok_yt
        if ok_yt:
            enrich_state["youtube"] = get_enrichment_mtime("youtube")
            save_enrichment_state(enrich_state)
            protocolized_synced = True
            ok += 1

    protocolized_pushed = push_protocolized_if_changed() if protocolized_synced else False

    elapsed = time.monotonic() - wall_start
    ts_end  = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log.info(f"=== Sync cycle {cycle} complete — {ok}/{len(steps)} steps OK ({elapsed:.0f}s) ===")

    log_cycle({
        "event":               "cycle",
        "bot_id":              "c3po_listener",
        "cycle":               cycle,
        "ts_start":            ts_start,
        "ts_end":              ts_end,
        "duration_s":          int(elapsed),
        "steps":               step_results,
        "website_pushed":      website_pushed,
        "protocolized_pushed": protocolized_pushed,
        "ok":                  ok,
        "errors":              len(steps) - ok,
    })


# ── Main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    log.info(f"C3PO sync daemon starting (interval={INTERVAL}s, pid={os.getpid()})")
    log.info(f"  C3PO dir : {C3PO_DIR}")
    log.info(f"  Website  : {WEBSITE_DIR}")
    log.info(f"  Venv     : {VENV_PY}")

    if not Path(VENV_PY).exists():
        log.error(f"Venv python not found: {VENV_PY} — aborting")
        sys.exit(1)

    cycle = 0
    while True:
        cycle += 1
        log.info(f"--- Cycle {cycle} ---")
        try:
            run_sync(cycle)
        except Exception as exc:
            log.error(f"Sync cycle {cycle} crashed: {exc}", exc_info=True)

        log.info(f"Sleeping {INTERVAL}s until next cycle...")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Daemon stopped by keyboard interrupt")
        sys.exit(0)
