**Executed 2026-08-01 (session 46).** VM is `c3po-vm.exe.xyz` (not `c3po.exe.xyz` —
exe.dev requires VM names ≥5 characters). See `status.md`'s session 46 entry for the
full account of what happened during execution, including two gaps this draft didn't
anticipate: gitignored data directories (`data/sigs/meetings/`, per-script state files)
that don't exist on a fresh clone and must be copied by hand or the daemon wastefully
re-derives them, and `sync_youtube_resources`'s wrangler/`CLOUDFLARE_API_TOKEN`
dependency (same shape as the Substack step below, but not optional — it's already a
core daemon step). Phase 5 (absorbing the GHA Substack workflow) was deliberately
**not** executed — that workflow already runs entirely on GitHub's own infrastructure,
independent of the laptop, so absorbing it wasn't required by the migration's actual
goal and would have added the same wrangler/Node dependency for no laptop-unblocking
benefit. This document is kept for its phase-by-phase command reference; treat
`status.md` session 46 as the authoritative account of what actually happened.

---

# exe.dev Migration Plan — C3PO Persistent Processes

**Supersedes:** [`plans/vps-migration.md`](vps-migration.md) (Hetzner-oriented; not executed). exe.dev is the
chosen host going forward — the old plan is kept for its systemd unit templates and Discord/env details,
which mostly carry over unchanged, but its provisioning steps (Phase 1–2) no longer apply.

**Scope (per 2026-07-08 decision):** `bin/daemon.py` (ingest cycle) **and** `bin/c3po_bot.py` (Discord gateway
bot) move to the exe.dev VM together. Humboldt stays out of scope — separate repo/project, can follow the
same pattern later if wanted. Claude Code on the VM is for **interactive SSH sessions only** — no
scheduled/autonomous Claude Code jobs are part of this plan.

---

## Why exe.dev over Hetzner

exe.dev VMs are root-SSH-access, persistent, Debian/Ubuntu-based Linux boxes with `apt` and `systemd`
available out of the box — functionally equivalent to the Hetzner CX22 the old plan specified, but the
account/VM already exists, so Phase 1 (provisioning) and most of Phase 2 (server hardening) from the old
plan are already done. $20/mo flat (2 vCPU, 8GB RAM, 100GB disk) vs Hetzner's ~€3.29/mo — a real cost
difference, but moot if this VM is already justified by other projects sharing it.

exe.dev's own agent ("Shelley") and HTTP-proxy/domain features are not needed here — c3po has no public
HTTP surface to serve from this VM (that's the Cloudflare Worker, unchanged). This VM is purely background
processes: the Discord gateway bot and the ingest daemon.

---

## Open questions to resolve before executing (need your input)

1. **VM access** — SSH hostname/alias for the existing VM, and whether it's accessed via plain `ssh` or an
   exe.dev CLI wrapper.
2. **Non-root user** — exe.dev drops you in as root. Recommend still creating a `pi` (or similar) non-root
   user for running the services, matching the old plan's hardening step — but if this VM is single-purpose
   and you're comfortable running as root, that's simpler. Your call.
3. **GitHub auth for the VM** — a dedicated fine-grained PAT scoped to `Protocol-Institute/c3po` (read),
   `Protocol-Institute/website` (read+write+PR), and `protocol-institute/protocolized-website` (read+write)
   is recommended — this also happens to satisfy the standing TODO ("rotate `GH_PAT` to fine-grained PAT").
   Alternative: reuse your personal `gh auth` session copied to the VM — simpler, but couples the
   automation's identity to your personal account the same way the laptop setup does today.
4. **Claude Code auth mode on the VM** — subscription login (`claude login`, device-code flow works over
   SSH) vs. a separate `ANTHROPIC_API_KEY` for API-metered billing. Subscription login is simplest if you're
   fine using your existing seat from a second machine.
5. **Directory layout** — depends on (2): `/opt/pi/...` if a `pi` user is created, `/root/...` or
   `/home/<you>/...` otherwise. `bin/daemon.py` resolves `WEBSITE_DIR` and `PROTOCOLIZED_DIR` as **sibling
   directories** to the c3po clone (`C3PO_DIR.parent / "website"` etc.) — whatever the base directory is,
   all three repos need to be cloned as siblings under it.

---

## Gap found during planning: no `requirements.txt`

The repo has no `requirements.txt` — the laptop's `.venv` was built up ad hoc over many sessions. Before
cloning anywhere new, this needs to exist. I can generate one now via `pip freeze` from the working laptop
venv (23 top-level-ish packages: `anthropic`, `pinecone`, `voyageai`, `discord.py`, `pdfplumber`,
`beautifulsoup4`, `youtube-transcript-api`, etc.) — say the word and I'll add it as its own small commit
ahead of the migration, independent of everything else here.

---

## Phase 1 — VM inventory & base packages

1. SSH in, confirm OS/version (`lsb_release -a`), existing `git`/`python3`/`node` versions.
2. Install Python 3.14 via `pyenv` (matches laptop; c3po's `CLAUDE.md` mandates 3.14 everywhere):
   ```
   curl https://pyenv.run | bash
   pyenv install 3.14.0 && pyenv global 3.14.0
   ```
3. `apt install -y git build-essential libssl-dev libffi-dev`
4. Install Node.js (needed for the GitHub CLI's newer releases are static binaries so this is really just
   for Claude Code): NodeSource setup script or `nvm`.
5. Install GitHub CLI: `apt install gh` (or the official apt repo if not in Debian/Ubuntu's default repos).
6. Install Claude Code: `npm install -g @anthropic-ai/claude-code`.

## Phase 2 — Credentials

1. Generate the GitHub PAT per open question 3 above; register it in `../admin/keys.md` per
   `security-policy.md` (never in `Code/.env.keys` — PI keys live in the admin repo).
2. `gh auth login --with-token < token-file`, then `gh auth setup-git` so plain `git push`/`git pull` use
   the same credential.
3. Copy `.env` values from `../.env.keys` / `../admin/keys.md`: `VOYAGE_API_KEY`, `PINECONE_API_KEY`,
   `PINECONE_C3PO_HOST`, `ANTHROPIC_API_KEY`, `DISCORD_BOT_TOKEN` + `DISCORD_GUILD_ID` +
   `DISCORD_CHANNEL_IDS`, `ORACLE_BOT_TOKEN` + `ORACLE_APPLICATION_ID` + `ORACLE_PUBLIC_KEY` +
   `ORACLE_ROLE_ID` + related. Cloudflare/Worker secrets are **not** needed on this VM — Worker deploys stay
   a separate, laptop-initiated action.
4. Resolve Claude Code auth per open question 4 above.

## Phase 3 — Clone & configure repos

```bash
cd <base-dir>   # /opt/pi, /root, or /home/<you> per open question 5
git clone https://github.com/Protocol-Institute/c3po.git
git clone git@github.com:Protocol-Institute/website.git      # or https + gh credential helper
git clone <protocolized-website repo url>
cd c3po
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt   # once it exists — see gap above
git config user.name  "c3po-daemon"
git config user.email "<something under your control>"      # for clean commit attribution
```

Same `git config` for the `website` and `protocolized-website` clones (commit attribution matters more
now that `website` pushes go through PRs with your name/email visible on them).

## Phase 4 — systemd services

Same shape as the old plan's Phase 3c, paths updated for wherever Phase 1–3 landed:

**`/etc/systemd/system/c3po-daemon.service`**
```ini
[Unit]
Description=C3PO ingest daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=<base-dir>/c3po
EnvironmentFile=<base-dir>/c3po/.env
ExecStart=<base-dir>/c3po/.venv/bin/python3 bin/daemon.py
Restart=always
RestartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

**`/etc/systemd/system/c3po-bot.service`** — identical shape, `ExecStart=... bin/c3po_bot.py`.

## Phase 5 — Absorb the GHA substack workflow

Once the daemon is running 24/7 here, the separate GitHub Actions cron for `sync_substack.py` becomes
redundant. Add it as a daemon step (step 0, before the existing steps), confirm it ingests at least one
cycle successfully on the VM, then delete `.github/workflows/sync-substack.yml`.

## Phase 6 — Cutover & verification

1. Start both services: `systemctl start c3po-daemon c3po-bot`
2. `journalctl -u c3po-daemon -f` / `journalctl -u c3po-bot -f` — confirm gateway connects, confirm one
   full ingest cycle completes without errors
3. Confirm the website PR flow works end to end: `gh pr list` shows the daemon successfully opening/updating
   a PR against `Protocol-Institute/website` when there are changes (this is new since the last plan draft —
   see `bin/daemon.py`'s `push_website_if_changed()`, which now needs `gh` specifically, not just `git`)
4. Confirm `protocolized-website` direct push still works
5. Unload the laptop launchd plists:
   ```
   launchctl unload ~/Library/LaunchAgents/org.protocol-institute.c3po-bot.plist
   launchctl unload ~/Library/LaunchAgents/org.protocol-institute.c3po.daily.plist
   ```
   then archive or delete the plist files.
6. Update `CLAUDE.md`'s architecture note ("Both local bots managed by launchd; logs at
   `~/Library/Logs/c3po/`") to describe the VM instead.

## Phase 7 — Retire GitHub Actions secrets

Once Phase 5 is confirmed working: remove `VOYAGE_API_KEY`, `PINECONE_API_KEY`, etc. from the c3po repo's
GitHub Actions secrets (or leave them — harmless once the workflow file is gone).

---

## Ongoing ops

- **Deploys:** `git pull` + `systemctl restart c3po-daemon` (or `c3po-bot`). No CI/CD.
- **Logs:** `journalctl -u c3po-daemon`, `journalctl -u c3po-bot`.
- **Secrets rotation:** edit `.env` on the VM, restart the affected service.
- **Interactive maintenance:** SSH in, `cd c3po`, `claude` — same workflow as this session, just running
  against the live persistent instance instead of the laptop's Dropbox-synced copy. Worth noting: the c3po
  repo on the VM and the laptop's Dropbox copy are now **two independent clones** — changes made via SSH
  need a normal `git push`/`git pull` round-trip to reach the laptop, same as any two-machine git workflow.
- **Monitoring:** `generate_monitoring_page.py` already reads daemon session logs; it'll reflect VM activity
  automatically once the daemon runs there.

## Not in scope for this plan

- Humboldt migration — same pattern would apply, deliberately deferred.
- Any exe.dev HTTP-proxy/domain/IAM-sharing features — nothing here needs a public endpoint from this VM.
- Scheduled/autonomous Claude Code runs on the VM — explicitly interactive-only per your answer above.
