# VPS Migration Plan — PI Org Backend

> **Superseded 2026-07-08** by [`plans/exe-dev-migration.md`](exe-dev-migration.md). exe.dev (existing
> account/VM) replaced Hetzner as the chosen host — this doc's Phase 1–2 (provisioning, server hardening) no
> longer apply, but its systemd unit templates and Discord/env inventory were carried forward. Kept for
> reference; do not execute this plan.

## Goal

Move all persistent PI org processes off the laptop onto a shared VPS. After migration, the laptop is no longer required for any PI infrastructure.

## Current state

| Process | Host | Managed by |
|---------|------|-----------|
| `c3po_bot.py` | Laptop | launchd |
| `bin/daemon.py` | Laptop | launchd |
| `sync_substack.py` | GitHub Actions | GHA cron |
| Humboldt daemon + bot | Laptop | launchd |
| Cloudflare Worker (`c3po.protocolized.io`) | Cloudflare | wrangler (stays) |

## Target state

| Process | Host | Managed by |
|---------|------|-----------|
| `c3po_bot.py` | VPS | systemd |
| `bin/daemon.py` | VPS | systemd |
| Humboldt daemon + bot | VPS | systemd |
| Cloudflare Worker | Cloudflare | wrangler (unchanged) |
| GitHub Actions | — | retired |

## Host

**Hetzner CX22** — €3.29/month, 2 vCPU, 4GB RAM, 40GB SSD, Ubuntu 24.04 LTS. Comfortably runs 4 Python processes with room to grow.

---

## Phase 1 — Server setup

1. Provision CX22 on Hetzner Cloud, Ubuntu 24.04 LTS
2. Create non-root user `pi`, SSH key auth only, disable password login
3. Install Python via pyenv (c3po uses 3.14 features; pyenv gives clean version control):
   ```
   curl https://pyenv.run | bash
   pyenv install 3.14.0
   pyenv global 3.14.0
   ```
4. Install system deps: `git`, `build-essential`, `libssl-dev`, `libffi-dev`
5. Create project directories: `/opt/pi/c3po/`, `/opt/pi/humboldt/`, `/opt/pi/website/`

---

## Phase 2 — Deploy keys

Each repo that the VPS needs to push to requires a deploy key with write access.

| Repo | Access needed | Why |
|------|-------------|-----|
| `Protocol-Institute/c3po` | read + write | clone, commit state files |
| `Protocol-Institute/humboldt` | read + write | clone, commit state files |
| `protocol-institute/website` | write | c3po daemon pushes SIG pages + monitoring HTML |

Steps per repo:
1. Generate key pair on VPS: `ssh-keygen -t ed25519 -f ~/.ssh/deploy_<repo>`
2. Add public key as deploy key in GitHub repo settings (write access)
3. Configure `~/.ssh/config` with `IdentityFile` per host alias

---

## Phase 3 — C3PO migration

### 3a. Clone and configure

```bash
cd /opt/pi
git clone git@github.com:Protocol-Institute/c3po.git
cd c3po
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Copy `.env` (all secrets — see `../admin/keys.md`). The VPS `.env` is not committed anywhere.

Update `WEBSITE_DIR` path: the daemon currently resolves `../website` relative to the repo. On the VPS, website is at `/opt/pi/website` — set `WEBSITE_DIR` as an env var or update the path in `daemon.py`.

### 3b. Absorb the GHA substack workflow

Add `sync_substack.py` as step 0 in `bin/daemon.py`. This replaces the GitHub Actions cron. The daemon already handles all other ingest; substack was only on GHA because the daemon wasn't cloud-hosted.

### 3c. Systemd units

**`/etc/systemd/system/c3po-bot.service`**
```ini
[Unit]
Description=C3PO Discord gateway bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/opt/pi/c3po
EnvironmentFile=/opt/pi/c3po/.env
ExecStart=/opt/pi/c3po/.venv/bin/python3 c3po_bot.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

**`/etc/systemd/system/c3po-daemon.service`**
```ini
[Unit]
Description=C3PO ingest daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/opt/pi/c3po
EnvironmentFile=/opt/pi/c3po/.env
ExecStart=/opt/pi/c3po/.venv/bin/python3 bin/daemon.py
Restart=always
RestartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

### 3d. Bot conversation spool

`sync_bot_conversations.py` reads from `data/spool/bot_conversations/` — local files written by `c3po_bot.py`. Since both bot and daemon run on the same VPS, the spool directory is shared on disk. **No redesign needed.** This is the main reason to colocate rather than use Railway.

### 3e. Verify and cut over

1. Start both services, watch `journalctl -u c3po-bot -f` and `journalctl -u c3po-daemon -f`
2. Confirm bot connects to Discord gateway and responds to a test mention
3. Confirm daemon completes a full cycle without errors
4. Confirm website push succeeds (SIG pages committed to website repo)
5. Unload launchd plists on laptop:
   ```
   launchctl unload ~/Library/LaunchAgents/org.protocol-institute.c3po-bot.plist
   launchctl unload ~/Library/LaunchAgents/org.protocol-institute.c3po.daily.plist
   ```
6. Delete or archive the plists

---

## Phase 4 — Humboldt migration

Same pattern as c3po.

```bash
cd /opt/pi
git clone git@github.com:Protocol-Institute/humboldt.git
cd humboldt
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Copy humboldt `.env` (Pinecone, Anthropic, Discord bot token, etc.).

**`/etc/systemd/system/humboldt.service`**
```ini
[Unit]
Description=Humboldt autonomous research daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/opt/pi/humboldt
EnvironmentFile=/opt/pi/humboldt/.env
ExecStart=/opt/pi/humboldt/.venv/bin/python3 -m agent.humboldt daemon run
Restart=always
RestartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Verify, then retire humboldt launchd plist on laptop.

---

## Phase 5 — Retire GitHub Actions

Once daemon is running on VPS with `sync_substack.py` as step 0:

1. Confirm VPS daemon has successfully ingested at least one new Substack post
2. Delete `.github/workflows/sync-substack.yml` from the c3po repo
3. Remove GHA secrets (VOYAGE_API_KEY, PINECONE_API_KEY, etc.) from the repo settings — or leave them; they're harmless

---

## Phase 6 — Decommission personal infrastructure

Remaining cleanup from the PI org migration (session 27):

1. Delete personal Cloudflare Worker (`c3po` on `vgr-702`) — window passed 2026-06-07
2. Delete personal Pinecone index (was migrated to PI org index in session 27)

---

## Ongoing ops

**Deployments:** `git pull` on the VPS + `systemctl restart <service>`. No CI/CD needed for now.

**Logs:** `journalctl -u c3po-bot`, `journalctl -u c3po-daemon`, `journalctl -u humboldt`

**Secrets rotation:** Update `.env` file on VPS, restart the affected service.

**Python upgrades:** `pyenv install <version>`, rebuild venv.

**Monitoring:** The c3po monitoring page (`generate_monitoring_page.py`) already reads daemon session logs — it will reflect VPS activity once the daemon is running there.

---

## Open questions

- **Git identity on VPS** — daemon commits to website repo. Need to configure `git config user.name/email` on the VPS for clean commit attribution.
- **Humboldt notebook publishing** — humboldt's `publish.py` writes to `protocol-institute.org` website. Confirm this uses the same website repo clone, or needs its own deploy key.
- **Budget alerts** — humboldt has a $5/day circuit breaker. Confirm this works correctly when running as a systemd service (reads `daemon/costs.jsonl` on local disk — should be fine).
- **State file persistence across deploys** — `git pull` won't overwrite state files since they're gitignored. Safe.
