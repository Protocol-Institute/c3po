# VM credential hardening — c3po-vm.exe.xyz

**Incident:** [`Code/incidents/2026-09-09-c3po-vm-github-token-scope.md`](../../../incidents/2026-09-09-c3po-vm-github-token-scope.md)
**Reference implementation of the pattern:** [`../humboldt/plans/phase5-vm-cutover.md`](../../humboldt/plans/phase5-vm-cutover.md) §3–§7
**Written:** 2026-09-09 (session 52). Phase 1 executed the same day; phases 2–3 open.

The incident found a GitHub token on `c3po-vm.exe.xyz`, authenticated as the account
owner, readable by `exedev` — the user both c3po services run as, which has passwordless
sudo. This is the c3po-side runbook for closing it. Humboldt's plan is the greenfield
version of the same design; c3po differs in being live, in having three checkouts rather
than one, and in having its org forbid the credential type that plan uses.

---

## What the survey found (2026-09-09)

Beyond what the incident recorded:

- **The token's reach is wider than "the org."** It has `admin:true, push:true` on
  `vgururao/venkateshrao.com` as well — it is an all-repositories token on the *account*,
  personal repos included, not a Protocol-Institute token.
- **`notes-ingest.exe.xyz` is clean.** No `gh auth` at all; it already pushes
  `vgururao/notes-archive` with a per-repo deploy key (`~/.ssh/id_ed25519_deploy`). The
  incident's "also check" item closes negative, and the good pattern already existed on
  this account a month before the bad one.
- **The laptop uses a different credential** (classic OAuth token in the macOS keyring,
  scopes `repo, workflow, gist, read:org, delete_repo`). Revoking the VM's PAT does not
  affect local work.
- **The VM holds no SSH keys and cannot reach `exe.dev`** over SSH from inside.
- **All three checkouts genuinely need write.** `bin/daemon.py` pushes `c3po` (state
  files, `:243`), `protocolized-website` (resource sync straight to main, `:181`), and
  the `c3po/auto-sig-pages` branch of `website` (`:351`). None can be demoted to read-only.
- **Only one capability needed more than push:** `gh pr list` / `gh pr create` against the
  website repo. That single call was the entire reason an API token existed on the box.

## Decisions, and why they differ from Humboldt's plan

**Deploy keys are disabled org-wide on Protocol-Institute.** `POST /repos/{repo}/keys`
returns `422 Deploy keys are disabled for this repository` for all three repos, while a
personal repo still accepts them. Humboldt's §4.2 (one deploy key per repo per service) is
therefore not available here without reversing an org policy. Chosen instead: **one
fine-grained PAT scoped to exactly the three repos, `Contents: write` and nothing else.**

This is weaker than a deploy key in two specific ways, and both are worth stating plainly
rather than glossing:

1. It is still an *account-identity* credential — pushes attribute to `vgururao`, and it
   is the account's token rather than a repository-bound one.
2. Its scope can be widened later in the GitHub UI with no signal. That is precisely how
   the original exposure happened, which is why the phase-3 assertions below are not
   optional garnish — they are the control that makes this choice safe over time.

What it does remove today is the whole of the blast radius that mattered: no admin
anywhere, no repos outside the three, no personal repos, no ability to open or merge
anything.

**PR opening moved off the VM entirely.** A contents-write token can push a branch but
cannot open a pull request. Rather than add `Pull requests: write` back onto the VM's
token, the website repo now opens the PR itself:
`Protocol-Institute/website/.github/workflows/c3po-auto-pr.yml` fires on a push to
`c3po/auto-sig-pages` and uses that workflow's own `GITHUB_TOKEN` (website PR #10). The
daemon's two `gh` calls are gone; the VM needs no GitHub API access at all.

A push to an already-open PR's branch updates it without help, so the workflow only acts
on the first push after each merge — the same condition the removed `gh pr list` was
testing for.

**A GitHub App was considered and deferred.** It is the strongest option — a real
non-human identity, per-repo installation, short-lived tokens, unaffected by the deploy-key
policy — but it means app creation, a private key on the VM, and a token-minting code path
in the daemon that can fail on its own. Revisit if a third service or a second VM needs
GitHub write; at that point the app amortises.

---

## Phase 1 — remove the org-wide exposure  ✅ executed 2026-09-09

1. Create a fine-grained PAT, resource owner **Protocol-Institute**, repository access
   limited to `c3po`, `website`, `protocolized-website`, permission **Contents:
   read and write** only (Metadata: read is implicit). Name `c3po-vm-daemon`.
2. Install it on the VM as the `gh` credential (`gh auth login --with-token`), replacing
   the account token. The https remotes and the `gh` credential helper stay as they are —
   only the scope behind them changes.
3. Verify a real push path on all three checkouts before trusting it.
4. **Revoke the old PAT** at GitHub → Settings → Developer settings → Fine-grained tokens.
   Only after step 3 passes.
5. Register the new token in [`../../admin/keys.md`](../../admin/keys.md) with the VM as
   its deployment location, per PI key policy.

Verification that the exposure is actually gone, run from the VM:

```bash
gh api repos/Protocol-Institute/humboldt --jq .full_name    # must 404
gh api repos/vgururao/venkateshrao.com --jq .full_name      # must 404
gh api repos/Protocol-Institute/c3po --jq '.permissions'    # push true, admin false
```

## Phase 2 — stop running services as `exedev`  ⬜ open

Until this is done the token is still readable by anything that executes as `exedev`, and
`exedev` has passwordless sudo. The credential is narrower; the box is not yet split.

Follows [`phase5-vm-cutover.md`](../../humboldt/plans/phase5-vm-cutover.md) §3 and §5, with
one c3po-specific finding that makes the split unusually worthwhile: **the bot needs
almost nothing.** `bin/c3po_bot.py` reads only `ORACLE_BOT_TOKEN`, `PINECONE_API_KEY`,
`PINECONE_C3PO_HOST`, `VOYAGE_API_KEY`, and reaches c3po's own answers through the public
unauthenticated `https://c3po.protocolized.io/query`. It needs no Anthropic key, no
Cloudflare credential, no `ADMIN_KEY`/`MCP_API_KEY`, and after phase 1 no GitHub
credential whatsoever — despite being the service most exposed to untrusted input.

| | daemon.env | bot.env |
|---|---|---|
| `ANTHROPIC_API_KEY` | ✓ | |
| `VOYAGE_API_KEY` | ✓ | ✓ |
| `PINECONE_API_KEY`, `PINECONE_C3PO_HOST` | ✓ | ✓ |
| `DISCORD_BOT_TOKEN`, `DISCORD_GUILD_ID`, `DISCORD_CHANNEL_IDS`, `INTRODUCTIONS_CHANNEL_ID` | ✓ | |
| `ORACLE_BOT_TOKEN` | | ✓ |
| `ORACLE_APPLICATION_ID`, `ORACLE_PUBLIC_KEY`, `ORACLE_ROLE_ID` | ✓ | |
| `ADMIN_KEY`, `MCP_API_KEY` | ✓ | |
| `CLOUDFLARE_ACCOUNT_ID`, `SUBSTACK_EXPORT_DIR` | ✓ | |
| GitHub push credential | ✓ | |

Steps:

- `c3po-daemon` and `c3po-bot` service users, no sudo, `nologin` shells; `exedev` keeps
  sudo as the operator path but runs nothing.
- Checkout moves to `/srv/c3po/repo`, group `c3po`, setgid `2775`, `core.sharedRepository
  group` — both services write the tree (the bot spools to
  `data/spool/bot_conversations/`, the daemon reads and commits it).
- Env files at `/etc/c3po/{daemon,bot}.env`, `0600`, owned per service, **outside the
  checkout** — a group-shared repo means anything inside it is readable by both, so a
  repo-root `.env` would defeat the split. systemd `EnvironmentFile=` plus dotenv's
  non-overriding `load_dotenv()` needs no code change.
- The GitHub token goes only in `daemon.env`.
- Add the systemd hardening block from §5 (`NoNewPrivileges`, `ProtectSystem=strict` +
  `ReadWritePaths`, `PrivateTmp`, `ProtectHome`, `RestrictSUIDSGID`) to both units.

Order matters: the bot's Discord gateway reconnect is the visible one, so move the daemon
first, confirm a clean cycle, then the bot.

## Phase 3 — standing assertions  ⬜ open

The point is that a credential widened later gets *caught* rather than discovered a month
on by an unrelated project. A systemd timer, daily, reporting through the existing
Telegram hook:

```bash
gh api repos/Protocol-Institute/humboldt  --jq .full_name   # must 404
gh api repos/vgururao/venkateshrao.com    --jq .full_name   # must 404
gh api repos/Protocol-Institute/c3po      --jq .permissions.admin   # must be false
ssh -o BatchMode=yes exe.dev whoami                          # must fail
```

Add one c3po-specific check the humboldt plan does not need: **token expiry**. A
fine-grained PAT expires, and the failure mode is a daemon that stops pushing silently —
the same silent-failure class as `status.md` TODO #4. `GET /` returns the token's
expiry in the `github-authentication-token-expiration` response header; warn at 14 days.

This belongs in the same reconciliation job as TODO #4 rather than as a lone timer.
