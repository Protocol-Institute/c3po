"""FIFO welcome queue — persists pending introductions across bot restarts."""
import json
import logging
from pathlib import Path

_log = logging.getLogger("c3po.welcome_queue")

QUEUE_PATH = Path(__file__).resolve().parent.parent / "data" / "welcome_queue.json"
MAX_ATTEMPTS = 3


def _load() -> list[dict]:
    try:
        if QUEUE_PATH.exists():
            return json.loads(QUEUE_PATH.read_text()).get("queue", [])
    except Exception as exc:
        _log.warning(f"Queue load failed: {exc}")
    return []


def _save(items: list[dict]) -> None:
    try:
        QUEUE_PATH.write_text(json.dumps({"queue": items}, indent=2))
    except Exception as exc:
        _log.warning(f"Queue save failed: {exc}")


def push(entry: dict) -> None:
    """Add entry to queue. Idempotent — skips if message_id already present."""
    items = _load()
    if any(i["message_id"] == entry["message_id"] for i in items):
        return
    items.append(entry)
    _save(items)
    _log.info(f"Welcome queued: {entry.get('user_name')} ({entry['message_id']})")


def remove(message_id: str) -> None:
    """Remove a processed entry from the queue."""
    items = [i for i in _load() if i["message_id"] != message_id]
    _save(items)


def pop_all() -> list[dict]:
    """Return all items and clear the queue atomically."""
    items = _load()
    _save([])
    return items


def size() -> int:
    return len(_load())
