"""
Discord channel structure sync for C3PO.

Calls the Discord REST API each daemon cycle to discover all channels in the PI
guild. For channels not yet in the registry (or whose name/topic has changed),
fetches a sample of recent messages and uses Claude Haiku to auto-write a
guide description + one-liner blurb. Embeds all active channel descriptions
into the `discord_guide` Pinecone namespace.

This is what enables c3po to act as a Discord guide — recommending channels
to new members based on their interests, answering navigation questions, etc.

Registry: config/discord_channels.json (tracked in git; human-editable fields
          like `recommend_to_newcomers`, `tags`, `cadence`, `lead` are preserved
          on update — only auto-generated fields are overwritten).

Re-embedding is driven by content_hash: a channel is re-embedded only when
its name, topic, or description text changes.

Usage:
    python3 ingest/sync_discord_channels.py
    python3 ingest/sync_discord_channels.py --dry-run
    python3 ingest/sync_discord_channels.py --force-reembed
"""

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
from utils import embed_chunks, get_voyage_client, get_pinecone_index, PINECONE_BATCH, append_run_log

load_dotenv(Path(__file__).parent.parent / ".env")

# ── Config ────────────────────────────────────────────────────────────────────

REGISTRY_PATH  = Path(__file__).parent.parent / "config" / "discord_channels.json"
MANIFEST_PATH  = Path(__file__).parent.parent / "data" / "channel_manifest.json"
NAMESPACE      = "discord_guide"
DISCORD_API    = "https://discord.com/api/v10"

GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "1082444651946049567")

# Discord channel types to track (skip CATEGORY=4, VOICE=2, STAGE=13, etc.)
GUIDE_TYPES = {0, 5, 15}  # TEXT, ANNOUNCEMENT, FORUM

DISCORD_TYPE_LABELS = {
    0: "General Discussion", 5: "Announcements", 15: "Forum",
    2: "Voice", 4: "Category", 13: "Stage",
}

SAMPLE_LIMIT = 30  # messages to sample per channel for Haiku description

# Categories where all channels are automatically not recommended to newcomers.
# Individual channels can be overridden by setting recommend_to_newcomers=true manually.
NO_RECOMMEND_CATEGORIES = {
    "archived read only",  # Discord archived-channel category
    "MOD",                 # moderator-only channels
    "Server Link Feed",    # automated bot feeds, not conversational
}

# Channel names that are never recommended regardless of category.
NO_RECOMMEND_NAMES = {
    "moderator-only", "introductions-mod", "kylebot",
    "kit-ops", "application-feedback", "accepted-links", "rejected-links",
    "boost-zone", "🚀-boost-zone",
}

# Human-editable fields that survive auto-update (never overwritten by sync).
# recommend_to_newcomers is NOT preserved — it's recomputed each cycle from
# the rules above, unless the entry has recommend_newcomers_override=true/false.
PRESERVED_FIELDS = {
    "recommend_newcomers_override", "tags", "cadence", "lead",
    "sig_display", "description", "guide_blurb",
}


def should_recommend(name: str, category: str, existing: dict | None) -> bool:
    """Determine recommend_to_newcomers, respecting manual overrides."""
    if existing and "recommend_newcomers_override" in existing:
        return existing["recommend_newcomers_override"]
    if category in NO_RECOMMEND_CATEGORIES:
        return False
    if name in NO_RECOMMEND_NAMES:
        return False
    return True


# ── Discord REST ───────────────────────────────────────────────────────────────

def discord_get(path: str) -> dict | list:
    token = os.environ["DISCORD_BOT_TOKEN"]
    req = urllib.request.Request(
        f"{DISCORD_API}{path}",
        headers={"Authorization": f"Bot {token}", "User-Agent": "C3PO-Sync/1.0"},
    )
    while True:
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                body = json.loads(e.read())
                time.sleep(float(body.get("retry_after", 1)) + 0.1)
            else:
                raise


def fetch_guild_channels() -> list[dict]:
    return discord_get(f"/guilds/{GUILD_ID}/channels")


def fetch_recent_messages(channel_id: str, limit: int = SAMPLE_LIMIT) -> list[dict]:
    try:
        data = discord_get(f"/channels/{channel_id}/messages?limit={min(limit, 100)}")
        data.sort(key=lambda m: int(m["id"]))
        return data
    except Exception:
        return []


# ── Registry ───────────────────────────────────────────────────────────────────

def load_registry() -> dict:
    if REGISTRY_PATH.exists():
        return json.loads(REGISTRY_PATH.read_text())
    return {"version": 1, "guild_id": GUILD_ID, "channels": {}}


def load_manifest_sig_displays() -> dict[str, str]:
    """Return {channel_id: sig_display} for all sig-type channels in channel_manifest.json."""
    try:
        m = json.loads(MANIFEST_PATH.read_text())
        return {
            cid: ch["sig_display"]
            for cid, ch in m.get("channels", {}).items()
            if ch.get("type") == "sig" and ch.get("sig_display")
        }
    except Exception:
        return {}


def save_registry(reg: dict) -> None:
    reg["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    REGISTRY_PATH.write_text(json.dumps(reg, indent=2, ensure_ascii=False))


def content_hash(path: str, topic: str, description: str, cadence: str = "") -> str:
    s = f"{path}|{topic}|{description}|{cadence}"
    return hashlib.sha256(s.encode()).hexdigest()[:16]


# ── Haiku description generation ───────────────────────────────────────────────

DESCRIBE_PROMPT = """\
You are writing a guide entry for the Protocol Institute Discord server.

The Discord is organized into named categories (sections), each containing related channels.
The category name and channel name together are the primary navigation aids — a new member
reads them to decide where to go.

CHANNEL LOCATION
Category (section): {category}
Channel: #{name}
Full path: {path}
Discord topic: {topic}
Channel type: {type_label}

RECENT MESSAGES SAMPLE ({msg_count} messages, oldest first):
{messages}

Write a guide entry for a new member deciding whether to follow or post in this channel.
- Reference the category to explain where this channel sits in the community structure.
- Be specific about content type, audience, and intellectual vibe.
- The guide_blurb must make sense standalone (a reader may only see the blurb, not the path).

Output ONLY valid JSON (no markdown):
{{
  "description": "<2-3 sentences — category context, purpose, audience, content style>",
  "guide_blurb": "<one sentence, max 90 chars — for inline recommendations>"
}}"""


def describe_channel(channel: dict, category: str, path: str, messages: list[dict]) -> dict:
    """Call Haiku to generate description + guide_blurb for a channel."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    def fmt(m: dict) -> str:
        ts_ms = (int(m["id"]) >> 22) + 1420070400000
        d = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        author = m.get("author", {}).get("username", "?")
        text = (m.get("content", "") or "")[:150].replace("\n", " ")
        return f"  [{d}] @{author}: {text}"

    prompt = DESCRIBE_PROMPT.format(
        name=channel.get("name", ""),
        category=category or "Unknown",
        path=path,
        topic=channel.get("topic") or "(none)",
        type_label=DISCORD_TYPE_LABELS.get(channel.get("type", 0), "Text Channel"),
        msg_count=len(messages),
        messages="\n".join(fmt(m) for m in messages) or "  (no recent messages)",
    )

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text.strip()
    # Strip markdown fences if Haiku adds them
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())


# ── Pinecone embedding ─────────────────────────────────────────────────────────

def embed_channel(entry: dict, vc, index) -> None:
    """Embed a single channel entry into the discord_guide namespace."""
    path       = entry.get("path", entry["display"])
    cadence_line   = (f"Meets: {entry['cadence']}\n"           if entry.get("cadence")         else "")
    next_line      = (f"Next session: {entry['next_event_time']}\n" if entry.get("next_event_time") else "")
    lead_line      = (f"Lead: {entry['lead']}\n"               if entry.get("lead")            else "")
    text = (
        f"Discord location: {path}\n"
        f"Channel: {entry['display']}  |  Section: {entry.get('category', '')}\n"
        f"Type: {entry.get('type_label', '')}\n"
        f"{cadence_line}{next_line}{lead_line}"
        f"Topic: {entry.get('topic') or '(none)'}\n\n"
        f"{entry.get('description', '')}"
    )
    vectors = embed_chunks([text], vc)
    meta = {
        "chunk_type":              "discord_channel",
        "channel_id":              entry["id"],
        "name":                    entry["name"],
        "display":                 entry["display"],
        "path":                    path,
        "type_label":              entry.get("type_label", ""),
        "category":                entry.get("category", ""),
        "guide_blurb":             entry.get("guide_blurb", ""),
        "sig_display":             entry.get("sig_display", ""),
        "cadence":                 entry.get("cadence", ""),
        "next_event_time":         entry.get("next_event_time", ""),
        "next_event_iso":          entry.get("next_event_iso", ""),
        "lead":                    entry.get("lead", ""),
        "recommend_to_newcomers":  entry.get("recommend_to_newcomers", True),
        "status":                  entry.get("status", "active"),
        "text":                    text[:1000],
    }
    record = {"id": f"discord_channel__{entry['id']}", "values": vectors[0], "metadata": meta}
    index.upsert(vectors=[record], namespace=NAMESPACE)


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run",      action="store_true")
    parser.add_argument("--force-reembed", action="store_true",
                        help="Re-embed all active channels even if unchanged")
    args = parser.parse_args()

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f"Fetching guild channels for guild {GUILD_ID}…")
    all_channels = fetch_guild_channels()

    # Build category map: id → name
    categories = {c["id"]: c["name"] for c in all_channels if c.get("type") == 4}

    # Only process text/announcement/forum channels
    channels = [c for c in all_channels if c.get("type") in GUIDE_TYPES]
    api_ids = {c["id"] for c in channels}
    print(f"  Found {len(channels)} guide-eligible channels "
          f"({len(all_channels)} total, {len(categories)} categories)")

    registry = load_registry()
    known = registry.setdefault("channels", {})
    manifest_sig_displays = load_manifest_sig_displays()

    new_count = changed_count = archived_count = embed_count = 0

    # ── Detect and describe new / changed channels ─────────────────────────────
    for ch in channels:
        cid        = ch["id"]
        name       = ch["name"]
        topic      = ch.get("topic") or ""
        cat_id     = ch.get("parent_id", "")
        category   = categories.get(cat_id, "")
        type_label = DISCORD_TYPE_LABELS.get(ch.get("type", 0), "Text Channel")
        path       = f"{category} > #{name}" if category else f"#{name}"

        existing = known.get(cid)

        # Determine if description needs (re)generation.
        # Generate when: new channel, or name/topic/category changed (auto-sourced only).
        need_describe = False
        if existing is None:
            need_describe = True
            new_count += 1
            print(f"  NEW  {path}")
        elif (existing.get("name") != name
              or existing.get("topic", "") != topic
              or existing.get("category", "") != category):
            if existing.get("source", "auto") == "auto":
                need_describe = True
                changed_count += 1
                print(f"  CHG  {path} (name/topic/category changed)")

        if need_describe and not args.dry_run:
            messages = fetch_recent_messages(cid, SAMPLE_LIMIT)
            try:
                generated = describe_channel(ch, category, path, messages)
                description = generated.get("description", "")
                guide_blurb = generated.get("guide_blurb", "")
            except Exception as exc:
                print(f"    ⚠ Haiku failed for {path}: {exc}")
                description = topic  # fall back to raw topic
                guide_blurb = f"#{name} — {type_label.lower()} channel."
            time.sleep(0.3)
        elif existing:
            description = existing.get("description", topic)
            guide_blurb = existing.get("guide_blurb", "")
        else:
            description = topic
            guide_blurb = ""

        # Build updated entry, preserving any human-edited fields
        entry = {
            "id":                     cid,
            "name":                   name,
            "display":                f"#{name}",
            "path":                   path,
            "discord_type":           ch.get("type", 0),
            "type_label":             type_label,
            "category":               category,
            "category_id":            cat_id,
            "topic":                  topic,
            "description":            description,
            "guide_blurb":            guide_blurb,
            "status":                 "active",
            "recommend_to_newcomers": should_recommend(name, category, existing),
            "source":                 "auto",
            "content_hash":           content_hash(path, topic, description,
                                                    (existing or {}).get("cadence", "")),
            "first_seen":             (existing or {}).get("first_seen", now),
            "last_seen":              now,
        }
        # Seed sig_display from channel_manifest.json for sig-type channels
        # (if not already set on the existing entry — PRESERVED_FIELDS will carry it forward after that)
        if cid in manifest_sig_displays and not (existing or {}).get("sig_display"):
            entry["sig_display"] = manifest_sig_displays[cid]

        # Preserve human-edited fields and event-sync fields from existing entry
        if existing:
            for field in PRESERVED_FIELDS:
                if field in existing:
                    entry[field] = existing[field]
            for field in ("next_event_name", "next_event_iso", "next_event_time", "secondary_events"):
                if field in existing:
                    entry[field] = existing[field]

        known[cid] = entry

    # ── Mark missing channels as archived ─────────────────────────────────────
    for cid, entry in known.items():
        if cid not in api_ids and entry.get("status") == "active":
            entry["status"] = "archived"
            entry["archived_at"] = now
            archived_count += 1
            print(f"  ARC  {entry.get('path', '#' + entry['name'])} (no longer in guild)")

    # ── Embed changed / new channels ──────────────────────────────────────────
    if not args.dry_run:
        vc    = get_voyage_client()
        index = get_pinecone_index()

        for entry in known.values():
            if entry.get("status") != "active":
                continue
            prev_hash = entry.get("last_embedded_hash", "")
            cur_hash  = entry.get("content_hash", "")
            if args.force_reembed or cur_hash != prev_hash:
                try:
                    embed_channel(entry, vc, index)
                    entry["last_embedded_hash"] = cur_hash
                    entry["last_embedded"]       = now
                    embed_count += 1
                    print(f"  EMB  #{entry['name']}")
                except Exception as exc:
                    print(f"  ⚠ embed failed for #{entry['name']}: {exc}")

        save_registry(registry)

    print(f"\n── Done ─────────────────────────────────────────")
    print(f"  New channels    : {new_count}")
    print(f"  Changed         : {changed_count}")
    print(f"  Archived        : {archived_count}")
    print(f"  Embedded        : {embed_count}")
    print(f"  Total in registry: {len(known)}")

    if not args.dry_run:
        append_run_log({
            "ts":          now,
            "script":      "sync_discord_channels",
            "new":         new_count,
            "changed":     changed_count,
            "archived":    archived_count,
            "embedded":    embed_count,
            "total":       len(known),
        })


if __name__ == "__main__":
    main()
