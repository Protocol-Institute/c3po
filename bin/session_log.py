"""Shared session-log helper — append JSON records to a .jsonl audit log."""
import json
import logging
from pathlib import Path

_log = logging.getLogger("c3po.session_log")

LOG_DIR = Path.home() / "Library" / "Logs" / "c3po"


def append(log_path: Path, record: dict) -> None:
    """Append one JSON record to a .jsonl session log file."""
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as exc:
        _log.warning(f"session log write failed: {exc}")
