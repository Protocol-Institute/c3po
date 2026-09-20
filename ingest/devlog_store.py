"""
Paged storage for the C3PO devlog.

`data/devlog.json` is the live page — the one a session appends its entry to.
Older entries are rolled out into `data/devlog_archive_NNN.json` pages by
`bin/devlog_roll.py` so the hand-edited live file stays small.

The split is a storage detail only. Every consumer loads the whole log through
`load_devlog()` and sees one continuous list of sessions, so the published page
at protocolized.io/resources/c3po-devlog has no seam and session anchors
(`#session-{id}`) keep working across a roll.

Usage:
    from devlog_store import load_devlog, save_live
    data = load_devlog()          # merged: archives + live, sorted by sort_key
"""

import json
from pathlib import Path

DATA_DIR      = Path(__file__).parent.parent / "data"
LIVE_PATH     = DATA_DIR / "devlog.json"
ARCHIVE_GLOB  = "devlog_archive_*.json"

# Top-level keys that describe the log itself rather than any one page. The
# live page is authoritative for these; archive copies are for standalone
# readability only.
METADATA_KEYS = ("project", "display_name", "description", "tracks")


# ── Page discovery ────────────────────────────────────────────────────────────

def archive_paths() -> list[Path]:
    """Archive pages, oldest first (zero-padded names sort chronologically)."""
    return sorted(DATA_DIR.glob(ARCHIVE_GLOB))


def next_archive_path() -> Path:
    existing = archive_paths()
    n = len(existing) + 1
    return DATA_DIR / f"devlog_archive_{n:03d}.json"


# ── Load / save ───────────────────────────────────────────────────────────────

def load_live() -> dict:
    return json.loads(LIVE_PATH.read_text())


def load_devlog() -> dict:
    """The complete log: every archive page plus the live page, merged.

    Returns the same shape the single-file devlog always had, so callers need
    no knowledge of paging.
    """
    live = load_live()

    sessions = []
    for path in archive_paths():
        page = json.loads(path.read_text())
        sessions.extend(page.get("sessions", []))
    sessions.extend(live.get("sessions", []))
    sessions.sort(key=lambda s: s["sort_key"])

    merged = {k: live[k] for k in METADATA_KEYS if k in live}
    merged["sessions"] = sessions
    return merged


def _dump(path: Path, data: dict) -> None:
    # indent=2, ensure_ascii=False, no trailing newline — matches how
    # data/devlog.json has always been written, so a roll produces a clean diff.
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def save_live(data: dict) -> None:
    _dump(LIVE_PATH, data)


def save_archive(path: Path, data: dict) -> None:
    _dump(path, data)
