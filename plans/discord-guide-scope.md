# Discord Guide — Embedding Scope Policy

> Created: 2026-08-04 (session 47), follow-up to `discord-awareness.md` Phase 2.

## Why this doc exists

`config/discord_channels.json` currently conflates two different questions under one implicit rule ("is this channel active in the guild?"):

1. **What gets proactively recommended to newcomers** (`recommend_to_newcomers` field, used by the intro handler's channel suggestion).
2. **What gets embedded into the `discord_guide` Pinecone namespace at all** — i.e. what's even eligible to surface when the bot answers "where should I post about X?" or does general Discord navigation.

Today, embedding scope = #2, but `ingest/sync_discord_channels.py`'s embed loop only skips a channel when its Discord-guild presence is gone (`status != "active"`). It does not look at `recommend_to_newcomers` or category. Every channel that still exists in the guild — including all 49 in the read-only "archived read only" category — gets embedded, which is why the namespace holds 80 entries. This doc defines the intended, narrower embedding scope, distinct from (and broader than) the recommend scope.

**This is a policy doc only — no code changes yet.** See Implementation Notes at the bottom for the follow-up.

---

## Recommend scope (`recommend_to_newcomers`) — for contrast, unchanged by this doc

Target: **only** the SIG channels (SPECIAL INTEREST GROUPS category) and `#idle-protocol-musings`.

Current live state is broader than this target — `recommend_to_newcomers` is presently `True` for most PLAZA/PROTOCOLIZED/BACKGROUND channels too, not just SIGs + idle-protocol-musings. Narrowing that is a separate follow-up from this doc; flagging the gap here so it isn't lost.

---

## Embed scope (`discord_guide` namespace)

Default rule: **embed unless explicitly excluded below.** A channel opts out via `embed_override: false` on its registry entry (mirrors the existing `recommend_newcomers_override` pattern); a channel with no override and not matching an exclusion rule is embedded automatically, including channels created after this policy is adopted.

### Guiding principle for new/changed channels

`ingest/sync_discord_channels.py` runs every daemon cycle and auto-discovers new or renamed channels/categories directly from the Discord API (the `NEW`/`CHG` detection in its main loop). The explicit lists below cover what exists today; for anything the auto-discovery finds later that isn't already covered by an explicit rule, apply this test:

> **Would embedding this channel's content be useful for long-term conversational/discourse memory — i.e. could the bot plausibly retrieve it later to answer a real question about protocol theory, PI community discussion, or "where should I post about X"?** If yes, embed it. If the channel is transient administrative noise — bot-ops, meetup logistics, contest/judging scaffolding, temporary test channels, link-dump feeds — exclude it.

This is the same reasoning that justifies the explicit exclusions above (MOD = staff admin noise, Server Link Feed = automated dump, introductions/bugs/announcements = operational rather than discursive) and should be applied by whoever reviews a newly auto-discovered channel, rather than defaulting purely on category name matching. Category-based rules are a shortcut for applying this test at scale, not a replacement for it — if a new category doesn't cleanly match "SIG-like discourse" or "admin noise," judge it directly against the principle instead of guessing from the category name alone.

### Embed

- **SPECIAL INTEREST GROUPS** (entire category, all 9 channels): `meeting-notes`, `sig-hosts-best-practices`, `sig-talk`, `⏳-psychohistory`, `🎩-formal-protocol-theory`, `👾-protocol-fiction`, `🕸️-memory-research-group`, `🚜-protocols-for-business`, `🦾-distributed-robotics`
- **PLAZA** > `🤔-idle-protocol-musings`
- **PLAZA** > `protocol-institute-forum`
- **PROTOCOLIZED** (entire category, all 3 channels): `editorial`, `protocol-nonfiction`, `🏆-pitches-bounties-workshop`
- **BACKGROUND** > `transcriptions`
- All other channels not listed under "Do not embed" (default-embed rule — e.g. `new-nature`, `☀️-general`, `🎲-random`, `🔍-protocol-watch`, `protocol-institute-network`, `symposium-2026`, `⭐-popular-posts`, `🙋-channel-proposals`, the uncategorized `rules` channel)

### Do not embed

- **PLAZA** > `👋-introductions` — self-referential, handled by the dedicated intro flow, not the guide
- **PLAZA** > `bugs-and-tests`
- **PLAZA** > `📣-announcements`
- **MOD** (entire category): `introductions-mod`, `moderator-only` — staff-only, not member-facing; inferred exclusion (not explicitly named by VGR, but private/mod content has no business surfacing in a public-facing RAG guide — flag if this assumption is wrong)
- **Server Link Feed** (entire category): `accepted-links`, `rejected-links` — automated bot output, not conversational; same inferred-exclusion caveat as MOD
- **archived read only** (entire category, 49 channels) — see special case below, not a permanent exclusion

### Archived read only — embed once, then freeze (special case)

- Embed each channel in this category **exactly once** (assumed already done for the current 49 — verify each has a `last_embedded_hash` in the registry before treating this as satisfied).
- After that one-time embed, **stop tracking for updates**: no re-fetching sample messages, no re-running Haiku description, no content-hash comparison, no re-embed on daemon cycles — even if Discord-side metadata (name, topic) changes.
- Rationale: these channels are permanently read-only, so their content won't meaningfully change; continued polling only burns API calls and Haiku cost for zero retrieval benefit.
- If a channel is *newly moved into* "archived read only" (wasn't there before), it still gets its one-time embed under the default-embed rule, then freezes going forward.

---

## Implementation notes (for the follow-up code change)

- `ingest/sync_discord_channels.py`: add `NO_EMBED_CATEGORIES = {"MOD", "Server Link Feed"}` and a name-based exclusion set for `introductions`/`bugs-and-tests`/`announcements` (match against the emoji-stripped channel name, since real names are `👋-introductions`, `📣-announcements`).
- Add a `should_embed(name, category, existing)` helper parallel to the existing `should_recommend()`, respecting a new preserved field `embed_override` (true/false), same pattern as `recommend_newcomers_override`.
- Change the embed-loop guard at `sync_discord_channels.py:396` (currently `if entry.get("status") != "active": continue`) to also require `should_embed(...)`.
- For the archived-read-only freeze: once a channel's category resolves to "archived read only" **and** it already has `last_embedded_hash` set, skip the whole `need_describe` / content-hash-recheck block for it on future cycles (e.g. tag it `frozen: true` the first time this condition is met, and short-circuit on that flag thereafter).
- Does not touch `recommend_to_newcomers` / `should_recommend()` — that computation is unchanged by this doc; narrowing it to SIGs + idle-protocol-musings is a separate follow-up (see "Recommend scope" section above).
