"""
Assemble corpus stats + patrol manifest and publish to c3po.protocolized.io/status.

Queries Pinecone live, reads local state files, then PUTs a JSON blob to the
Worker via PUT /api/admin/dashboard. The Worker stores it in KV and serves it
from GET /status.

Usage:
    python3 ingest/publish_dashboard.py
    python3 ingest/publish_dashboard.py --dry-run   # print JSON, don't publish
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent))

DATA_DIR     = Path(__file__).parent.parent / "data"
MEETINGS_DIR = DATA_DIR / "sigs" / "meetings"
LOG_PATH     = DATA_DIR / "sync_log.json"
WORKER_URL   = "https://c3po.protocolized.io"

NAMESPACE_DESCRIPTIONS = {
    "discord_links": "External URLs shared in Discord, fetched and relevance-scored",
    "discord":       "General + forum channel messages",
    "sig":           "SIG discussions, meeting summaries, and .org meeting pages",
    "videos":        "YouTube talk transcripts (PI corpus, 91 videos)",
    "substack":      "Protocolized magazine posts",
    "definitions":   "PI protocol lexicon (914 terms)",
    "pdfs":          "Papers and essays from the PI PDF library",
    "bibliography":  "External works cited by the PI corpus",
    "discord_guide": "Guild channel map (used for navigation queries)",
    "meta":          "C3PO self-knowledge: devlog sessions",
    "transcripts":   "Bot conversation memory: web + Discord Q&A",
}

SIG_DISPLAY = {
    "SIGFPT":    "Formal Protocol Theory",
    "MRG":       "Memory Research Group",
    "SIGPfB":    "Protocols for Business",
    "ProtFiSIG": "Protocol Fiction",
    "SIGPSY":    "Special Interest Group in Psychohistory",
    "DRG":       "Distributed Robotics Group",
}

# Static patrol manifest — each entry describes one ingest source
PATROL_MANIFEST = [
    {
        "source":    "Protocolized Substack",
        "namespace": "substack",
        "targets":   "protocolized.io (all posts)",
        "cadence":   "Daily (GitHub Actions cron)",
        "script":    "sync_substack",
        "status":    "active",
    },
    {
        "source":    "Discord — general channels",
        "namespace": "discord",
        "targets":   "#idle-protocol-musings, #protocol-watch",
        "cadence":   "Every 30 min (daemon)",
        "script":    "sync_discord",
        "status":    "active",
    },
    {
        "source":    "Discord — SIG channels",
        "namespace": "sig",
        "targets":   "SIGFPT, MRG, SIGPfB, ProtFiSIG, SIGPSY, DRG",
        "cadence":   "Every 30 min (daemon)",
        "script":    "sync_sig",
        "status":    "active",
    },
    {
        "source":    "Discord — channel guide",
        "namespace": "discord_guide",
        "targets":   "All active guild channels",
        "cadence":   "Every 30 min (daemon)",
        "script":    "sync_discord_channels",
        "status":    "active",
    },
    {
        "source":    "Community links — fetch",
        "namespace": "discord_links",
        "targets":   "URLs shared across all Discord channels",
        "cadence":   "Every 30 min (daemon)",
        "script":    "fetch_links",
        "status":    "active",
    },
    {
        "source":    "Community links — enrichment",
        "namespace": "discord_links",
        "targets":   "Relevance scoring + pruning via Claude Haiku",
        "cadence":   "Every 30 min (daemon)",
        "script":    "enrich_links",
        "status":    "active",
    },
    {
        "source":    "Bot conversations",
        "namespace": "transcripts",
        "targets":   "Discord bot spool + public web chats",
        "cadence":   "Every 30 min (daemon)",
        "script":    "sync_bot_conversations",
        "status":    "active",
    },
    {
        "source":    "SIG meeting pages",
        "namespace": "sig",
        "targets":   "protocol-institute.org/sigs/ (published meeting pages)",
        "cadence":   "Manual (after website deploy)",
        "script":    "sync_sig_pages",
        "status":    "active",
    },
    {
        "source":    "PDF corpus",
        "namespace": "pdfs",
        "targets":   "protocolized.io resource library (82 papers/essays)",
        "cadence":   "Manual (on new publications)",
        "script":    "ingest_pdfs",
        "status":    "static",
    },
    {
        "source":    "YouTube talks",
        "namespace": "videos",
        "targets":   "PI YouTube channel (91 talks)",
        "cadence":   "Manual (on new uploads)",
        "script":    "ingest_youtube",
        "status":    "static",
    },
    {
        "source":    "Protocol lexicon",
        "namespace": "definitions",
        "targets":   "sources/lexicon_draft.json (914 terms, triage a/b/c)",
        "cadence":   "Manual (on curation updates)",
        "script":    "sync_lexicon",
        "status":    "static",
    },
    {
        "source":    "Devlog",
        "namespace": "meta",
        "targets":   "data/devlog.json (session records)",
        "cadence":   "Per session",
        "script":    "sync_devlog",
        "status":    "active",
    },
]


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def pinecone_stats() -> tuple[int, list[dict]]:
    from pinecone import Pinecone
    pc    = Pinecone(api_key=os.environ["PINECONE_API_KEY"])
    idx   = pc.Index(host=os.environ["PINECONE_C3PO_HOST"])
    stats = idx.describe_index_stats()
    total = stats.total_vector_count
    namespaces = [
        {
            "name":        ns,
            "vectors":     info.vector_count,
            "description": NAMESPACE_DESCRIPTIONS.get(ns, ""),
        }
        for ns, info in sorted(stats.namespaces.items(), key=lambda x: -x[1].vector_count)
    ]
    return total, namespaces


def sig_stats(log: dict) -> list[dict]:
    counts: dict[str, int]  = defaultdict(int)
    last_date: dict[str, str] = {}
    if MEETINGS_DIR.exists():
        for f in MEETINGS_DIR.glob("*.json"):
            try:
                r   = json.loads(f.read_text())
                sig = r.get("sig", "")
                if sig not in SIG_DISPLAY:
                    continue
                counts[sig] += 1
                d = r.get("date", "") or ""
                if d and d != "unknown" and d > last_date.get(sig, ""):
                    last_date[sig] = d
            except Exception:
                pass

    last_sync: dict[str, str] = {}
    for r in log.get("runs", []):
        if r.get("script") == "sync_sig":
            for ch in r.get("channels", []):
                sig = ch.get("sig", "")
                ts  = r.get("ts", "")
                if sig and ts > last_sync.get(sig, ""):
                    last_sync[sig] = ts

    return [
        {
            "key":          sig,
            "name":         name,
            "meetings":     counts.get(sig, 0),
            "last_meeting": last_date.get(sig, ""),
            "last_synced":  last_sync.get(sig, ""),
        }
        for sig, name in SIG_DISPLAY.items()
    ]


def last_sync_per_script(log: dict) -> dict[str, str]:
    latest: dict[str, str] = {}
    for r in log.get("runs", []):
        s  = r.get("script", "")
        ts = r.get("ts", "")
        if s and ts > latest.get(s, ""):
            latest[s] = ts
    return latest


def build_blob() -> dict:
    log                 = load_json(LOG_PATH, {"runs": []})
    total, namespaces   = pinecone_stats()
    sigs                = sig_stats(log)
    last_sync           = last_sync_per_script(log)

    # Attach last_run to each patrol entry
    patrol = []
    for entry in PATROL_MANIFEST:
        e          = dict(entry)
        e["last_run"] = last_sync.get(entry["script"], "")
        patrol.append(e)

    return {
        "generated":  datetime.now(timezone.utc).isoformat(),
        "corpus": {
            "total_vectors": total,
            "namespaces":    namespaces,
        },
        "sigs":    sigs,
        "patrol":  patrol,
    }


def publish(blob: dict) -> None:
    import requests as req_lib
    admin_key = os.environ.get("ADMIN_KEY", "")
    if not admin_key:
        raise RuntimeError("ADMIN_KEY not set in .env")
    resp = req_lib.put(
        f"{WORKER_URL}/api/admin/dashboard",
        json=blob,
        headers={"X-Admin-Key": admin_key},
        timeout=15,
    )
    resp.raise_for_status()
    result = resp.json()
    if not result.get("ok"):
        raise RuntimeError(f"Worker rejected blob: {result}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print("Building dashboard blob...")
    blob = build_blob()
    ns_count = len(blob["corpus"]["namespaces"])
    print(f"  Corpus: {blob['corpus']['total_vectors']:,} vectors across {ns_count} namespaces")
    print(f"  SIGs: {len(blob['sigs'])}")
    print(f"  Patrol entries: {len(blob['patrol'])}")

    if args.dry_run:
        print(json.dumps(blob, indent=2))
        return

    print(f"Publishing to {WORKER_URL}/api/admin/dashboard ...")
    publish(blob)
    print("✓ Dashboard published")


if __name__ == "__main__":
    main()
