# C3PO ↔ Website Interface Plan

**Status:** Design approved; not yet implemented.  
**Problem this solves:** c3po currently generates full HTML pages for SIG archives, including nav and footer. When the website's nav changes, c3po's generator clobbers it on the next run — as happened repeatedly with the DRG page.

---

## Division of Labor

| Concern | Owned by |
|---|---|
| HTML structure, nav, CSS, page layout | **website** |
| Discord ingestion, Claude summarization | **c3po** |
| Structured content records (meeting JSON) | **c3po** produces, **website** consumes |
| Rendering content records to HTML | **website** |
| Ingesting rendered pages back to Pinecone | **c3po** (after website renders) |

c3po's output boundary is JSON. It never writes HTML or touches page structure.

---

## The Handoff Location

```
website/data/sigs/meetings/{sig_slug}/{date}-{thread_id}.json
```

c3po writes meeting JSON here (directly into the website repo's `data/` tree, as it already does for HTML). The website's renderer reads from this location. The directory is tracked in git so the website repo is self-contained and deployable without needing c3po present.

Naming: `{date}-{thread_id}.json` (e.g. `2026-06-11-1514625033601814528.json`). If date is unknown, omit the date prefix.

---

## JSON Schema (stable contract)

```json
{
  "thread_id": "string — Discord thread snowflake",
  "sig":       "string — SIG key (SIGFPT | MRG | SIGPfB | ProtFiSIG | SIGPSY | DRG)",
  "title":     "string — meeting title",
  "date":      "string — YYYY-MM-DD or empty",
  "topics":    ["string"],
  "summary":   "string — multi-paragraph prose, paragraphs separated by \\n\\n",
  "key_insights": ["string"],
  "participants": ["string — Discord usernames"],
  "links":     ["string — URLs (substantive only; no Discord/Drive/calendar)"],
  "discord_url": "string — link to Discord thread"
}
```

Fields `sig_name`, `channel_id`, `all_urls` are c3po-internal and not part of the handoff schema. The website renderer ignores unknown fields.

**Schema changes require coordination:** bump a `schema_version` field and update both the c3po writer and the website renderer together.

---

## What Changes in c3po

### `generate_sig_pages.py` → refactor to `export_sig_content.py`

Remove all HTML generation. The script's new job:

1. Load meeting JSON from `data/sigs/meetings/*.json`
2. Rename/copy to the handoff location: `../website/data/sigs/meetings/{sig_slug}/{date}-{thread_id}.json`
3. Validate schema (required fields present, date format correct)
4. Print a summary of what was exported

The `SIG_INFO` dict should be slimmed to only c3po-relevant fields (`slug`, `channel_id`). Remove `description`, `lead`, `schedule` — those move to `website/data/sigs.json`.

### `rebuild_sig_summaries.py`

No change needed. This script produces the source meeting JSON in `data/sigs/meetings/` — still c3po's job.

### `sync_sig_pages.py`

No change needed. This script ingests the rendered HTML pages from the website back into Pinecone. It runs after the website renderer has already written the HTML, so the loop is:

```
c3po generates JSON
  → c3po exports JSON to website/data/sigs/meetings/
  → website renders HTML to sigs/{slug}/index.html
  → c3po ingests rendered HTML → Pinecone (sig namespace)
```

---

## What Changes in the Website

### New: `website/data/sigs.json`

Authoritative SIG metadata. Move here from c3po's `SIG_INFO`:

```json
{
  "SIGFPT": {
    "slug": "sigfpt",
    "name": "Formal Protocol Theory",
    "description": "...",
    "lead": "Venkatesh Rao and Patrick Nast",
    "schedule": "Biweekly Fridays, 10am Pacific"
  },
  ...
}
```

### New: `website/scripts/render_sig_pages.py`

Reads `data/sigs.json` and `data/sigs/meetings/**/*.json`, writes `sigs/{slug}/index.html`.

This script is the website's rendering tool. It:
- Uses `sigs/CONVENTIONS.md` as the spec for page structure
- Generates the same HTML structure as the current c3po generator produces
- Never writes nav or footer — uses `<header id="site-header"></header>` / `<footer class="site-footer"></footer>`
- Is idempotent (safe to re-run)
- Uses absolute paths for all asset references (`/css/style.css`, `/js/main.js`, etc.)

Run after new meeting JSON arrives:

```bash
python3 scripts/render_sig_pages.py
# or for a single SIG:
python3 scripts/render_sig_pages.py --sig sigfpt
```

### No other website changes needed

The individual session page format (`sigs/{slug}/{date}-{title}/index.html`, per CONVENTIONS.md) can be added as a second output target in `render_sig_pages.py` in a future iteration. The monolithic SIG archive page (`sigs/{slug}/index.html`) is the first target.

---

## Future Content Areas

The same interface pattern applies to other content areas c3po might supply to the website:

| Content area | c3po writes | Website reads |
|---|---|---|
| SIG meeting summaries | `data/sigs/meetings/` JSON | `render_sig_pages.py` |
| Cogergo writeups (future) | `data/cogergo/` JSON | website renderer |
| Event summaries (future) | `data/events/` JSON | website renderer |

In each case: c3po owns the ingestion + summarization pipeline; the website owns the rendering. The JSON files are committed to the website repo and serve as the permanent record.

---

## Implementation Order

1. **Create `website/data/sigs.json`** — move SIG metadata from c3po's `SIG_INFO`
2. **Write `website/scripts/render_sig_pages.py`** — port the rendering logic from c3po's generator, using absolute paths and injected nav/footer
3. **Test**: run the script, verify output matches current pages, diff and confirm
4. **Refactor c3po**: slim `generate_sig_pages.py` to `export_sig_content.py` (JSON copy + validation only)
5. **Verify end-to-end**: new meeting → c3po summary → JSON → export → render → Pinecone ingest
