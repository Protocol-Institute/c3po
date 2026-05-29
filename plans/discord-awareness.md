# C3PO Discord Awareness — Architecture & Roadmap

> Created: 2026-05-29 (session 25)

C3PO currently has a static, hardcoded, incomplete picture of the Protocol Institute Discord. This plan describes a richer, dynamic, embedded Discord awareness system that lets c3po act as a genuine Discord guide — for new member introductions, navigation questions, and future community features.

---

## Current State (what's broken)

**Hardcoded in `bin/c3po_bot.py`:**
```python
SIG_CHANNELS = {
    "SIGFPT":    {"id": ..., "desc": "Formal Protocol Theory — ..."},
    "MRG":       {"id": ..., "desc": "Memory Research Group — ..."},
    "SIGPfB":    {"id": ..., "desc": "Protocols for Business — ..."},
    "ProtFiSIG": {"id": ..., "desc": "Protocol Fiction — ..."},
}
```
The bot knows only these 4 SIGs. Any change to Discord structure (new channel, new SIG, renamed channel) requires a code edit and bot restart.

**`data/channel_manifest.json`** — ingest-focused, not a full Discord map:
- Covers 15 channels (2 active general, 4 active SIG, 9 archived)
- Not read by the bot at all
- Missing many channels: `#introductions`, `#announcements`, `#general`, newcomer channels, etc.
- Descriptions are ingest/scraping notes, not human-readable channel guides

**Introduction handler** — broken until session 25 (2026-05-29):
- Query length fix applied: Worker limit raised 500 → 2000 chars; intro text capped 800 → 400 chars
- Retrieval approach still suboptimal: the full prompt (preamble + SIG list + instructions) is sent as the Voyage embedding query instead of just the intro text
- SIG list is injected as prompt text rather than retrieved from Pinecone

---

## Target: Embedded Discord Awareness

The goal is a system where:

1. **Every channel in the Discord is described** in a structured, human-readable registry — purpose, audience, what to post, cadence, leads.
2. **Channel descriptions are embedded in Pinecone** (`discord_guide` namespace) so the bot can retrieve the right channels for any context via RAG — no hardcoding.
3. **The registry is the source of truth**, loaded by the bot at startup and updated without code changes.
4. **Intro handler uses proper RAG**: intro text → Voyage query → retrieve relevant corpus items *and* relevant channels → Claude synthesizes welcome reply.

---

## Phase 1: Channel Registry (`config/discord_channels.json`)

A new config file separate from `channel_manifest.json`. While the manifest is ingest-oriented (thresholds, patterns, backfill status), this file is *guide-oriented* — what a new member needs to know.

### Schema
```json
{
  "guild_id": "1082444651946049567",
  "channels": {
    "<channel_id>": {
      "id": "<channel_id>",
      "name": "channel-name",
      "display": "#channel-name",
      "type": "general | sig | announcements | introductions | archived | forum",
      "status": "active | archived | read-only",
      "sig_display": "SIGFPT",         // SIG channels only
      "cadence": "biweekly Fridays 10am PT",  // SIG channels only
      "lead": "Venkatesh Rao",
      "description": "One-paragraph description for RAG embedding — purpose, audience, what to post, what not to post.",
      "guide_blurb": "One-sentence blurb for the welcome reply — concise enough to include in a Discord message.",
      "recommend_to_newcomers": true,
      "tags": ["theory", "math", "foundations"]
    }
  }
}
```

### Channels to document (minimum)
- All 4 active SIGs (already partially described)
- `#idle-protocol-musings` — general freeform discussion
- `#protocol-watch` — link sharing
- `#introductions` — self-referential, not recommended but good to know
- `#announcements` — read-only, important to flag for newcomers
- Any other active non-archived channels not yet in the manifest

**Action needed from VGR:** provide the full list of active Discord channels with short descriptions, or give c3po bot access to read the guild channel list via Discord API.

---

## Phase 2: Embed Channels into Pinecone (`discord_guide` namespace)

New script: `ingest/sync_discord_channels.py`

- Reads `config/discord_channels.json`
- For each channel with `recommend_to_newcomers: true` (or all active channels), embeds the `description` field
- Upserts to a new `discord_guide` namespace
- Vector ID: `discord_channel__{channel_id}`
- Metadata: `channel_id`, `display`, `type`, `sig_display`, `guide_blurb`, `cadence`, `lead`, `tags`
- Incremental by content hash (re-embeds only if description changed)

Wire into daemon cycle as a lightweight step (rarely changes, can run weekly or on-change only).

---

## Phase 3: Rework Introduction Handler

With Phase 1+2 in place, the intro handler becomes proper RAG:

```
intro text
    ↓ (Voyage embed, query mode)
Pinecone: corpus namespaces → relevant articles/essays
Pinecone: discord_guide namespace → relevant channels + SIGs
    ↓
Claude: synthesize welcome reply using retrieved channels + corpus hits
```

**Changes to `bin/c3po_bot.py` `handle_introduction()`:**
- Send `intro_text` alone as the `query` to the Worker (not the full prompt)
- Pass `namespaces: ["pdfs", "substack", "videos", "discord_guide"]` in the request (or let Worker handle it)
- Worker returns both corpus sources and channel recommendations from `discord_guide`
- Bot formats reply: welcome + corpus recommendation + channel/SIG recommendation

**Changes to `api/worker.js`:**
- New query path or flag: `mode: "intro"` that adds `discord_guide` to the namespace set
- `normalizeDiscordGuide()` → formats channel hit as a recommendation with `guide_blurb` + Discord `<#id>` mention
- Channel hits weighted separately from corpus hits in `mergeResults()`

---

## Phase 4: General Discord Navigation

Once embedded, the same `discord_guide` namespace enables navigation queries:
- "Where should I post about X?" → retrieve from `discord_guide`
- "What SIGs exist?" → retrieve all SIG channels
- "When does SIGFPT meet?" → retrieve SIGFPT channel entry (cadence in metadata)

This can be wired into the existing mention-based RAG path — c3po can answer Discord structure questions just like corpus questions, without any special casing.

---

## Open Questions

1. **How to get the full channel list?** Options:
   - Manual: VGR provides a list of all active Discord channels
   - Automated: bot calls `GET /guilds/{guild_id}/channels` at startup and logs unknown channels; human reviews and adds to registry
   - Hybrid: bot auto-discovers and flags new channels; human writes descriptions

2. **Should `discord_guide` be queried on every RAG call or only for intro/navigation queries?** Adding it to all queries risks polluting corpus results with channel recommendations. Probably: only for intro mode + explicit navigation queries.

3. **Sync cadence.** Channel descriptions change rarely. Weekly re-embed in daemon cycle is probably sufficient; alternatively, trigger manually when `discord_channels.json` is edited.

4. **Should the Worker know about Discord channels?** The web UI has no use for channel recommendations. Probably keep it bot-only: the bot queries `discord_guide` separately (direct Pinecone call) rather than routing through the Worker.

---

## Status

| Phase | Status |
|-------|--------|
| Phase 1: Channel registry | ✅ Complete — `ingest/sync_discord_channels.py` auto-discovers from Discord API; 78 channels found; wired into daemon as step 1 |
| Phase 2: Pinecone embed | ✅ Complete — `discord_guide` namespace; Haiku auto-describes each channel; re-embeds on content_hash change |
| Phase 3: Intro handler rework | ✅ Complete — RAG-driven channel recs + corpus resources; next_event_time shown in reply |
| Phase 3.5: Scheduled events sync | ✅ Complete — `ingest/sync_discord_events.py`; cadence + next_event_time in registry + Pinecone |
| Phase 4: General navigation | ⬜ Not started — discord_guide namespace ready; needs "where should I post?" routing in bot |

**Immediate next step:** Phase 4 — handle direct navigation questions ("where should I post about X?", "what SIGs exist?") by querying discord_guide directly from the bot's mention handler.
