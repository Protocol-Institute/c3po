# C3PO Bot Ecology — Architecture & Roadmap

> Last updated: 2026-05-28 (session 23)

C3PO is evolving from a single RAG endpoint into a multi-node system — a small swarm of listening, ingesting, and conversing agents that share a corpus, coordinate through a spool pattern, and collectively build institutional memory. This document describes the architecture and the roadmap for getting there.

---

## Current Nodes (2026-05-28)

| Node ID | Script | Type | Managed by | Logs |
|---------|--------|------|-----------|------|
| `c3po_listener` | `bin/daemon.py` | Ingest poller (REST, no gateway) | launchd `org.protocol-institute.c3po.daily` | `~/Library/Logs/c3po/daemon.log` |
| `c3po_bot` | `bin/c3po_bot.py` | Discord gateway bot (WebSocket) | launchd `org.protocol-institute.c3po-bot` | `~/Library/Logs/c3po/c3po_bot.log` |
| `c3po_web` | Cloudflare Worker | Web chat (HTTP, stateless) | Cloudflare managed | KV store + `/api/chats` |

---

## Design Principles

**One ingest owner.** Only `c3po_listener` (daemon.py) writes to Pinecone. All other nodes spool outputs locally; the listener picks them up each cycle and embeds them. No concurrent writers to the vector index.

**Stateless gateways.** `c3po_bot` and `c3po_web` hold no persistent state. They call the Worker API and optionally spool Q&A output. This keeps gateways simple and restartable without data loss.

**Spool as inbox.** `data/spool/` is a simple bounded queue. Any node drops a JSON file; the listener picks it up next cycle, embeds, and deletes. Future nodes (Slack bot, email responder, API gateway) are just new spool writers — no pipeline changes needed.

**bot_id on every transcripts vector.** Each ingested conversation carries a `bot_id` field (`c3po_bot`, `c3po_web`, future nodes). Enables per-node memory queries: "what has this channel said about X?"

**Registry-driven.** `config/bot_registry.json` is the single source of truth for what nodes exist, what type they are, where they log, and whether they produce transcripts.

---

## Node Details

### c3po_listener

Runs a full sync cycle every 30 minutes via `bin/daemon.py`. Each cycle:

1. `sync_discord.py` — general channels (REST poll)
2. `fetch_discord_links.py` — fetch pending URLs (cap: 200/cycle)
3. `enrich_discord_links.py` — score/prune with Claude Haiku
4. `sync_sig.py` — SIG channels
5. `rebuild_sig_summaries.py` — new meeting summaries
6. `generate_sig_pages.py` — regenerate SIG HTML pages
7. `generate_monitoring_page.py` — rebuild monitoring dashboard
8. `website push` — git commit+push if pages changed
9. *(Phase B)* `ingest/sync_bot_conversations.py` — spool → transcripts
10. *(Phase C)* `ingest/sync_web_chats.py` — web KV → transcripts

**Session log:** `~/Library/Logs/c3po/daemon_sessions.jsonl` — one JSON line per cycle with step outcomes and vector deltas.

### c3po_bot

Gateway Discord bot. Responds to `@c3po` mentions, opens threads, continues for up to 5 turns. Monitors `#introductions`. Does not write to Pinecone.

**Session log:** `~/Library/Logs/c3po/bot_sessions.jsonl` — one JSON line per conversation with query length, answer length, sources count, turn count, latency.

**Spool output (Phase B):** after each completed conversation, writes `data/spool/bot_conversations/{uuid}.json` with full Q&A struct for the listener to pick up.

Discord application: separate from `c3po_listener`. Token: `ORACLE_BOT_TOKEN`. App ID: `ORACLE_APPLICATION_ID`.

### c3po_web

Cloudflare Worker serving the web chat UI. Stateless HTTP. Conversations stored in KV with status flags (`public`, `private`, `reviewed`).

**Self-memory (Phase C):** `ingest/sync_web_chats.py` polls `/api/chats` with admin key, embeds public/reviewed conversations into the `transcripts` Pinecone namespace. State tracked in `data/web_chats_state.json`.

---

## Roadmap

### Phase A — Unified launchd + audit logs ✅ (session 23)

- [x] `org.protocol-institute.c3po-bot.plist` — c3po_bot under launchd, KeepAlive, persistent logs
- [x] `c3po_bot.py` writes per-conversation JSON summary to `bot_sessions.jsonl`
- [x] `daemon.py` writes per-cycle JSON summary to `daemon_sessions.jsonl`
- [x] Both bots logging to `~/Library/Logs/c3po/` consistently

### Phase B — Discord conversation self-memory

- [ ] `c3po_bot.py`: write completed Q&A to `data/spool/bot_conversations/{uuid}.json`
  - Fields: `bot_id`, `ts`, `user_id_hash`, `channel_id`, `thread_id`, `question`, `answer`, `sources`, `turn_count`, `latency_ms`
- [ ] `ingest/sync_bot_conversations.py`: read spool, embed, upsert to `transcripts` namespace as `chunk_type=discord_conversation`, delete spool file
- [ ] Add as step 9 in `daemon.py` cycle
- [ ] `normalizeSig`/`normalizeTranscript` in Worker: surface `discord_conversation` type in query results with c3po_bot label

### Phase C — Web chat self-memory

- [ ] `ingest/sync_web_chats.py`: poll `/api/chats`, embed public/reviewed conversations, upsert to `transcripts` as `chunk_type=web_conversation`
  - State: `data/web_chats_state.json` (last-seen chat IDs)
  - Only embed conversations with `rating >= 4` or `status: "public"` (quality gate)
- [ ] Add as step 10 in `daemon.py` cycle
- [ ] Worker: new `normalizeTranscript()` function handles both `discord_conversation` and `web_conversation`; tier weight 0.85× (high-quality, self-referential)

### Phase D — Bot registry + swarm scaffolding

- [ ] `config/bot_registry.json` — formal node registry with id, type, plist, log paths, transcripts namespace, spool path
- [ ] Shared session-logging helper (Python module) — DRY across all bots
- [ ] Monitoring page: show all bot node statuses (last seen, conversations today, vectors added)
- [ ] Naming convention locked: `c3po_{role}` — e.g., `c3po_listener`, `c3po_bot`, `c3po_web`, future: `c3po_slack`, `c3po_email`, `c3po_api`

### Phase E — Swarm (future)

As c3po evolves toward a multi-agent system:

- Multiple listener nodes monitoring different corpora or platforms
- Multiple gateway nodes on different channels (Slack, email, API)
- Each node registered, logged, and contributing to shared `transcripts` memory
- Orchestrator coordinates ingest priority and deduplication across spool dirs
- Per-node memory queryable: "what has the Slack bot said about protocols this week?"

The spool+registry pattern is designed to accommodate this without pipeline changes — new nodes are additive.

---

## launchd Plists

Both plists live in `~/Library/LaunchAgents/`. Manage with:

```bash
# Load (start) a new plist
launchctl load ~/Library/LaunchAgents/org.protocol-institute.c3po-bot.plist

# Unload (stop)
launchctl unload ~/Library/LaunchAgents/org.protocol-institute.c3po-bot.plist

# Check status
launchctl list | grep protocol-institute

# Restart (unload + load)
launchctl unload ~/Library/LaunchAgents/org.protocol-institute.c3po-bot.plist
launchctl load   ~/Library/LaunchAgents/org.protocol-institute.c3po-bot.plist

# View live logs
tail -f ~/Library/Logs/c3po/c3po_bot.log
tail -f ~/Library/Logs/c3po/daemon.log
```

---

## Log Formats

### `bot_sessions.jsonl` (one line per conversation)

```json
{"event":"conversation","ts":"2026-05-28T18:30:00Z","bot_id":"c3po_bot","user_hash":"a3f9...","channel_id":"108244...","thread_id":"150929...","query_len":82,"answer_len":1840,"sources_count":5,"turn":1,"latency_ms":3200}
```

### `daemon_sessions.jsonl` (one line per sync cycle)

```json
{"event":"cycle","ts_start":"2026-05-28T18:00:00Z","ts_end":"2026-05-28T18:03:12Z","steps":[{"name":"sync_discord","ok":true,"vectors_added":12},{"name":"sync_sig","ok":true,"vectors_added":0}],"total_vectors":12,"errors":0}
```
