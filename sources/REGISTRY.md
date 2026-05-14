# C3PO Source Registry

Index of all corpus sources. Each source maintains its own `registry.json` with ingestion state and metadata schema. The C3PO Worker reads these at startup to build the `CORPUS_MAP` injected into the system prompt.

## Registered Sources

| Source | Namespace | Access | Cadence | Registry |
|---|---|---|---|---|
| PDF archive | `pdfs` | public | on-change | [sources/pdfs/registry.json](pdfs/registry.json) |
| Substack | `substack` | public | realtime (CF cron) | [sources/substack/registry.json](substack/registry.json) |
| YouTube | `youtube` | public | daily (CF cron) | [sources/youtube/registry.json](youtube/registry.json) · [PLAN](youtube/PLAN.md) |
| Discord | `discord` | member | daily batch | [sources/discord/registry.json](discord/registry.json) · [PLAN](discord/PLAN.md) |
| Submissions | `submissions` | public (post-review) | on-approval | [sources/submissions/registry.json](submissions/registry.json) |
| Crawl | `crawl` | public (post-review) | scheduled (CF cron) | [sources/crawl/registry.json](crawl/registry.json) |
| Case studies | `casestudies` | private | manual | [sources/casestudies/registry.json](casestudies/registry.json) |

## Adding a New Source

1. Create `sources/<type>/` directory
2. Write `registry.json` (see schema below)
3. Write `ingest.py` or `worker.js`
4. Add entry to the table above
5. Update the C3PO Worker to load the new registry at startup

## registry.json Schema

```json
{
  "source": "string — matches Pinecone namespace",
  "display_name": "string — human-readable name for CORPUS_MAP",
  "pinecone_namespace": "string",
  "vector_count": 0,
  "last_ingested": "ISO 8601 datetime or null",
  "schema_version": 1,
  "access_level": "public | member | private",
  "freshness_cadence": "realtime | daily | weekly | on-change | manual",
  "ingest_script": "path/to/ingest.py or worker.js",
  "notes": "optional string"
}
```
