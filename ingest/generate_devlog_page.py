"""
Publish the C3PO development log as a resource on protocolized.io.

Renders data/devlog.json to markdown and upserts it into the protocolized-website
D1 database (protocolized-resources) as slug='c3po-devlog'. Idempotent — skips
the D1 write if the rendered content hash hasn't changed.

The rendered markdown uses <a id="session-{id}"> anchors before each session
heading so that Pinecone vector URLs (e.g. #session-21) deep-link correctly.

Usage:
    python3 ingest/generate_devlog_page.py
    python3 ingest/generate_devlog_page.py --dry-run
    python3 ingest/generate_devlog_page.py --local     # write to local D1 dev DB
    python3 ingest/generate_devlog_page.py --force     # re-publish even if unchanged

Requires:
    CLOUDFLARE_API_TOKEN in environment (or in ../.env.keys)
    npx wrangler available in PATH (via protocolized-website node_modules)
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

DEVLOG_PATH    = Path(__file__).parent.parent / "data" / "devlog.json"
STATE_PATH     = Path(__file__).parent.parent / "data" / "devlog_page_state.json"
WEBSITE_DIR    = Path(__file__).parent.parent.parent / "protocolized-website" / "worker"
DB_NAME        = "protocolized-resources"
SLUG           = "c3po-devlog"
PUBLIC_URL     = "https://protocolized.io/resources/c3po-devlog"


# ── HTML → Markdown (reuse devlog_render logic) ───────────────────────────────

def strip_html(html: str) -> str:
    text = re.sub(r'<strong>(.*?)</strong>', r'**\1**', html, flags=re.DOTALL)
    text = re.sub(r'<em>(.*?)</em>', r'*\1*', text, flags=re.DOTALL)
    text = re.sub(r'<code>(.*?)</code>', r'`\1`', text, flags=re.DOTALL)
    text = re.sub(r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', r'[\2](\1)', text, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', '', text)
    return text.strip()


MAX_BODY_CHARS = 90_000  # D1 statement size limit is ~100KB; leave headroom for SQL overhead


def render_markdown(data: dict) -> str:
    lines = []
    lines.append(data["description"])
    lines.append("")

    sessions = sorted(data["sessions"], key=lambda s: s["sort_key"])

    for s in sessions:
        sid   = s.get("id", s.get("sort_key", 0))
        label = s.get("label", "")
        title = s.get("title", "")
        date  = s.get("date", "")
        time_pt = s.get("time_pt", "")
        tracks  = s.get("tracks", [])
        costs   = s.get("costs_usd", {})
        vectors = s.get("vector_counts", {})

        # HTML anchor for deep-linking from Pinecone vector URLs
        lines.append(f'<a id="session-{int(sid)}"></a>')
        lines.append("")

        heading = f"## {label}: {title}" if label else f"## {title}"
        lines.append(heading)
        lines.append("")

        date_line = date
        if time_pt:
            date_line += f" · {time_pt}"
        if date_line:
            lines.append(f"*{date_line}*")
            lines.append("")

        if tracks:
            lines.append(f"**Tracks:** {', '.join(tracks)}")
            lines.append("")

        if costs:
            cost_parts = [f"{k}: ${v:.2f}" for k, v in costs.items()]
            lines.append(f"**Session costs:** {' · '.join(cost_parts)}")
            lines.append("")

        if vectors:
            vec_parts = [f"{ns}: {n:,}" for ns, n in vectors.items()]
            lines.append(f"**Vectors upserted:** {' · '.join(vec_parts)}")
            lines.append("")

        for item in s.get("items", []):
            html = item.get("html", "")
            text = strip_html(html)
            if text:
                lines.append(f"- {text}")
                lines.append("")

        lines.append("---")
        lines.append("")

    body = "\n".join(lines)
    if len(body) > MAX_BODY_CHARS:
        # Keep the most recent sessions — truncate from the front
        body = body[-MAX_BODY_CHARS:]
        # Trim to next session boundary to avoid cutting mid-entry
        cut = body.find("\n## ")
        if cut > 0:
            body = body[cut + 1:]
        body = f"*(Earlier sessions omitted — {len(data['sessions'])} total sessions)*\n\n---\n\n" + body
    return body


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def sql_escape(s: str) -> str:
    return s.replace("'", "''")


# ── CF API token resolution ───────────────────────────────────────────────────

def get_cf_token() -> str:
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    if token:
        return token
    keys_path = Path(__file__).parent.parent.parent / ".env.keys"
    if keys_path.exists():
        for line in keys_path.read_text().splitlines():
            if line.startswith("CLOUDFLARE_API_TOKEN="):
                return line.split("=", 1)[1].strip()
    return ""


# ── D1 upsert ─────────────────────────────────────────────────────────────────

def upsert_to_d1(body_md: str, data: dict, local: bool, dry_run: bool) -> bool:
    sessions = sorted(data["sessions"], key=lambda s: s["sort_key"])
    latest_date = sessions[-1].get("date", "2026-01-01") if sessions else "2026-01-01"
    session_count = len(sessions)

    description = (
        f"How C3PO was built — corpus decisions, architectural choices, and engineering "
        f"notes from {session_count} development sessions. "
        f"Covers ingest pipelines, vector architecture, the Cloudflare Worker API, "
        f"the Discord bot, and the move to PI org infrastructure."
    )

    tags     = '["c3po","infrastructure","rag","ai","technical","devlog"]'
    audience = '["researcher","builder"]'
    authors  = '[{"name":"Protocol Institute"}]'

    sql = f"""INSERT OR REPLACE INTO resources
  (slug, title, type, authors, date, description, tags, audience, featured, url, body)
VALUES (
  '{sql_escape(SLUG)}',
  'C3PO Build Log',
  'devlog',
  '{sql_escape(authors)}',
  '{sql_escape(latest_date)}',
  '{sql_escape(description)}',
  '{sql_escape(tags)}',
  '{sql_escape(audience)}',
  0,
  '{sql_escape(PUBLIC_URL)}',
  '{sql_escape(body_md)}'
);
"""

    if dry_run:
        print(f"[dry-run] Would write {len(body_md):,} chars to D1 slug={SLUG}")
        print(f"[dry-run] SQL first 200 chars: {sql[:200]}")
        return True

    with tempfile.NamedTemporaryFile(mode="w", suffix=".sql", delete=False,
                                     prefix="c3po_devlog_") as f:
        f.write(sql)
        sql_path = f.name

    try:
        cf_token = get_cf_token()
        if not cf_token:
            print("ERROR: CLOUDFLARE_API_TOKEN not found in environment or ../.env.keys")
            return False

        cmd = ["npx", "wrangler", "d1", "execute", DB_NAME, "--file", sql_path]
        if local:
            cmd.append("--local")
        else:
            cmd.append("--remote")

        env = {**os.environ, "CLOUDFLARE_API_TOKEN": cf_token}
        result = subprocess.run(cmd, cwd=str(WEBSITE_DIR), env=env,
                                capture_output=True, text=True, timeout=60)

        if result.returncode == 0:
            print(f"D1 upsert OK — slug='{SLUG}', date={latest_date}, "
                  f"body={len(body_md):,} chars")
            return True
        else:
            print(f"D1 upsert failed (rc={result.returncode})")
            print(result.stderr[:400])
            return False
    finally:
        Path(sql_path).unlink(missing_ok=True)


# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2))


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--local",   action="store_true", help="Write to local D1 dev DB")
    parser.add_argument("--force",   action="store_true", help="Re-publish even if unchanged")
    args = parser.parse_args()

    data    = json.loads(DEVLOG_PATH.read_text())
    body_md = render_markdown(data)
    h       = content_hash(body_md)
    state   = load_state()

    if not args.force and state.get("body_hash") == h:
        print(f"Devlog page unchanged (hash={h}) — skipping D1 write")
        return

    ok = upsert_to_d1(body_md, data, local=args.local, dry_run=args.dry_run)
    if ok and not args.dry_run:
        state["body_hash"] = h
        save_state(state)


if __name__ == "__main__":
    main()
