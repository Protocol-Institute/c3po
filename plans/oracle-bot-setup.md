# C3PO Oracle Bot — Deployment Setup

*Written: 2026-05-26 · Status: pending execution*

Code is complete. This document is the step-by-step execution guide for the next session.

---

## What was built (Session 19)

- `POST /interactions` endpoint in `api/worker.js` — Discord Interactions webhook with Ed25519 signature verification
- `runRagQuery()` shared helper — RAG core extracted from POST /query; used by both the HTTP endpoint and the queue consumer
- `async queue()` handler in `export default` — consumes `c3po-oracle` queue jobs, runs RAG, posts back via Discord followup webhook
- `scripts/register_discord_commands.py` — registers `/ask`, `/search`, `/help` slash commands
- `api/wrangler.toml` — queue producer + consumer bindings
- `.env.template` — ORACLE_* vars documented

Architecture: Discord → `POST /interactions` → Worker ACKs with `{type:5}` (deferred) → enqueues to Cloudflare Queue → queue consumer runs RAG → posts answer via Discord followup API.

---

## Step 1 — Create a Discord application

1. Go to https://discord.com/developers/applications
2. Click **New Application** → name it `c3po_oracle` → Create
3. **General Information** tab → copy:
   - **Application ID** → `ORACLE_APPLICATION_ID`
   - **Public Key** → `ORACLE_PUBLIC_KEY`
4. **Bot** tab → **Reset Token** → copy token → `ORACLE_BOT_TOKEN`
5. Under Bot settings, ensure **Public Bot** is checked (needed for guild invite later)
6. No privileged intents needed (Oracle is slash-command only, no message reading)

Add to `.env`:
```
ORACLE_BOT_TOKEN=...
ORACLE_APPLICATION_ID=...
ORACLE_PUBLIC_KEY=...
```

---

## Step 2 — Create the Cloudflare Queue

```bash
cd api
npx wrangler queues create c3po-oracle
```

If successful, you'll see the queue ID. No further config needed — `wrangler.toml` already references `c3po-oracle` by name.

---

## Step 3 — Set Worker secrets

```bash
cd api
npx wrangler secret put ORACLE_BOT_TOKEN
npx wrangler secret put ORACLE_APPLICATION_ID
npx wrangler secret put ORACLE_PUBLIC_KEY
```

Each command prompts for the value. Paste from `.env`.

Optional — restrict to specific channels (leave empty for all):
```bash
npx wrangler secret put ORACLE_ALLOWED_CHANNEL_IDS
# value: comma-separated channel IDs, e.g. "1082444651946049567,1234567890"
# or just press Enter to allow all channels
```

---

## Step 4 — Deploy the worker

```bash
cd api
npx wrangler deploy
```

Verify the deployment shows the queue consumer binding and `POST /interactions` is live.

---

## Step 5 — Register the Interactions Endpoint URL

1. Discord Developer Portal → your `c3po_oracle` application
2. **General Information** tab → **Interactions Endpoint URL**
3. Enter: `https://c3po.vgr-702.workers.dev/interactions`
4. Click **Save Changes**

Discord will immediately send a PING request. The worker returns `{type:1}` (PONG). Discord will show a green checkmark. If it shows an error, check that the worker deployed successfully and `ORACLE_PUBLIC_KEY` is set correctly.

---

## Step 6 — Register slash commands

For instant testing, register to the PI Discord guild first:

```bash
source .venv/bin/activate
python3 scripts/register_discord_commands.py --guild-id YOUR_PI_GUILD_ID
```

Once tested, register globally (takes up to 1 hour to appear everywhere):
```bash
python3 scripts/register_discord_commands.py --global
```

Registered commands: `/ask <question>`, `/search <query>`, `/help`

---

## Step 7 — Invite the bot to the PI Discord

Generate an invite URL in the Discord Developer Portal:
- **OAuth2** → **URL Generator**
- Scopes: `bot` + `applications.commands`
- Bot Permissions: `Send Messages` + `Use Slash Commands`
- Copy the URL and open it to invite `c3po_oracle` to the PI Discord server

---

## Step 8 — Test in Discord

In the PI Discord (whichever channel you allowed):
```
/help
/ask what is a protocol handshake?
/search stigmergy
```

Expected behavior:
- `/help` — immediate ephemeral reply (only visible to you)
- `/ask` — Discord shows "c3po_oracle is thinking…" for a few seconds, then posts the answer publicly with sources
- `/search` — same deferred pattern, returns source list without synthesis

---

## Open questions / follow-up

- **Channel scope**: Set `ORACLE_ALLOWED_CHANNEL_IDS` once you decide which channel(s) should have access (e.g., a new `#c3po-queries` channel, or leave empty for all).
- **Rate limit**: Currently 5 queries per user per hour, tracked by Discord user ID in KV. Adjust `ORACLE_RATE_LIMIT_MAX` constant in `worker.js` if needed.
- **`definitions` namespace**: Not yet wired into `runRagQuery()` — still TODO #1 from session 18.

---

## Rollback

If anything goes wrong, roll back by removing the Interactions Endpoint URL in the Discord Developer Portal (set it to blank). The worker itself is unaffected — existing `/query` and all other routes continue to work.
