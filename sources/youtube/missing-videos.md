# YouTube Videos Missing from c3po Corpus

These 6 videos exist as resources in `protocolized-website` but are not in any of the
playlists tracked by `fetch_youtube_meta.py`, so they were never ingested into the
`videos` Pinecone namespace. They need to be added to the playlist map (or ingested
individually) and then enriched and embedded.

---

## Videos to Ingest

| Slug (protocolized-website) | Title | Video ID | Date |
|---|---|---|---|
| `atoms-institutions-blockchains` | Atoms, Institutions, Blockchains | `c-HKt0XhxSE` | 2024-04-17 |
| `punk-folk-myth-protocols-sketching-the-socioimaginational` | Punk, Folk, Myth, Protocols: Sketching The Socioimaginational | `2IyKlJR56Ww` | 2024-07-10 |
| `scaling-bitcoin-the-rise-of-the-lightning-network` | Scaling Bitcoin: The Rise of the Lightning Network | `Te-q8oxW35I` | 2024-04-10 |
| `seeing-scp-as-a-narrative-protocol` | Seeing SCP as a Narrative Protocol | `Ejuwkc51gR4` | 2023-11-08 |
| `summer-of-protocols-office-hours-0` | Summer of Protocols Office Hours 0 | `OjYRYH3oRJk` | 2023-03-20 |
| `summer-of-protocols-town-hall` | Summer of Protocols Town Hall | `vCG1N3Ww6T0` | 2023-03-13 |

The two 2023 entries (office hours, town hall) predate the tracked playlists and are
likely ungrouped or from a different era of the channel. The other four are substantive
guest talks that appear to be published on the PI channel but not assigned to any
tracked playlist.

---

## How to Ingest

**Option A — Add to an existing playlist in `fetch_youtube_meta.py`:**
If any of these belong to a series (e.g. guest-talks), add their playlist to the
`PLAYLISTS` dict and re-run `fetch_youtube_meta.py`. They'll be picked up automatically.

**Option B — Ingest individually** (quicker for one-offs):

```bash
source .venv/bin/activate

# 1. Fetch captions for each video
for VID in c-HKt0XhxSE 2IyKlJR56Ww Te-q8oxW35I Ejuwkc51gR4 OjYRYH3oRJk vCG1N3Ww6T0; do
    python3 ingest/fetch_youtube_meta.py --video $VID
done

# 2. Enrich with Haiku (summary, categories, speakers, key_concepts)
for VID in c-HKt0XhxSE 2IyKlJR56Ww Te-q8oxW35I Ejuwkc51gR4 OjYRYH3oRJk vCG1N3Ww6T0; do
    python3 ingest/enrich_youtube.py --video $VID
done

# 3. Embed and upsert to Pinecone videos namespace
python3 ingest/ingest_youtube.py
```

Then run `sync-youtube-resources.py` in `protocolized-website` to pull the enrichment
back into the resource Markdown files and D1.

---

## Series Assignment

Suggested `series` values for `video_meta.json` if ingested individually:

| Video ID | Suggested series |
|---|---|
| `c-HKt0XhxSE` | `guest-talks` |
| `2IyKlJR56Ww` | `guest-talks` |
| `Te-q8oxW35I` | `guest-talks` |
| `Ejuwkc51gR4` | `guest-talks` |
| `OjYRYH3oRJk` | `town-hall` |
| `vCG1N3Ww6T0` | `town-hall` |
