# C3PO — Migration: Personal Account → Protocol Institute Org

This document covers migrating the full C3PO system from personal accounts to the Protocol Institute organization when that is ready. The design is intentionally account-neutral so migration is low-risk.

---

## Design Invariants That Make Migration Clean

- **No `account_id` in `wrangler.toml`** — Cloudflare account ID is set via `CLOUDFLARE_ACCOUNT_ID` env var at deploy time. The config file is account-neutral.
- **Pinecone vectors are reproducible** — given the same source documents and Voyage AI model, the same chunks and embeddings are produced. Re-ingestion is a clean migration option.
- **Secrets are never committed** — all API keys are injected via `wrangler secret put` or environment variables. Nothing personal-account-specific is in the repo.
- **Discord bot registered under PI from Phase 3** — no migration needed for the bot itself.

---

## Resource-by-Resource Migration

### GitHub repo

Transfer from `vgururao/c3po` to `Protocol-Institute/c3po`:
1. GitHub repo settings → Danger Zone → Transfer
2. Update any local git remotes: `git remote set-url origin git@github.com:Protocol-Institute/c3po.git`
3. Update references in `protocolized-website/wrangler.toml` if the Worker name changes

### Cloudflare Worker

No code changes. Just redeploy with PI account credentials:
```bash
CLOUDFLARE_ACCOUNT_ID=<pi-account-id> wrangler deploy
```
Re-add all secrets in the PI account:
```bash
wrangler secret put VOYAGE_API_KEY
wrangler secret put PINECONE_API_KEY
wrangler secret put PINECONE_C3PO_HOST
wrangler secret put ANTHROPIC_API_KEY
wrangler secret put ADMIN_KEY
wrangler secret put MCP_API_KEY
```
Delete the Worker from the personal account after verifying the PI deployment.

### KV namespace (rate limiting)

KV data is ephemeral (rate limit counters). No data migration needed.
1. Create new KV namespace in PI account: `wrangler kv namespace create RATE_LIMIT`
2. Update `wrangler.toml` with the new namespace ID
3. The personal-account namespace can be deleted

### D1 databases (if added)

D1 database IDs are account-specific UUIDs. For each database:
```bash
# Export from personal account
wrangler d1 export <db-name> --output=backup.sql
# Create in PI account
CLOUDFLARE_ACCOUNT_ID=<pi-account-id> wrangler d1 create <db-name>
# Import
CLOUDFLARE_ACCOUNT_ID=<pi-account-id> wrangler d1 execute <db-name> --file=backup.sql
```
Update the `database_id` value in `wrangler.toml` after migration.

### R2 buckets (submissions + case studies)

Use `rclone` with S3-compatible credentials for both accounts:
```bash
# Configure rclone remotes for each account using R2 S3 API tokens
rclone copy cf-personal:c3po-submissions cf-pi:c3po-submissions --progress
rclone copy cf-personal:c3po-casestudies cf-pi:c3po-casestudies --progress
```
Update R2 binding names in `wrangler.toml` if bucket names change.

### Pinecone index

Two options — re-ingestion is recommended:

**Option A — Re-ingest (recommended):**
1. Generate a new Pinecone API key under the PI org's Pinecone account
2. Create a new index `c3po` with the same spec (1024d, cosine, aws us-east-1)
3. Run all ingestion scripts against the new index using the PI API key
4. Verify retrieval quality before cutting over
5. Update `PINECONE_API_KEY` and `PINECONE_C3PO_HOST` secrets in the PI CF Worker
6. Delete the personal-account Pinecone index

Vectors are deterministic — same source documents + same Voyage AI model = same embeddings. Re-ingestion is safe and produces an identical index.

**Option B — Export/import (if re-ingestion is too slow):**
Pinecone supports vector export via the `fetch` API (by ID list). This requires fetching all IDs, then all vectors in batches. Script this with `ingest/migrate_pinecone.py`. Use only if source documents are unavailable or ingestion scripts are missing.

### Voyage AI API key

Generate a new key under the PI org's Voyage AI account:
1. Add to PI CF Worker: `wrangler secret put VOYAGE_API_KEY`
2. Add to `Code/.env.keys` (under PI section)
3. Update all local `.env` files used by ingestion scripts
4. Revoke the personal-account key after confirming PI key works

### Anthropic API key

Same pattern: generate PI org key, add to Worker secrets, update `.env.keys`, revoke personal key.

### Discord bot

If the bot was registered under PI's Discord Developer account from Phase 3, no migration is needed. If it was registered under a personal Discord account:
1. Create a new application in PI's Discord Developer Portal
2. Generate a new bot token
3. Re-invite the bot to the PI Discord server with the same permission set
4. Update `DISCORD_BOT_TOKEN` in the Worker secrets and ingestion scripts
5. Revoke the personal-account bot token

### Slack app

If the Slack app was installed to a PI Slack workspace, migration is usually a reinstall:
1. Create a new Slack app under PI's Slack org (if needed)
2. Update `SLACK_SIGNING_SECRET` and `SLACK_BOT_TOKEN` in Worker secrets
3. Reinstall the app to the workspace

---

## Migration Checklist

- [x] CF Worker deployed to PI account (`team-7e8`) — **2026-05-31**
- [x] All 10 Worker secrets set on PI account — **2026-05-31**
- [x] KV namespace `C3PO_KV` created in PI account; `wrangler.toml` updated — **2026-05-31**
- [x] Queue `c3po-oracle` created in PI account — **2026-05-31**
- [x] Custom domain `c3po.protocolized.io` provisioned on protocolized.io zone — **2026-05-31**
- [x] All `c3po.vgr-702.workers.dev` references replaced with `c3po.protocolized.io` in worker.js — **2026-05-31**
- [x] `protocolized-website` resources page URL updated — **2026-05-31**
- [x] `admin/keys.md` updated with c3po Worker secrets — **2026-05-31**
- [ ] Repo transferred to `Protocol-Institute/c3po` (GitHub UI step — deferred)
- [ ] Personal-account Worker deleted (after 1-week verification period)
- [ ] D1 databases migrated (none yet)
- [ ] R2 buckets copied (none yet)
- [ ] Pinecone index re-ingested under PI org API key (deferred — to PI billing setup)
- [ ] Voyage AI key rotated to PI org key (deferred)
- [ ] Anthropic key rotated to PI org key (deferred)
- [ ] Discord bot token updated (registered under personal account; deferred)
- [ ] `Code/.env.keys` PI section updated when keys rotate
