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
    "videos":        "YouTube talk transcripts",
    "substack":      "Protocolized magazine posts",
    "definitions":   "PI protocol lexicon",
    "pdfs":          "Papers and essays from the PI library",
    "bibliography":  "External works cited by the PI corpus",
    "discord_guide": "Guild channel descriptions (used for navigation queries)",
    "meta":          "C3PO devlog sessions",
    "transcripts":   "Bot conversation memory: web + Discord Q&A",
}

# Which broad category each namespace belongs to.
# Shown as a badge on the status page.
NAMESPACE_TIERS = {
    "substack":      "pi",
    "pdfs":          "pi",
    "videos":        "pi",
    "definitions":   "pi",
    "discord":       "community",
    "sig":           "community",
    "discord_guide": "community",
    "discord_links": "third_party",
    "bibliography":  "third_party",
    "meta":          "system",
    "transcripts":   "system",
}

# Human-readable unit for the artifact count (singular).
ARTIFACT_UNITS = {
    "substack":      "posts",
    "pdfs":          "papers",
    "videos":        "talks",
    "definitions":   "terms",
    "discord":       "channels",
    "sig":           "meetings",
    "discord_guide": "channels",
    "discord_links": "links",
    "bibliography":  "references",
    "meta":          "sessions",
    "transcripts":   "conversations",
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


BASE_DIR = Path(__file__).parent.parent
SOURCES_DIR = BASE_DIR / "sources"
CONFIG_DIR  = BASE_DIR / "config"


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def artifact_counts() -> dict[str, int | None]:
    """Return the number of discrete artifacts per namespace, derived from local files."""
    counts: dict[str, int | None] = {}

    # PI-published
    try:
        api_meta = json.loads((SOURCES_DIR / "substack/api_metadata.json").read_text())
        counts["substack"] = len(api_meta)
    except Exception:
        counts["substack"] = None

    try:
        pdf_meta = json.loads((SOURCES_DIR / "pdfs/enriched_meta.json").read_text())
        counts["pdfs"] = sum(1 for p in pdf_meta.values() if not p.get("deprecated"))
    except Exception:
        counts["pdfs"] = None

    try:
        yt_meta = json.loads((SOURCES_DIR / "youtube/enriched_meta.json").read_text())
        counts["videos"] = len(yt_meta)
    except Exception:
        counts["videos"] = None

    try:
        lexicon = json.loads((SOURCES_DIR / "lexicon_draft.json").read_text())
        counts["definitions"] = len(lexicon)
    except Exception:
        counts["definitions"] = None

    # Third-party
    try:
        reg = json.loads((DATA_DIR / "discord_links_registry.json").read_text())
        counts["discord_links"] = sum(
            1 for e in reg.values() if e.get("relevance_score", -1) >= 1
        )
    except Exception:
        counts["discord_links"] = None

    try:
        bib = json.loads((SOURCES_DIR / "bibliography/sourced_refs.json").read_text())
        counts["bibliography"] = len(bib)
    except Exception:
        counts["bibliography"] = None

    # Community — general Discord channels monitored
    try:
        mf = json.loads((DATA_DIR / "channel_manifest.json").read_text())
        chs = mf.get("channels", {})
        counts["discord"] = sum(
            1 for ch in chs.values()
            if isinstance(ch, dict) and ch.get("type") in ("general", "forum")
        )
    except Exception:
        counts["discord"] = None

    # Community — SIG meetings archived
    try:
        counts["sig"] = len(list(MEETINGS_DIR.glob("*.json"))) if MEETINGS_DIR.exists() else None
    except Exception:
        counts["sig"] = None

    # Community — discord_guide channels described
    try:
        dc = json.loads((CONFIG_DIR / "discord_channels.json").read_text())
        chs = dc.get("channels", {})
        counts["discord_guide"] = len(chs) if isinstance(chs, dict) else None
    except Exception:
        counts["discord_guide"] = None

    # System — meta and transcripts use Pinecone vector count (set later)
    counts["meta"] = None
    counts["transcripts"] = None

    return counts


def pinecone_stats(artifacts: dict[str, int | None]) -> tuple[int, list[dict]]:
    from pinecone import Pinecone
    pc    = Pinecone(api_key=os.environ["PINECONE_API_KEY"])
    idx   = pc.Index(host=os.environ["PINECONE_C3PO_HOST"])
    stats = idx.describe_index_stats()
    total = stats.total_vector_count

    # For system namespaces (meta, transcripts) use vector count as artifact count
    for ns, info in stats.namespaces.items():
        if artifacts.get(ns) is None and NAMESPACE_TIERS.get(ns) == "system":
            artifacts[ns] = info.vector_count

    namespaces = [
        {
            "name":          ns,
            "vectors":       info.vector_count,
            "description":   NAMESPACE_DESCRIPTIONS.get(ns, ""),
            "tier":          NAMESPACE_TIERS.get(ns, ""),
            "artifacts":     artifacts.get(ns),
            "artifact_unit": ARTIFACT_UNITS.get(ns, "items"),
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


def build_breakdown(namespaces: list[dict]) -> list[dict]:
    """Aggregate artifact and vector counts by tier for the summary section."""
    tier_order  = ["pi", "community", "third_party", "system"]
    tier_labels = {
        "pi":          "Protocol Institute",
        "community":   "Community",
        "third_party": "Third-party",
        "system":      "System",
    }
    tier_descs = {
        "pi":          "Content published or curated by the Protocol Institute",
        "community":   "Contributions from PI Discord members",
        "third_party": "External content linked or cited by the community",
        "system":      "Internal metadata and conversation memory",
    }

    by_tier: dict[str, dict] = {t: {"vectors": 0, "items": []} for t in tier_order}
    for ns in namespaces:
        t = ns.get("tier", "")
        if t not in by_tier:
            continue
        by_tier[t]["vectors"] += ns.get("vectors", 0)
        if ns.get("artifacts") is not None:
            by_tier[t]["items"].append({
                "unit":  ns["artifact_unit"],
                "count": ns["artifacts"],
            })

    return [
        {
            "tier":        t,
            "label":       tier_labels[t],
            "description": tier_descs[t],
            "vectors":     by_tier[t]["vectors"],
            "items":       by_tier[t]["items"],
        }
        for t in tier_order
    ]


def build_blob() -> dict:
    log                      = load_json(LOG_PATH, {"runs": []})
    artifacts                = artifact_counts()
    total, namespaces        = pinecone_stats(artifacts)
    sigs                     = sig_stats(log)
    last_sync                = last_sync_per_script(log)
    breakdown                = build_breakdown(namespaces)

    # Attach last_run to each patrol entry
    patrol = []
    for entry in PATROL_MANIFEST:
        e             = dict(entry)
        e["last_run"] = last_sync.get(entry["script"], "")
        patrol.append(e)

    return {
        "generated":  datetime.now(timezone.utc).isoformat(),
        "corpus": {
            "total_vectors": total,
            "namespaces":    namespaces,
        },
        "breakdown": breakdown,
        "sigs":      sigs,
        "patrol":    patrol,
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
