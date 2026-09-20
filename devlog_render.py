#!/usr/bin/env python3
"""
devlog_render.py — render the devlog to a readable Markdown file.

Reads every page of the log (data/devlog.json plus any data/devlog_archive_*.json)
through devlog_store.load_devlog().

Usage:
    python3 devlog_render.py            # writes DEVLOG.md
    python3 devlog_render.py --stdout   # print to stdout

When the Cloudflare Worker UI is ready, this script can be retired in favor of
a /devlog route rendered from devlog.json at the edge.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "ingest"))
from devlog_store import load_devlog

OUT_PATH = Path("DEVLOG.md")


def strip_html(html: str) -> str:
    """Naive HTML → Markdown approximation for terminal/Markdown rendering."""
    text = re.sub(r'<strong>(.*?)</strong>', r'**\1**', html, flags=re.DOTALL)
    text = re.sub(r'<em>(.*?)</em>', r'*\1*', text, flags=re.DOTALL)
    text = re.sub(r'<code>(.*?)</code>', r'`\1`', text, flags=re.DOTALL)
    text = re.sub(r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', r'[\2](\1)', text, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', '', text)
    return text.strip()


def render(data: dict) -> str:
    lines = []
    lines.append(f"# {data['display_name']}")
    lines.append("")
    lines.append(data["description"])
    lines.append("")
    lines.append("---")
    lines.append("")

    sessions = sorted(data["sessions"], key=lambda s: s["sort_key"])

    for s in sessions:
        label = s.get("label", "")
        title = s.get("title", "")
        date = s.get("date", "")
        time_pt = s.get("time_pt", "")
        tracks = s.get("tracks", [])
        costs = s.get("costs_usd", {})
        vectors = s.get("vector_counts", {})

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


def main():
    data = load_devlog()
    rendered = render(data)

    if "--stdout" in sys.argv:
        print(rendered)
    else:
        OUT_PATH.write_text(rendered)
        print(f"Written to {OUT_PATH}")


if __name__ == "__main__":
    main()
