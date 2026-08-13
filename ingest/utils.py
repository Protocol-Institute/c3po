"""Shared utilities: chunking, cleaning, Voyage embedding, Pinecone upsert."""

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterator

import voyageai
from pinecone import Pinecone

VOYAGE_MODEL = "voyage-3"
EMBED_BATCH = 128        # voyage-3 max batch size
PINECONE_BATCH = 100     # upsert batch size
CHUNK_TOKENS = 512
OVERLAP_TOKENS = 64


# ── Ingestion pause / resume (Pinecone quota guard) ─────────────────────────
#
# PINECONE_API_KEY is shared account-wide with the humboldt project (separate
# index, same account/quota) — a write-unit or read-unit 429 here doesn't
# necessarily mean c3po caused it. See Code/warnings-keys.md "Shared Keys =
# Shared Quotas" and status.md session 44/45.
#
# State lives in data/ingestion_pause_state.json, which is committed (not
# gitignored — see .gitignore) so a GitHub-Actions-triggered script (e.g.
# sync_substack.py) sees the same pause state as the local daemon after its
# checkout, not just processes running on this machine.

PAUSE_STATE_PATH = Path(__file__).parent.parent / "data" / "ingestion_pause_state.json"

# Matches Pinecone's monthly-quota 429 messages generically — e.g. "reached
# your write unit limit", "reached your read unit limit", "reached your
# egress limit" — so a not-yet-seen quota dimension (confirmed 2026-08-13:
# "egress limit" wasn't recognized by the old write-unit/read-unit-only
# regexes, so the daemon retried it blind for 3+ days instead of pausing)
# still gets auto-paused instead of silently falling through. Captures the
# quota name so the pause reason and read/write bucketing stay accurate.
_QUOTA_LIMIT_RE = re.compile(r"reached your ([\w\s]+?) limit", re.IGNORECASE)


class IngestionPaused(RuntimeError):
    """Raised by a guarded Pinecone write while ingestion is paused."""


def _load_all_pause_state() -> dict:
    if not PAUSE_STATE_PATH.exists():
        return {}
    try:
        raw = json.loads(PAUSE_STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    # Back-compat: older/first version of this file was a single flat
    # {"kind": "write", ...} record rather than {"write": {...}, "read": {...}}.
    if "kind" in raw:
        return {raw["kind"]: raw}
    return raw


def pause_ingestion(reason: str, resume_at: str, kind: str = "write") -> dict:
    """Pause Pinecone writes (kind='write') or reads (kind='read') until
    resume_at (ISO 8601 UTC, e.g. '2026-08-01T00:00:00Z'). Write and read
    pauses are independent — Pinecone tracks them as separate monthly quotas
    — so pausing one does not affect the other."""
    all_state = _load_all_pause_state()
    entry = {
        "paused": True,
        "kind": kind,
        "reason": reason,
        "paused_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "resume_at": resume_at,
    }
    all_state[kind] = entry
    PAUSE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PAUSE_STATE_PATH.write_text(json.dumps(all_state, indent=2))
    return entry


def resume_ingestion(kind: str | None = None) -> None:
    """Clear the pause state immediately (manual override). kind=None clears
    both write and read pauses; kind='write'/'read' clears just that one."""
    if kind is None:
        if PAUSE_STATE_PATH.exists():
            PAUSE_STATE_PATH.unlink()
        return
    all_state = _load_all_pause_state()
    all_state.pop(kind, None)
    if all_state:
        PAUSE_STATE_PATH.write_text(json.dumps(all_state, indent=2))
    elif PAUSE_STATE_PATH.exists():
        PAUSE_STATE_PATH.unlink()


def pause_status(kind: str = "write") -> dict | None:
    """Current pause state for `kind` ('write' or 'read'), or None if not
    paused / resume_at has passed.

    Does not mutate the file when resume_at has passed — the next attempt
    either succeeds (truly reset) or re-pauses itself via the guarded index
    below, so a stale-but-expired entry is harmless to leave in place and
    doubles as a record of the last incident.
    """
    entry = _load_all_pause_state().get(kind)
    if entry is None:
        return None
    resume_at = entry.get("resume_at", "")
    try:
        resume_dt = datetime.fromisoformat(resume_at.replace("Z", "+00:00"))
    except ValueError:
        return entry  # malformed timestamp — fail safe, treat as still paused
    if datetime.now(timezone.utc) >= resume_dt:
        return None
    return entry


def _default_resume_at() -> str:
    """1st of next UTC month, 02:00 UTC (small buffer for the reset to land).

    If today already is the 1st, a 429 probably means the reset just hasn't
    propagated yet rather than a whole month having passed again — retry in
    a few hours instead of jumping to next month.
    """
    now = datetime.now(timezone.utc)
    if now.day == 1:
        return (now + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
    year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return datetime(year, month, 1, 2, 0, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _GuardedIndex:
    """Wraps a Pinecone Index: blocks upsert/update/delete while a write
    pause is active and query/fetch/list/describe_index_stats while a read
    pause is active (raises IngestionPaused before making the call either
    way), and auto-pauses the moment Pinecone itself returns a write- or
    read-unit 429 — so one script hitting the cap stops every other script
    from repeating the same doomed (and, for embeddings, non-free) call for
    the rest of the outage instead of each discovering it independently.

    Write and read are guarded (and paused) independently — Pinecone tracks
    them as separate monthly quotas, confirmed 2026-07-24: c3po's own
    read-unit quota was found exhausted (rebuild_sig_summaries.py's idx.list()
    429'd) even while only a write pause was active, i.e. reads are NOT
    automatically safe just because writes are paused or vice versa.
    """
    _WRITE_METHODS = {"upsert", "update", "delete"}
    # describe_index_stats is deliberately excluded: confirmed 2026-07-24 it
    # keeps working during a read-unit 429 (it's a metadata/control-plane
    # call, not a per-vector data-plane read) — publish_dashboard.py and the
    # Worker's /health endpoint rely on that staying available.
    _READ_METHODS  = {"query", "fetch", "list"}

    def __init__(self, index):
        self._index = index

    def __getattr__(self, name):
        attr = getattr(self._index, name)
        if name in self._WRITE_METHODS:
            kind = "write"
        elif name in self._READ_METHODS:
            kind = "read"
        else:
            return attr
        if not callable(attr):
            return attr

        def guarded(*args, **kwargs):
            state = pause_status(kind)
            if state is not None:
                raise IngestionPaused(
                    f"Ingestion paused ({state['reason']}) until {state['resume_at']}"
                )
            try:
                return attr(*args, **kwargs)
            except Exception as exc:
                msg = str(exc)
                m = _QUOTA_LIMIT_RE.search(msg)
                if m:
                    quota_name = m.group(1).strip().lower()
                    # "write unit" -> write pause; everything else (read unit,
                    # egress, and any future read-side quota name) -> read
                    # pause, since those are all data-plane read costs.
                    pause_kind = "write" if "write" in quota_name else "read"
                    pause_ingestion(f"Pinecone {quota_name} quota exhausted",
                                     _default_resume_at(), kind=pause_kind)
                raise
        return guarded

# SIG meeting threads are created ahead of the actual session (agenda, reading
# list). Don't treat one as a completed meeting — for ingestion, summary
# building, or website publishing — until this many days after its date have
# passed. Shared by sync_sig.py, rebuild_sig_summaries.py, update_sig_pages.py,
# and generate_sig_pages.py so all four stages agree on "ready".
MEETING_GRACE_DAYS = 7


def meeting_ready(date_str: str, grace_days: int = MEETING_GRACE_DAYS) -> bool:
    """True once `date_str` (YYYY-MM-DD) is at least `grace_days` in the past."""
    if not date_str or date_str == "unknown":
        return False
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return False
    return (datetime.now(timezone.utc).date() - d).days >= grace_days


def get_voyage_client() -> voyageai.Client:
    return voyageai.Client(api_key=os.environ["VOYAGE_API_KEY"])


def get_pinecone_index():
    pc = Pinecone(api_key=os.environ["PINECONE_API_KEY"])
    return _GuardedIndex(pc.Index(host=os.environ["PINECONE_C3PO_HOST"]))


def clean_text(text: str) -> str:
    """Basic text cleaning: normalize whitespace, remove control chars."""
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", text)
    text = re.sub(r" {2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def chunk_text(text: str, chunk_tokens: int = CHUNK_TOKENS,
               overlap_tokens: int = OVERLAP_TOKENS) -> list[str]:
    """
    Naive word-count-based chunking (approximates tokens at ~0.75 words/token).
    For production, replace with a tiktoken or voyage tokenizer.
    """
    words = text.split()
    chunk_words = int(chunk_tokens * 0.75)
    overlap_words = int(overlap_tokens * 0.75)
    step = chunk_words - overlap_words

    chunks = []
    for i in range(0, len(words), step):
        chunk = " ".join(words[i: i + chunk_words])
        if chunk:
            chunks.append(chunk)
        if i + chunk_words >= len(words):
            break
    return chunks


def chunk_id(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:24]


def embed_chunks(texts: list[str], vc: voyageai.Client) -> list[list[float]]:
    """Embed a list of text strings in batches, returning vectors."""
    vectors = []
    for i in range(0, len(texts), EMBED_BATCH):
        batch = texts[i: i + EMBED_BATCH]
        result = vc.embed(batch, model=VOYAGE_MODEL, input_type="document")
        vectors.extend(result.embeddings)
        if i + EMBED_BATCH < len(texts):
            time.sleep(0.5)  # respect rate limits
    return vectors


_LOG_PATH = Path(__file__).parent.parent / "data" / "sync_log.json"
_LOG_RETENTION_DAYS = 90


def append_run_log(entry: dict):
    """Append a run record to data/sync_log.json, pruning entries older than 90 days."""
    try:
        data = json.loads(_LOG_PATH.read_text()) if _LOG_PATH.exists() else {"runs": []}
        cutoff = (datetime.now(timezone.utc) - timedelta(days=_LOG_RETENTION_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        data["runs"] = [r for r in data["runs"] if r.get("ts", "") >= cutoff]
        data["runs"].append(entry)
        _LOG_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"  Warning: failed to write run log: {e}")


def upsert_chunks(chunks: list[str], vectors: list[list[float]],
                  metadata_base: dict, index, namespace: str = "") -> int:
    """Upsert chunks with metadata to Pinecone. Returns count upserted."""
    total = len(chunks)
    records = []
    for i, (chunk, vector) in enumerate(zip(chunks, vectors)):
        meta = {**metadata_base, "chunk_index": i, "chunk_total": total, "text": chunk[:1000]}
        records.append({"id": chunk_id(chunk), "values": vector, "metadata": meta})

    kwargs = {}
    if namespace:
        kwargs["namespace"] = namespace

    for i in range(0, len(records), PINECONE_BATCH):
        index.upsert(vectors=records[i: i + PINECONE_BATCH], **kwargs)

    return len(records)
