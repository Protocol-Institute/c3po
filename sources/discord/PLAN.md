# Discord Integration Plan

Covers both corpus ingestion (farming messages and links) and the query interface (slash commands). See `ARCHITECTURE.md` for where Discord fits in the overall C3PO system.

---

## The Key Constraint: MESSAGE_CONTENT Intent

Since August 2022, bots cannot read message content by default. The `MESSAGE_CONTENT` privileged intent must be explicitly enabled.

**Under 100 servers:** enable in the Discord Developer Portal — no verification or approval required. This is C3PO's situation (one private PI server). Non-issue.

**100+ servers:** requires a formal Discord use-case review. Discord evaluates whether the use is "unique, compelling, transformative, and privacy-respecting." Mass archival/ingestion is specifically the kind of use case they scrutinize. Not a concern unless C3PO is ever deployed as a public multi-server bot.

Without `MESSAGE_CONTENT`, the bot receives message objects with empty `content`, `embeds`, `attachments`, and `components` fields — it can only read messages that @mention it or that it sent itself. Useless for ingestion.

---

## The Biggest Surprise: CDN Attachment URLs Expire After 24 Hours

Introduced December 2023. Every Discord attachment URL now contains:
- `ex=` — expiration Unix timestamp (hex)
- `is=` — issued Unix timestamp (hex)
- `hm=` — HMAC signature

After 24 hours the URL returns 404, permanently. There is no way to refresh or extend a CDN URL without re-fetching the message object from the Discord API.

**For C3PO ingestion this means:**

| Content type | Impact |
|---|---|
| Text links shared in messages | None — store the URL as-is |
| Uploaded files (PDFs, docs) | Must be **downloaded immediately** during ingestion or lost |
| Images | Same — download immediately or lose them |
| Historical attachments (pre-crawl) | Already expired if the message is old — unrecoverable |

**Strategy:** during the daily batch crawl, any message with attachments gets its files downloaded to R2 (`c3po-submissions` bucket) immediately. The Pinecone chunk stores the R2 URL, not the Discord CDN URL.

---

## Architecture: No Persistent Bot Process Needed

This is the most important design decision. Passive message ingestion (listening for `MESSAGE_CREATE` events in real time) requires a **persistent WebSocket connection** to Discord's Gateway — which means always-on hosting (VPS, Fly.io, Railway), not CF Workers.

For a research corpus, real-time ingestion is not required. A daily batch REST API crawl captures the same content with up to 24 hours of lag — acceptable for C3PO. This eliminates the persistent hosting requirement entirely.

**Split:**

| Function | Method | Hosting |
|---|---|---|
| Slash commands (`/c3po`, `/submit`) | HTTP Interactions endpoint | CF Worker |
| Message ingestion | Scheduled REST API crawl | GitHub Actions cron |
| Attachment download | During the crawl | Same cron job → R2 |
| Link extraction | `message.content` + `embeds[]` | Same cron job |

Note: if real-time ingestion ever becomes necessary (e.g., for a future live moderation or notification feature), Cloudflare Durable Objects can maintain a persistent Gateway WebSocket connection. Defer this until needed.

---

## REST API Ingestion — How It Works

**Endpoint:** `GET /channels/{channel_id}/messages?limit=100&before={last_message_id}`

- Max 100 messages per request
- No limit on how far back you can go (Discord snowflake IDs encode timestamps back to the Discord epoch: January 1, 2015)
- Paginate backwards: fetch 100, take the last message ID, use as `before=` in the next request
- Rate limits: global 50 req/s, per-route limits vary — always parse `X-RateLimit-*` response headers, never hardcode limits

**Incremental ingestion (daily):** store the most recent ingested message ID per channel in `sources/discord/registry.json` as `last_message_id`. Each run fetches only messages newer than that watermark.

**Initial bulk ingestion:** paginate backwards from the present to the server's creation date. For a large server with years of history this takes hours to days — run once, spread over time with exponential backoff on rate limit hits.

**Embeds are free link metadata:** Discord auto-generates `embeds[]` on messages containing URLs, with pre-extracted `title`, `description`, and `thumbnail` from OG metadata. For link farming, this gives structured metadata without having to scrape URLs server-side.

---

## Thread and Forum Channel Access

| Content type | Permission needed | Notes |
|---|---|---|
| Active threads | `VIEW_CHANNEL` on parent | Standard |
| Archived threads | `MANAGE_THREADS` | Grant this to the bot from the start |
| Locked archived threads | `MANAGE_THREADS` | Same |
| Private threads | Explicit invite or `MANAGE_THREADS` | Require explicit scoping |
| Forum channel posts | `VIEW_CHANNEL` on forum | Posts are threads; same pagination applies |
| Archived forum posts | `MANAGE_THREADS` | Same as archived threads |

API endpoints:
- Active threads in channel: `GET /channels/{id}/threads/active`
- Archived public threads: `GET /channels/{id}/threads/archived/public`
- Archived private threads: `GET /channels/{id}/threads/archived/private` (requires `MANAGE_THREADS`)
- Messages in a thread: `GET /channels/{thread_id}/messages` (same as regular channels)

---

## Bot Setup

**Register under PI's Discord Developer account from day one** — not a personal account. No migration needed later.

**Required permissions (in each designated channel):**
- `VIEW_CHANNEL`
- `READ_MESSAGE_HISTORY`
- `MANAGE_THREADS`

**Required intents (in Developer Portal):**
- `MESSAGE_CONTENT` — privileged, enable manually
- `GUILDS`
- `GUILD_MESSAGES`

**Not needed:**
- `GUILD_MEMBERS`, `GUILD_PRESENCES` — don't need member info or online status
- Any moderation permissions beyond `MANAGE_THREADS`

---

## Slash Commands (CF Worker — HTTP Interactions)

The bot's query interface is a separate concern from ingestion. Slash commands are stateless HTTP requests — CF Worker handles them with no persistent connection.

Discord POSTs interaction payloads to the configured endpoint URL. The Worker must respond within 3 seconds. For queries that take longer (C3PO RAG calls typically take 3–8 seconds): acknowledge immediately with a deferred response (`type: 5`), then POST the result to `interaction.token` when ready (valid for 15 minutes).

**Commands:**

| Command | Description |
|---|---|
| `/c3po <question>` | RAG query — answer with cited sources |
| `/c3po search <terms>` | Semantic search — list matching resources |
| `/c3po submit <url>` | Add URL to submissions review queue |
| `/c3po status` | Corpus stats from source registries (admin only) |
| `/c3po approve <id>` | Approve a queued submission (admin only) |

**Verification:** Discord requires the HTTP Interactions endpoint to verify request signatures using `Ed25519`. The CF Worker must validate the `X-Signature-Ed25519` and `X-Signature-Timestamp` headers on every request.

---

## Known Limitations and Gotchas

**No edit history:** the API only returns the current version of a message. No endpoint exposes prior revisions. Accept this — research corpus doesn't need edit history.

**Deleted messages:** not recoverable via REST API. If a message is deleted after ingestion, its vector stays in Pinecone. For a research archive, treat this as acceptable (the content was public at time of ingestion). Do not build deletion tracking.

**Attachment expiry on historical ingestion:** if bulk ingestion runs months after a message was posted, its attachments are already expired. Text content and links are still available; files are not. Document this limitation in the source registry notes.

**Rate limits on bulk crawl:** for large channels, spread the initial bulk ingestion over days. Use exponential backoff with jitter. A GitHub Actions cron that runs nightly and processes N channels per run is safer than a single large crawl job.

**Forum channel pagination inconsistency:** newly archived threads may not immediately appear in the archived thread list (eventual consistency). Tolerate this — they'll appear in the next crawl.

**Private channels:** the bot only sees channels where it has `VIEW_CHANNEL`. Never grant blanket access to all channels. Maintain an explicit allowlist of channels to ingest in `sources/discord/registry.json`.

---

## Privacy and Transparency

The PI Discord server members should know the bot exists and what it does:

- Bot should be visible in the member list (do not hide it)
- Server rules or #announcements should note: "Designated research channels are indexed by C3PO, the PI research assistant"
- The `access_level: "member"` tier in C3PO means Discord content is only queryable by authenticated PI members (SIWE), not the general public — note this in the server announcement
- Channel owners can opt a channel out by not granting the bot `VIEW_CHANNEL` — make this the default for non-research channels

---

## registry.json

```json
{
  "source": "discord",
  "display_name": "Protocol Institute Discord",
  "pinecone_namespace": "discord",
  "vector_count": 0,
  "last_ingested": null,
  "last_message_ids": {},
  "schema_version": 1,
  "access_level": "member",
  "freshness_cadence": "daily",
  "ingest_script": "ingest/ingest_discord.py",
  "channel_allowlist": [],
  "notes": "Designated channels only. Attachment CDN URLs expire after 24h — download to R2 immediately during crawl."
}
```

`last_message_ids` is a map of `{channel_id: message_id}` — the watermark for incremental ingestion per channel. `channel_allowlist` is the explicit list of channel IDs to crawl.
