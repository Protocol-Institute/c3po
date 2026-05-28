#!/usr/bin/env python3
"""
C3PO sync daemon.

Runs a full sync cycle on startup, then every INTERVAL seconds.
Designed to be kept alive by launchd (KeepAlive=true, RunAtLoad=true).
If the machine sleeps mid-sleep, the cycle resumes on wake — no missed runs.

Steps each cycle:
  1. sync_discord.py        — general channels (REST poll, no gateway)
  2. fetch_discord_links.py — fetch up to LINK_FETCH_LIMIT pending URLs
  3. enrich_discord_links.py — score/prune with Claude
  4. sync_sig.py            — SIG channels
  5. rebuild_sig_summaries.py — build any new meeting summaries
  6. generate_sig_pages.py  — regenerate SIG HTML pages
  7. generate_monitoring_page.py — rebuild monitoring dashboard
  8. website push           — git commit+push if pages changed

Usage (manual):
    /opt/homebrew/bin/python3 bin/daemon.py

Launchd keeps it alive automatically. Logs to stdout (captured by launchd).
"""

import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

INTERVAL = 30 * 60          # seconds between sync cycles
LINK_FETCH_LIMIT = 200      # URLs per cycle (cap API cost)
STEP_TIMEOUT = 10 * 60      # max seconds per subprocess step

C3PO_DIR = Path(__file__).resolve().parent.parent
WEBSITE_DIR = C3PO_DIR.parent / "website"
VENV_PY = str(C3PO_DIR / ".venv" / "bin" / "python3")

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("c3po.daemon")

# ── Helpers ───────────────────────────────────────────────────────────────────

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
        return

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
    except subprocess.CalledProcessError as exc:
        log.error(f"  Website push failed: {exc.stderr.decode()[:200]}")


# ── Sync cycle ────────────────────────────────────────────────────────────────

def run_sync() -> None:
    start = time.monotonic()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    log.info(f"=== Sync cycle starting [{now}] ===")

    steps = [
        ("sync_discord",          [VENV_PY, "ingest/sync_discord.py"]),
        ("fetch_discord_links",   [VENV_PY, "ingest/fetch_discord_links.py", "--limit", str(LINK_FETCH_LIMIT)]),
        ("enrich_discord_links",  [VENV_PY, "ingest/enrich_discord_links.py"]),
        ("sync_sig",              [VENV_PY, "ingest/sync_sig.py"]),
        ("rebuild_sig_summaries", [VENV_PY, "ingest/rebuild_sig_summaries.py"]),
        ("generate_sig_pages",    [VENV_PY, "ingest/generate_sig_pages.py"]),
        ("generate_monitoring",   [VENV_PY, "ingest/generate_monitoring_page.py"]),
    ]

    ok = 0
    for label, args in steps:
        if run_step(label, args):
            ok += 1

    push_website_if_changed()

    elapsed = time.monotonic() - start
    log.info(f"=== Sync cycle complete — {ok}/{len(steps)} steps OK ({elapsed:.0f}s) ===")


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
            run_sync()
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
