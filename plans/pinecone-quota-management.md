# Pinecone Quota Management

**Cross-project policy:** see
[`../../admin/sop-pinecone-quota-management.md`](../../admin/sop-pinecone-quota-management.md)
— required components, what not to do, and why this is a shared doc rather than a shared
library across `c3po`/`humboldt`/future bots on the same Pinecone account. This file is
c3po's own history and implementation status against that policy.

## History (condensed from `status.md` sessions 44–49)

**Chapter 1 — write/read-unit exhaustion (2026-07-21 to 2026-08-01).** Root cause was
humboldt's `ingest_all()` re-embedding its entire corpus on every notebook update, burning
the shared account's write cap; c3po's own read-unit cap was independently found exhausted
too. Fixed: humboldt's ingest made content-hash incremental
([`Protocol-Institute/humboldt#1`](https://github.com/Protocol-Institute/humboldt/pull/1));
c3po built its first pause/resume mechanism (`ingest/utils.py` `_GuardedIndex`,
`ingest/ingestion_control.py`); the live Worker's silent-degradation bug fixed (now sets
`degraded: true` instead of answering confidently with `sources: []`). Confirmed held
through the 2026-08-01 reset.

**Chapter 2 — egress discovered as a 4th, separate quota (2026-08-13).** 1GB/month cap, not
shown on Pinecone's main Quotas page. Root cause: `fetch_discord_links.py`'s
`harvest_urls_from_namespace()` doing a full `idx.fetch()`+`idx.list()` over 12,873 vectors
every 30-min daemon cycle forever, plus `rebuild_sig_summaries.py` doing the same over ~124
vectors. Fixed with persisted per-namespace harvest state and a `describe_index_stats()`
precheck (control-plane, exempt from egress) to skip the full scan when nothing changed.
Also generalized the auto-pause regex, which previously only recognized "write unit
limit"/"read unit limit" and let the daemon retry blind against the new egress 429 for 3+
days.

**Chapter 3 — the real driver was fan-out, not volume (2026-08-17).** Traced every read call
site: each user interaction fanned out to 9-11 Pinecone `query()` calls (one per namespace)
in `api/worker.js`, and humboldt was independently querying c3po's live index directly on
every Discord reply it composed. Mitigated by trimming `TOP_K_EACH` 8→5 across all fan-out
sites and humboldt's own namespace breadth 7→5. Deferred at the time: result caching (the
bigger lever) and local egress accounting — both open TODOs carried into this session.

**2026-09-01 — egress quota reset confirmed** via a raw REST `POST /query` bypassing our own
code (not just trusting the computed resume date), and the chapter-3 trims held for the full
15-day window without re-exhausting.

**Same session — audited for duplication against humboldt** (which had independently built
and shipped a more complete version of exactly the two deferred items — see
`humboldt/plans/read-outage-2026-08.md`) and decided to document the shared pattern rather
than extract a library (see the SOP linked above for the reasoning), then closed the gap in
c3po's own Worker.

## Current implementation status

| Component (SOP §2) | Python (`ingest/`) | Worker (`api/worker.js`) |
|---|---|---|
| Typed failure at a chokepoint | ✅ `IngestionPaused`, raised by `_GuardedIndex` before any write/read-touching call | ⚠️ partial — `degraded: true` flag on the response, not a raised/typed exception; acceptable for an HTTP API shape but callers must check the flag explicitly |
| Auto-tripping breaker | ✅ `ingestion_control.py`, independent write/read pause, self-clearing | ➖ none — the Worker has no equivalent local pause state; it just retries every request and reports `degraded` per-request. Not yet a gap that's bitten us (Pinecone itself is the rate limiter), but worth a TODO if 429 volume ever gets high enough to matter for Worker CPU/latency |
| Per-call-site disclosure | ✅ `daemon.py` skips write/read-touching steps entirely while paused | ✅ all 4 fan-out sites (`runMcpSearch`, `runMcpAsk`, `runRagQuery`, the `/search` route) surface `degraded`/`note` |
| Right-sized `top_k` | ✅ `TOP_K_EACH` 8→5 (session 49) | (same code) |
| Result caching | ➖ not built (c3po's Python scripts are mostly incremental-write jobs, not the fan-out driver) | ✅ **built this session** — KV-keyed cache in `queryNamespace()`, keyed on a hash of the embedding vector + namespace + top_k + filter (not query text — see note below), 6h TTL, never caches a failed/degraded response |
| Egress accounting | ➖ not built | ✅ **built this session** — `trackEgress()`/`totalEgressBytes()`/`anyCached()`, one KV read-modify-write per request (aggregated across the parallel namespace fan-out to avoid a race on the shared monthly counter), surfaced in `GET /stats` as `pinecone_egress` |
| Proactive alerting (~70% of cap) | ➖ not built | ➖ not built — `/stats` reports `over_warn_threshold` but nothing pushes it anywhere (no DM/Telegram hook yet, unlike humboldt's daemon-side watcher) |

**Design note on the cache key:** humboldt's Python cache is keyed on the raw query text,
which lets a full cache hit skip the Voyage embedding call too. c3po's Worker cache is keyed
on a hash of the already-computed embedding vector instead, because `queryNamespace()` never
receives the raw query text — only the vector — and threading query text through all ~40
call sites was judged not worth it for what it saves (the Voyage call is small relative to
Pinecone egress). Practical effect: an exact-repeat question still hits the cache (Voyage
embeddings are deterministic for identical input), but two differently-phrased questions
with the same intent won't share an entry the way a text-keyed cache would. Revisit if
cache-hit rates turn out lower than expected.

## Deploy verification (2026-09-01)

Deployed (`wrangler deploy`, version `f14011da`). Confirmed live: `/health` and `/stats`
respond normally; a real `/query` call returns a non-degraded, sourced answer; a repeated
identical question hit the cache (`cache_hit_requests` incremented, `saved_bytes` > 0); a
sequence of fresh unique queries accumulated `pinecone_egress.estimated_bytes` correctly
across requests; no errors in `wrangler tail` during any of this.

**Known limitation observed, not a bug:** the very first two-call test (same query, ~30s
apart) showed the KV counter's byte total get clobbered rather than added — consistent with
Cloudflare KV's documented eventual consistency (writes can take up to ~60s to propagate
across edge locations), which the per-request aggregation in `trackEgress()` cannot fully
close since it only prevents the *intra-request* race across parallel namespace calls, not
an *inter-request* one on a fast enough cadence. A later, more spaced-out test accumulated
correctly. This is the same "estimate, not a billing reconciliation" caveat already
documented for this feature (see SOP §2 point 4) — under concurrent/rapid-fire traffic the
counter can undercount; treat `/stats` → `pinecone_egress` as a trend indicator, not an
exact figure, same as intended.

## Remaining TODOs

1. **Watch `/stats` → `pinecone_egress` for a full month** before trusting that the cache +
   accounting combination keeps egress comfortably under budget — this is the first time
   either has run against live traffic.
2. **Proactive alerting** — extend the existing Telegram hook (already wired for the cost
   circuit breaker) to fire once when `pinecone_egress.over_warn_threshold` flips true,
   mirroring humboldt's `task_read_budget_watch`. Not yet built.
3. **Route humboldt's reply-composition retrieval through `mode="worker"`** instead of
   direct Pinecone, so it benefits from this session's Worker-side cache too instead of
   needing separate tuning in both repos (carried from session 49).
4. If cache-hit rates under real traffic are disappointing, consider the text-keyed
   alternative noted above, or explicitly measure the gap before investing in it.
5. Port c3po's more general quota-name regex into humboldt's `read_budget.py`/`chat.js` (see
   SOP §4 "known gap") — humboldt's repo, flag it there rather than editing directly.
