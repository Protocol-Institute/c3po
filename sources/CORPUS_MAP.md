# C3PO Corpus Map

Structural information about the Protocolized corpus: sources, roles, extended universe plans,
and data source roadmap. Update this when publication structure changes or new sources are confirmed.

Last updated: 2026-05-14

---

## Publication Structure

**Protocolized** (`protocolized.summerofprotocols.com`)

| Role | Person |
|---|---|
| Editor / Founder | Venkatesh Rao |
| Co-editor | Timber Stinson-Schroff |

Timber is co-editor and a frequent contributor (editorial posts, SIG reports, and SOP coverage).
He appears in bylines as "Timber Stinson-Schroff". Do not treat him as a guest contributor —
his editorial posts (`brackish-strategy`, `how-to-protocol-watch`, `lessons-from-the-librarians`,
`one-tension-to-rule-them-all`, etc.) are authoritative editorial voice, not guest pieces.

**Author handle resolution (byline → real name):**

| Byline | Real name | Notes |
|---|---|---|
| Timber Stinson-Schroff | Timber Stinson-Schroff | Co-editor |
| Venkatesh Rao | Venkatesh Rao | Editor |
| Sachin | Sachin Benny | Prolific contributor, co-author of multiple series |
| Thing Party | Elizabeth Maher | Pseudonym; author of T.R.O.(L.L.) universe |
| Spencer Nitkey - Writer | Spencer Nitkey | Zoothesia series author |
| Marie-Hélène Lebeault - Author | Marie-Hélène Lebeault | |
| TΞRM1NΞX | TΞRM1NΞX | AI-assisted author persona (Entropic Gate series) |
| rafa | Rafael Fernandez | |
| Amita | Amita | |
| Kei Kreutler | Kei Kreutler | Memory Research Group lead |
| Protocolized | Protocolized | Publication byline — not a person |

---

## Extended Universe Project (planned, ~late 2026)

Several Protocolized fiction contributors are building a **shared extended universe** that will
eventually become an off-Substack standalone project (separate website/publication).

**Confirmed universe contributors:**
- **Spencer Nitkey** — Zoothesia series (6 posts, ongoing on Substack; forms a continuous narrative)
- **Sachin Benny** — UE-T1 Train Series (signals-in-the-margins, the-flesh-perfected-is-the-flesh, the-headless-empire; explicitly a serialized world-building project per enrichment)
- **Elizabeth Maher** (Thing Party) — T.R.O.(L.L.) universe (troll, all-you-can-do-here-is-leave; building a character and world across contest entries)
- **Randy Lubin** — Caduceus City (caduceus-city; described as a contest-winning story that seeds a new series)

**When this project launches:**
- Add a new data source: `sources/extended-universe/`
- Create a new Pinecone namespace: `extended-universe` (or extend `substack` with `collection_universe` metadata)
- These Substack posts are seeds — update their metadata to include `universe: "extended-universe"` field when the project is formalized
- Consider cross-namespace retrieval for queries that span both Substack and extended universe content

**Current status:** All four are publishing seeds on Protocolized. No off-Substack presence yet.
Watch for announcement in the editorial posts or Timber's updates (~Q3-Q4 2026 estimate).

---

## Current Data Sources

| Source | Namespace | Status | Notes |
|---|---|---|---|
| Protocolized Substack | `substack` | ✅ Active | 116 posts ingested; API sync planned |
| Protocol Institute PDFs | `pdfs` | Partially ingested | Resources in `data/pdfs/` |
| Discord | `discord` | Planned | |
| YouTube | `youtube` | Planned | |
| Google Drive | `drive` | Planned | |
| Extended Universe | — | ~Q4 2026 | Off-Substack fiction project (see above) |

---

## Substack Collection Structure

See `sources/substack/collections.json` for the authoritative collection/series/SIG membership.

Quick reference:

| Collection | Type | Tag | Posts |
|---|---|---|---|
| Terminological Twists | fiction-contest | terminological-twists | 8 |
| Ghosts in Machines | fiction-contest | ghosts-in-machines | 8 |
| The Librarians | fiction-contest | the-librarians | 7 |
| Building and Burning Bridges | fiction-contest | building-and-burning-bridges | 3 |
| Zoothesia | fiction-series | zoothesia | 6 |
| Bridge Atlas | interview-series | bridge-atlas | 7 |
| Protocols for the Long Now | series | protocols-for-the-long-now | 1 |
| UE-T1 Train Series | proto-collection | (none yet) | 3 |
| SIGFPT | sig | sigfpt | 4 |
| SIGBIZ | sig | sigbiz | 4 |
| SIGMEM | sig | sigmem | 4 |
| SIGFIC | sig | sigfic | 2 |
| Obliquities | editorial-column | obliquities | 3 |

**Substack sections** (readers can subscribe to sections independently):

| Section name | section_id | Post count | Description |
|---|---|---|---|
| Protocolized | (none/default) | 5 | Catch-all default section |
| Fictions | 333105 | 58 | Protocol fiction: stories, serials, contest entries |
| Articles | 333110 | 47 | Essays, case studies, research, SIG reports |
| Obliquities | 333103 | 5 | Venkatesh Rao's editorial column |

Stored per-post in `section_name` metadata field. Source: `section_id` from Substack API.

**Publication categories** (higher-order metadata on posts, not collection membership):
Stories, Technology, Fiction, Culture, Education, Philosophy, Design, Art & Illustration,
Studies, Literature, Crypto, Business, International, History, Health & Wellness, Finance, Science.
These are Protocolized's own category choices applied per-post.
Stored per-post in `substack_categories` metadata field.

---

## Notes on Structural Changes to Track

- **New fiction contests:** Each contest generates a new collection. Watch for contest announcements
  in editorial posts (typically tagged `announcement`). Add to `collections.json` when posts appear.
- **New SIGs:** Protocolized incubates SIGs through Summer of Protocols. New SIGs get their own tag
  when they launch. Update `collections.json` and add a new SIG entry.
- **Extended universe formalization:** When off-Substack site launches, update this doc and add
  data source config in `sources/extended-universe/`.
- **New editors:** If editorial team expands, add to the Author handle resolution table above.
