#!/bin/bash
# C3PO daily sync coordinator
# Runs: discord sync → SIG sync → rebuild summaries → generate pages → conditional website push
# Designed to be called by launchd; also safe to run manually.

set -euo pipefail

C3PO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
WEBSITE_DIR="$(cd "$C3PO_DIR/../website" && pwd)"
VENV="$C3PO_DIR/.venv/bin/python3"
LOG_PREFIX="[$(date '+%Y-%m-%d %H:%M:%S')]"

echo "$LOG_PREFIX C3PO daily sync starting"

# Load env
set -a
source "$C3PO_DIR/.env"
set +a

cd "$C3PO_DIR"

# ── 1. General Discord channels ────────────────────────────────────────────────
echo "$LOG_PREFIX Running sync_discord.py..."
"$VENV" ingest/sync_discord.py
echo "$LOG_PREFIX sync_discord.py done"

# ── 2. Link farming: fetch newly registered URLs (cap 200/day) ────────────────
echo "$LOG_PREFIX Running fetch_discord_links.py..."
"$VENV" ingest/fetch_discord_links.py --limit 200
echo "$LOG_PREFIX fetch_discord_links.py done"

# ── 3. Link enrichment: score newly fetched content with Claude Haiku ─────────
echo "$LOG_PREFIX Running enrich_discord_links.py..."
"$VENV" ingest/enrich_discord_links.py
echo "$LOG_PREFIX enrich_discord_links.py done"

# ── 4. SIG channels ───────────────────────────────────────────────────────────
echo "$LOG_PREFIX Running sync_sig.py..."
"$VENV" ingest/sync_sig.py
echo "$LOG_PREFIX sync_sig.py done"

# ── 5. Rebuild SIG meeting summaries (skips already-built) ────────────────────
echo "$LOG_PREFIX Running rebuild_sig_summaries.py..."
"$VENV" ingest/rebuild_sig_summaries.py
echo "$LOG_PREFIX rebuild_sig_summaries.py done"

# ── 6. Regenerate SIG pages ───────────────────────────────────────────────────
echo "$LOG_PREFIX Running generate_sig_pages.py..."
"$VENV" ingest/generate_sig_pages.py
echo "$LOG_PREFIX generate_sig_pages.py done"

# ── 7. Regenerate monitoring dashboard ────────────────────────────────────────
echo "$LOG_PREFIX Running generate_monitoring_page.py..."
"$VENV" ingest/generate_monitoring_page.py
echo "$LOG_PREFIX generate_monitoring_page.py done"

# ── 8. Conditional website push ───────────────────────────────────────────────
cd "$WEBSITE_DIR"
if [[ -n "$(git status --porcelain sigs/ sigs.html monitoring.html 2>/dev/null)" ]]; then
    echo "$LOG_PREFIX Website has changes — committing and pushing..."
    git add sigs/ sigs.html monitoring.html
    git commit -m "Auto: SIG pages + monitoring dashboard updated $(date '+%Y-%m-%d')"
    git push
    echo "$LOG_PREFIX Website push done"
else
    echo "$LOG_PREFIX Website unchanged — skipping push"
fi

cd "$C3PO_DIR"
echo "$LOG_PREFIX C3PO daily sync complete"
