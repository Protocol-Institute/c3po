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

# ── 2. SIG channels ───────────────────────────────────────────────────────────
echo "$LOG_PREFIX Running sync_sig.py..."
"$VENV" ingest/sync_sig.py
echo "$LOG_PREFIX sync_sig.py done"

# ── 3. Rebuild SIG meeting summaries (skips already-built) ────────────────────
echo "$LOG_PREFIX Running rebuild_sig_summaries.py..."
"$VENV" ingest/rebuild_sig_summaries.py
echo "$LOG_PREFIX rebuild_sig_summaries.py done"

# ── 4. Regenerate website pages ───────────────────────────────────────────────
echo "$LOG_PREFIX Running generate_sig_pages.py..."
"$VENV" ingest/generate_sig_pages.py
echo "$LOG_PREFIX generate_sig_pages.py done"

# ── 5. Conditional website push ───────────────────────────────────────────────
cd "$WEBSITE_DIR"
if [[ -n "$(git status --porcelain sigs/ sigs.html 2>/dev/null)" ]]; then
    echo "$LOG_PREFIX Website has changes — committing and pushing..."
    git add sigs/ sigs.html
    git commit -m "Auto: SIG pages updated $(date '+%Y-%m-%d')"
    git push
    echo "$LOG_PREFIX Website push done"
else
    echo "$LOG_PREFIX Website unchanged — skipping push"
fi

cd "$C3PO_DIR"
echo "$LOG_PREFIX C3PO daily sync complete"
