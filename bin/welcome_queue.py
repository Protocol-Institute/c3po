"""FIFO welcome queue — persists pending introductions across bot restarts.

Queue file schema:
  {"queue": [...], "watermark": "<ISO timestamp or null>"}

The watermark is the Discord message timestamp of the latest item successfully
drained. The seed only adds messages newer than the watermark, preventing the
system from regressing into arbitrarily old history on each restart.
"""
import json
import logging
from pathlib import Path

_log = logging.getLogger("c3po.welcome_queue")

QUEUE_PATH = Path(__file__).resolve().parent.parent / "data" / "welcome_queue.json"
MAX_ATTEMPTS = 3


def _load() -> dict:
    try:
        if QUEUE_PATH.exists():
            data = json.loads(QUEUE_PATH.read_text())
            return {
                "queue":     data.get("queue", []),
                "watermark": data.get("watermark"),
                "welcomed":  data.get("welcomed", []),
            }
    except Exception as exc:
        _log.warning(f"Queue load failed: {exc}")
    return {"queue": [], "watermark": None, "welcomed": []}


def _save(data: dict) -> None:
    try:
        QUEUE_PATH.write_text(json.dumps(data, indent=2))
    except Exception as exc:
        _log.warning(f"Queue save failed: {exc}")


def get_watermark() -> str | None:
    """Return the current watermark ISO timestamp, or None if unset."""
    return _load().get("watermark")


def set_watermark(ts: str) -> None:
    """Set the watermark if ts is later than the current one."""
    data = _load()
    if not data.get("watermark") or ts > data["watermark"]:
        data["watermark"] = ts
        _save(data)


def advance_watermark(items: list[dict]) -> None:
    """Advance watermark to the latest message_ts among the given items."""
    timestamps = [i.get("message_ts") for i in items if i.get("message_ts")]
    if timestamps:
        set_watermark(max(timestamps))


def push(entry: dict) -> None:
    """Add entry to queue. Idempotent — skips if message_id already present."""
    data = _load()
    if any(i["message_id"] == entry["message_id"] for i in data["queue"]):
        return
    data["queue"].append(entry)
    _save(data)
    _log.info(f"Welcome queued: {entry.get('user_name')} ({entry['message_id']})")


def remove(message_id: str) -> None:
    """Remove a processed entry from the queue."""
    data = _load()
    data["queue"] = [i for i in data["queue"] if i["message_id"] != message_id]
    _save(data)


def pop_all() -> list[dict]:
    """Return all items and clear the queue atomically. Watermark is preserved."""
    data = _load()
    items = data["queue"]
    data["queue"] = []
    _save(data)
    return items


def size() -> int:
    return len(_load()["queue"])


def is_welcomed(user_id: str) -> bool:
    """True if this user has already received a #introductions welcome.

    Prevents re-welcoming a new member on every subsequent top-level message
    they post in the channel (e.g. casual replies to someone else's greeting)
    — a real introduction should only trigger the welcome flow once.
    """
    return user_id in _load().get("welcomed", [])


def mark_welcomed(user_id: str) -> None:
    """Record that this user has received a #introductions welcome (idempotent)."""
    data = _load()
    welcomed = data.setdefault("welcomed", [])
    if user_id not in welcomed:
        welcomed.append(user_id)
        _save(data)
