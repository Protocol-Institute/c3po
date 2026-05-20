# C3PO Discord Bot System — Design Document

*Status: draft · May 2026*

---

## Overview

The C3PO Discord integration has **two distinct bots with different operating modes**:

| Bot | Role | Mode | Auth |
|-----|------|------|------|
| **c3po_listener** | Keeps the corpus up to date; publishes meeting pages | Headless cron / launchd | Bot token (REST only) |
| **c3po_oracle** | Answers member questions in Discord | Interactive / always-on | Bot token (Gateway) |

Separating them keeps the listener stateless and cheap to operate (no persistent process), while the oracle bot can use a full Discord gateway connection for interactivity.

---

## Bot 1: c3po_listener

A collection of batch scripts run on a schedule via launchd. No persistent process.  
Current bot token: `DISCORD_BOT_TOKEN` in `.env`. Same token serves all listener tasks.

### Function 1 — Corpus sync (embeddings)

**Goal:** Keep Pinecone up to date with new Discord messages and SIG meeting transcripts.

**Scripts:**
- `ingest/sync_discord.py` — general channels (`discord` namespace); incremental by cursor
- `ingest/sync_sig.py` — SIG channels (`sig` namespace); incremental + full thread rescan

**Schedule:**
- `sync_discord.py`: daily, off-peak (e.g. 3am PT)
- `sync_sig.py`: biweekly (or twice weekly) to catch new meetings promptly

**State files:** `data/discord_state.json`, `data/sig_state.json`

**Known issue — missed meetings:** Thread names must match per-channel `meeting_patterns` in `sync_sig.py`. When PI members name a meeting thread with a novel prefix, the pattern misses it and the meeting is classified `is_meeting=False`. Recent SIGFPT meetings (May 1 and May 15, 2026) appear to be affected.

Fix strategy:
1. Add broader fallback pattern per channel (see §Missed Meetings below)
2. Add a `--rescan-threads` mode to re-evaluate `is_meeting` for all threads within the last 90 days without re-fetching messages

---

### Function 2 — Meeting index + summaries (website)

**Goal:** Keep the protocol-institute.org SIG archive pages current after each sync.

**Pipeline:**
1. `ingest/sync_sig.py` — detects and summarizes new meetings; writes `data/sigs/meetings/*.json`
2. `ingest/rebuild_sig_summaries.py` — (re)builds full structured summaries for all meetings
3. `ingest/generate_sig_pages.py` — writes `../website/sigs/*.html` and `../website/sigs.html`
4. Website deploy — `git push` to the `website` repo triggers Netlify rebuild

**Schedule:** Run as a pipeline after `sync_sig.py` completes (i.e., same launchd job, sequential).

**Auto-push consideration:** The pipeline can commit and push `../website/` automatically after page generation. This requires the launchd job to have a Git identity configured and push access to the website repo. Low risk since it's a content-only write. Decision: **auto-push yes**, with a guard that skips the push if no `.html` files changed.

---

### Function 3 — 2nd-order material (links + attachments)

**Goal:** Mine URLs and file attachments from Discord messages and embed the retrieved content into C3PO.

**Links sub-pipeline** (currently built):
1. `ingest/fetch_discord_links.py` — harvests URLs from `discord` + `sig` namespaces; fetches web content; embeds into `discord_links` namespace
2. `ingest/enrich_discord_links.py` — scores each page 0–3 for protocol relevance; deletes score-0 vectors

**Attachments sub-pipeline** (TODO):
- Discord CDN URLs expire after ~24 hours. Attachments must be downloaded at sync time.
- `sync_discord.py` and `sync_sig.py` already have access to `msg["attachments"]`; need to:
  1. Download at sync time to `data/attachments/` (by message ID)
  2. Detect file type (PDF → existing PDF pipeline; images → vision pass; `.docx`/`.pptx` → text extract)
  3. Store attachment metadata in a sidecar registry `data/discord_attachments_registry.json`

**YouTube sub-pipeline** (TODO):
- 161 deferred YouTube URLs in `discord_links_registry.json`
- Use `youtube-transcript-api` (no API key); embed transcript chunks into `discord_links` namespace with `source_type=youtube`

**Schedule:**
- `fetch_discord_links.py`: weekly (run after `sync_discord.py` + `sync_sig.py` to pick up new URLs)
- `enrich_discord_links.py`: weekly, immediately after fetch
- Attachment download: inline in sync scripts (at message ingest time, not a separate job)
- YouTube pass: one-time backfill + weekly incremental

---

### Launchd Job Design

Three launchd plists, all in `~/Library/LaunchAgents/`:

| Plist | Schedule | Scripts |
|-------|----------|---------|
| `org.protocol-institute.c3po.daily_sync` | Daily 3am PT | `sync_discord.py` |
| `org.protocol-institute.c3po.sig_sync` | Tue + Fri 11am PT | `sync_sig.py` → `rebuild_sig_summaries.py` → `generate_sig_pages.py` → website git push |
| `org.protocol-institute.c3po.weekly_links` | Sunday 2am PT | `fetch_discord_links.py` → `enrich_discord_links.py` |

**Logging:** Each job writes to `~/Library/Logs/c3po/<job>.log` (stdout) and `<job>.err` (stderr).

**Env vars:** Each plist sets `PATH`, `VIRTUAL_ENV`, and passes `.env` via a wrapper script `bin/run_with_env.sh` that `source`s `.env` before exec.

Wrapper script pattern:
```bash
#!/bin/bash
set -e
source /path/to/c3po/.env
exec /path/to/c3po/.venv/bin/python3 "$@"
```

**Failure alerting:** A `--post-summary` flag in each sync script already POSTs to `DISCORD_SUMMARY_CHANNEL_ID`. Summary posts serve as heartbeat + failure signal (if the channel goes silent, the job is down).

---

## Bot 2: c3po_oracle (query bot)

**Goal:** Let PI Discord members query C3PO directly from Discord with a slash command.

### Design

**Command surface:**
- `/ask <question>` — RAG query; posts a threaded reply with top sources
- `/search <query>` — semantic search; returns a bulleted source list without synthesis
- `/help` — brief description + link to the web UI

**Implementation options:**

| Option | Pros | Cons |
|--------|------|------|
| **A: discord.py + Gateway** (always-on process) | Full interactivity, streaming responses | Needs a persistent host (VPS, Cloud Run, Fly.io) |
| **B: Discord Interactions webhook** (serverless) | No persistent process; can run on Cloudflare Worker | 3s initial response deadline; must use `deferred` pattern |
| **C: Cloudflare Worker + Discord Interactions** | Unified with existing c3po worker; no new infra | 30s CPU limit; must queue long RAG calls |

**Recommendation: Option B/C hybrid** — Discord sends an HTTP POST to the c3po Cloudflare Worker endpoint. Worker immediately ACKs (deferred response), then enqueues a Cloudflare Queue task that does the RAG call and posts back via Discord's `followup` API. This keeps all bot logic in the existing worker, avoids a persistent host, and handles the 3s deadline cleanly.

If Cloudflare Queue latency proves problematic, fall back to a small always-on process (e.g. Fly.io free tier, ~$0/mo).

**Auth:** Separate Discord application from c3po_listener. Register at Discord Developer Portal → add application → slash commands. New env vars: `ORACLE_BOT_TOKEN`, `ORACLE_APPLICATION_ID`, `ORACLE_PUBLIC_KEY`.

**Response format:**
```
**Q:** What is a protocol handshake?

A handshake is a…[2–3 sentence synthesis]…

**Sources**
- [Substack] "On Protocol Handshakes" — Apr 2025
- [SIGFPT] Meeting transcript: 6Feb26
- [Discord] #idle-musings — @vgr, 12 Mar 2026
```

**Rate limiting:** Max 5 queries per user per hour (enforced in Worker via KV store with TTL).

**Scope gating:** Only permitted in designated channels (configurable via `ORACLE_ALLOWED_CHANNEL_IDS`). DMs: optionally allowed for admins only.

---

## Missed Meetings Fix

The May 1 and May 15, 2026 SIGFPT meetings are absent from `sig_state.json`. Possible causes:

1. **Thread naming changed** — new threads don't match `meeting_patterns`; most likely cause
2. **Threads are still active** (not archived) — should still be caught by `/threads/active`; less likely
3. **`sync_sig.py` hasn't run** since April — possible if launchd isn't set up yet

**Immediate fix:**
1. Run `sync_sig.py --dry-run` to see what threads are currently visible
2. If the threads exist but are `is_meeting=False`, inspect their names and update `meeting_patterns`
3. Run `sync_sig.py --channel 1327337414175490160` to re-process SIGFPT, which will re-evaluate `is_meeting` for new threads it discovers
4. Run `rebuild_sig_summaries.py` + `generate_sig_pages.py` to regenerate the website pages

**Long-term fix:** Add a "fuzzy date match" fallback pattern per channel:
- SIGFPT: any thread name containing a date-like token (e.g. `\b\d{1,2}(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\d{2}\b`) within 14 days of a known meeting cadence
- Or: flag any thread in a SIG channel that is longer than 10 messages as a potential meeting for human review

---

## Phase Roadmap

| Phase | Work | Status |
|-------|------|--------|
| **3A** | launchd jobs for daily_sync + sig_sync + weekly_links | TODO |
| **3B** | Missed meetings fix + auto-website-push | TODO |
| **3C** | Attachment capture in sync scripts + registry | TODO |
| **3D** | YouTube transcript pass (161 deferred URLs) | TODO |
| **3E** | c3po_oracle bot — webhook + deferred response | TODO |
| **3F** | Oracle slash commands registered + tested in PI Discord | TODO |

---

## Open Questions

1. **Oracle hosting** — Cloudflare Queue vs. Fly.io for the deferred RAG response? Cloudflare Queue is free tier but adds ~5s latency; Fly.io is simpler but another service to manage.
2. **Website auto-push** — Should the sig_sync launchd job auto-push to the website repo? Need to confirm Git identity on the machine and that `website` remote is configured.
3. **Summary channel** — `DISCORD_SUMMARY_CHANNEL_ID` is in `.env.template` but may not be set in `.env`. Which Discord channel should receive sync heartbeats?
4. **Oracle scope** — Should `/ask` be available in all PI Discord channels, or only `#c3po-queries` (a new dedicated channel)?
5. **c3po_listener token** — Is the current `DISCORD_BOT_TOKEN` the same application as `c3po_listener`? Need to verify the application ID before registering oracle slash commands on a separate application.
