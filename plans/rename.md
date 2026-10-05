# Rename plan: c3po → `<NEW>` (draft for VGR review)

Queue item #10 from session 56. VGR supplies the name later; this plan is written so that it
can run as soon as the name exists. `<NEW>` is the display name (e.g. "Foo") and `<new>` the
slug (e.g. `foo`). Inventory taken session 57 (2026-10-05).

## Principle: rename what people see, alias what they call, leave the plumbing alone

The name reaches people through about a dozen surfaces. It also appears in roughly 70 files of
plumbing (Pinecone index, KV, queue, env var names, the VM hostname, systemd units, Python
filenames) that no user ever sees. Renaming the plumbing costs real risk: a Pinecone index
cannot be renamed, only rebuilt; an env var rename means a coordinated change across the
laptop, the VM, the Worker secrets and `../admin/keys.md`. It buys nothing visible. So:

- **Tier 1, identity:** renamed, in one coordinated pass.
- **Tier 2, addresses other people have saved:** the new name is added, the old one keeps
  working indefinitely as an alias or redirect. Nothing anyone bookmarked, configured or linked
  breaks.
- **Tier 3, plumbing:** not renamed. A one-line note in `CLAUDE.md` says the internal name
  is `c3po` for historical reasons. Code identifiers change opportunistically when a file is
  touched anyway, never as a project.
- **History:** never rewritten. The devlog, `status.md`, past Discord messages, the `meta`
  and `transcripts` namespaces all say C3PO, and they should, because that is what it was called.

## Inventory

### Tier 1: identity (rename)

| Surface | Where | Notes |
|---|---|---|
| Bot persona | `api/worker.js` `SYSTEM_PROMPT`, `DISCORD_SYSTEM_PROMPT`, identity guard line (~1258), `SOUL_EXCERPT` | Add "formerly known as C3PO" so the bot does not disown its own history (the `meta` namespace, devlog and transcripts all say C3PO) |
| Web UI | `worker.js` HTML: 7 `<title>`s, headers, How It Works / Terms / Roadmap / Conversations / Corpus Status pages | ~128 `C3PO` strings in worker.js. Most are UI copy; sweep them all |
| Discord bot display name + avatar | Discord Developer Portal: the `c3po_bot` gateway app and the `c3po_oracle` interactions app | **VGR, manually.** The bot's API username change is rate-limited and needs the token; the portal is simpler. The server nickname can differ from the username |
| Bot message copy | `bin/c3po_bot.py`: docstring "Mention @c3po", turn-limit notice, intro copy | The mention works by user ID, so behaviour doesn't change |
| MCP `serverInfo.name` | `worker.js:3771` `"c3po"` | Shown in MCP clients' tool lists |
| protocol-institute.org | `js/main.js`, `js/sig-meta.js`, `programs/index.html`, `humboldt/index.html`, `monitoring/index.html`, `pitchdeck/deck.html`, one recording page; D1 `projects` row `c3po` (title, description) | Website-agent PR. The project **slug** is Tier 2 |
| protocolized.io | `src/pages/resources/index.astro`, one resource page | PR (always a PR; see memory) |
| PI GitHub org profile | `.github/profile/README.md` (2 mentions) | |
| README / ARCHITECTURE | this repo, first lines | Public repo, so a visitor's first read |

### Tier 2: addresses (add new, keep old)

| Address | Who depends on it | Plan |
|---|---|---|
| `c3po.protocolized.io` | Web users, MCP clients configured by third parties, the VM bot (`WORKER_URL`), Humboldt (`humboldt-site/functions/chat.js`, `agent/retrieval.py`), the website | Add `<new>.protocolized.io` as a **second custom domain on the same Worker**. Both serve. Move our own callers to the new one. Keep the old one indefinitely: removing it buys nothing and breaks unknown MCP configs |
| MCP tool `ask_c3po` | Third-party MCP clients | Register `ask_<new>` and keep `ask_c3po` as a hidden alias that dispatches to the same handler. `search_corpus` has no name in it |
| GitHub `Protocol-Institute/c3po` | Clones (laptop, VM), `website/.github/workflows/c3po-auto-pr.yml`, PATs scoped to this repo | GitHub redirects renamed repos. **Check fine-grained PAT scoping before renaming:** the VM's tokens are scoped by repo and expire 2026-12-08, and they should follow the rename, but verify with a push from the VM right after. Update remotes explicitly anyway; redirects are a fallback, not a plan |
| Devlog `protocolized.io/resources/c3po-devlog` | Links in Discord, `#session-N` anchors in `meta` vectors | New slug `<new>-devlog` plus a redirect from the old one. Do this last, or not at all: the anchors in 58 `meta` vectors point at the old URL |
| Website project page `/projects/c3po` | Links from programs, Intelligence Media SIG | Slug change needs a redirect; the title change alone is cheap. Website agent's call |

### Tier 3: plumbing (do not rename)

Worker name `c3po` (wrangler), KV `C3PO_KV`, queue `c3po-oracle`, Pinecone index `c3po` and
env `PINECONE_C3PO_HOST`, `C3PO_VM_GH_TOKEN` / `C3PO_ACTIONS_GH_TOKEN`, VM `c3po-vm.exe.xyz`,
`~/c3po` on the VM, systemd `c3po-daemon` / `c3po-bot`, bot ids `c3po_listener` / `c3po_bot`
in `config/bot_registry.json`, `bin/c3po_bot.py`, `C3PO_ROOT` in protocolized-website's sync
scripts, `website/.github/workflows/c3po-auto-pr.yml`, Humboldt's reads of the c3po index,
`admin/keys.md` rows.

### The local folder: a trap

Renaming `Code/protocol-institute/c3po/` on the laptop looks cosmetic but breaks four things:
the `.venv` (absolute paths baked in, so it must be recreated); protocolized-website's
`C3PO_ROOT` default (`REPO_ROOT.parent / "c3po"`); `Code/CLAUDE.md` and sibling docs; and
**Claude Code's memory for this project**, which is keyed on the folder path
(`~/.claude/projects/-Users-Venkat-Dropbox-Code-protocol-institute-c3po/`), so it would silently
start empty. **Recommendation: keep the folder name.** If it must change: move the memory
directory to match, recreate the venv, set `C3PO_ROOT`, and update `Code/CLAUDE.md`.

## Sequence (once the name exists)

0. **Name checks:** `<new>.protocolized.io` is free (our zone); GitHub org repo name free;
   Discord username available; a web search for collisions with other AI products.
1. **Domain:** add `<new>.protocolized.io` to the Worker; verify both serve. *(laptop, 10 min)*
2. **Worker identity:** persona prompts with the "formerly C3PO" line, UI strings, `serverInfo`,
   `ask_<new>` plus the `ask_c3po` alias. Deploy; probe that the bot answers "what is C3PO?" and
   "what is `<NEW>`?" as the same thing. *(one commit)*
3. **Discord:** VGR renames both apps in the Developer Portal and sets the avatar. Bot copy
   change in `c3po_bot.py`; `WORKER_URL` moves to the new domain; restart `c3po-bot` on the VM.
4. **GitHub repo rename;** update remotes on laptop and VM; verify the VM daemon's next push lands
   (watch for the PAT-scope surprise).
5. **Website + protocolized.io PRs** (via their agents, as PRs). Humboldt switches to the new
   domain on its next session.
6. **Docs:** README, ARCHITECTURE, `CLAUDE.md` (one "internal name" line), `Code/CLAUDE.md`
   project table, the devlog entry announcing the rename.
7. **Optional, later:** devlog slug plus redirect.

Steps 1-2 are reversible in minutes. Step 4 is the only one that touches credentials.

## Decisions for VGR

1. The name, its exact casing, and whether it has a punctuated form (as "C-3PO" did).
2. Keep `c3po.protocolized.io` serving indefinitely? (Proposed: yes.)
3. Keep the laptop folder name? (Proposed: yes.)
4. Does the bot acknowledge the old name? (Proposed: yes, one line, "formerly C3PO", so its
   own archive stays coherent.)
