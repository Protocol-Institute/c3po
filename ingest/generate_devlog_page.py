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

sys.path.insert(0, str(Path(__file__).parent))
from devlog_store import load_devlog

load_dotenv(Path(__file__).parent.parent / ".env")

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


# D1 caps a single SQL statement at 100KB, so the body is written as one INSERT
# plus a series of `body = body || '...'` appends, each statement well under the
# limit. What actually bounds the page is D1's 2MB maximum stored string.
D1_CHUNK_BUDGET_BYTES = 60_000     # escaped body bytes per SQL statement
D1_MAX_BODY_BYTES     = 1_500_000  # hard stop, with headroom under D1's 2MB
WARN_BODY_BYTES       = 400_000    # past this, a reader-facing split is worth considering


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

    return "\n".join(lines)


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

def split_for_sql(body: str, budget_bytes: int) -> list[str]:
    """Split the body so each piece stays under `budget_bytes` once escaped.

    Escaping is per character, so escape(a) + escape(b) == escape(a + b): the
    pieces can be reassembled inside D1 with `||` and come back byte-identical.
    """
    chunks: list[str] = []
    start = 0
    size  = 0
    for i, ch in enumerate(body):
        n = len(sql_escape(ch).encode())
        if size + n > budget_bytes:
            chunks.append(body[start:i])
            start, size = i, 0
        size += n
    chunks.append(body[start:])
    return [c for c in chunks if c]


def build_statements(body_md: str, data: dict) -> list[str]:
    """The row write, as one INSERT plus one append per additional chunk."""
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

    chunks = split_for_sql(body_md, D1_CHUNK_BUDGET_BYTES)

    statements = [f"""INSERT OR REPLACE INTO resources
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
  '{sql_escape(chunks[0])}'
);"""]

    for chunk in chunks[1:]:
        statements.append(
            f"UPDATE resources SET body = body || '{sql_escape(chunk)}' "
            f"WHERE slug = '{sql_escape(SLUG)}';"
        )

    return statements


def run_sql(statements: list[str], local: bool, cf_token: str) -> tuple[bool, str]:
    """Execute statements as a single wrangler file run (one D1 batch)."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sql", delete=False,
                                     prefix="c3po_devlog_") as f:
        f.write("\n".join(statements) + "\n")
        sql_path = f.name

    try:
        cmd = ["npx", "wrangler", "d1", "execute", DB_NAME, "--file", sql_path]
        cmd.append("--local" if local else "--remote")
        env = {**os.environ, "CLOUDFLARE_API_TOKEN": cf_token}
        result = subprocess.run(cmd, cwd=str(WEBSITE_DIR), env=env,
                                capture_output=True, text=True, timeout=180)
        return result.returncode == 0, (result.stderr or result.stdout)[:400]
    finally:
        Path(sql_path).unlink(missing_ok=True)


def read_body_length(local: bool, cf_token: str) -> int | None:
    """Characters currently stored in the page body, or None if unreadable."""
    cmd = ["npx", "wrangler", "d1", "execute", DB_NAME, "--json", "--command",
           f"SELECT length(body) AS n FROM resources WHERE slug = '{sql_escape(SLUG)}';"]
    cmd.append("--local" if local else "--remote")
    env = {**os.environ, "CLOUDFLARE_API_TOKEN": cf_token}
    result = subprocess.run(cmd, cwd=str(WEBSITE_DIR), env=env,
                            capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        return None
    match = re.search(r'"n"\s*:\s*(\d+)', result.stdout)
    return int(match.group(1)) if match else None


def upsert_to_d1(body_md: str, data: dict, local: bool, dry_run: bool) -> bool:
    body_bytes = len(body_md.encode())
    if body_bytes > D1_MAX_BODY_BYTES:
        print(f"ERROR: body is {body_bytes:,} bytes, over the {D1_MAX_BODY_BYTES:,} "
              f"byte ceiling. Note that rolling entries into an archive page does "
              f"not help — archive pages are published too. The published page "
              f"itself has to be split at this point.")
        return False
    if body_bytes > WARN_BODY_BYTES:
        print(f"WARNING: page body is {body_bytes:,} bytes — large enough that a "
              f"reader-facing split is worth considering.")

    statements = build_statements(body_md, data)
    widest = max(len(st.encode()) for st in statements)

    if dry_run:
        print(f"[dry-run] Would write {len(body_md):,} chars to D1 slug={SLUG} "
              f"in {len(statements)} statement(s), widest {widest:,} bytes")
        return True

    cf_token = get_cf_token()
    if not cf_token:
        print("ERROR: CLOUDFLARE_API_TOKEN not found in environment or ../.env.keys")
        return False

    ok, err = run_sql(statements, local, cf_token)
    if not ok:
        print(f"D1 upsert failed\n{err}")
        return False

    # A multi-statement write can in principle land partially; confirm the row
    # holds the whole body before recording the hash, so a short write is
    # retried on the next daemon cycle rather than sitting published.
    stored = read_body_length(local, cf_token)
    if stored is None:
        print(f"D1 upsert reported success but the body length could not be read "
              f"back — not recording the hash, will retry next run.")
        return False
    if stored != len(body_md):
        print(f"D1 upsert incomplete — stored {stored:,} chars, expected "
              f"{len(body_md):,}. Not recording the hash; next run rewrites it.")
        return False

    print(f"D1 upsert OK — slug='{SLUG}', body={len(body_md):,} chars "
          f"in {len(statements)} statement(s), verified")
    return True


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

    data    = load_devlog()
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
