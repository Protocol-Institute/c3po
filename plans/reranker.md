# Reranker — plan (draft for VGR review)

Source: [issue #10](https://github.com/Protocol-Institute/c3po/issues/10) (filed by Allethrin, 2026-10-05,
after the Discord thread on rerankers). Plan drafted session 57.

## What the issue proposes

Put a cross-encoder rerank between `mergeResults()`'s merge and its slice. Do it first on `search_corpus`
(no LLM in the path, so before/after is directly comparable), behind an optional `rerank` argument, falling
back to cosine order on failure.

## What checked out (session 57)

| Claim | Status |
|---|---|
| `namespace: "all"` pool is fixed-width: 5/ns, 4 bibliography, 3 meta → ≤47 candidates | ✅ `worker.js` `runMcpSearch` |
| Pool is ordered by cosine × tier weight, then sliced to `limit` / `MAX_SOURCES` | ✅ `mergeResults()` |
| Reranking the merged pool adds no Pinecone egress | ✅ it reorders what is already fetched |
| Voyage rerank works on the worker's existing `VOYAGE_API_KEY` | ✅ probed `rerank-3`, `rerank-3-lite`, `rerank-2.5`, `rerank-2.5-lite`: all 200, 0.12-0.33s for 3 docs |
| **Lexicon hits return no text** | ✅ **confirmed, and it is a bug independent of reranking.** `definitions` metadata keys are `chunk_type, definition_index, source, source_slug, term, triage, triage_reason, variant_count`. No definition, no text. `normalizeDefinition()` reads `m.definition \|\| m.text` and gets `""` |
| Same passage under two URLs both survive | Plausible. Dedup is by `docId` only. Not reproduced yet |
| Pricing ($0.05 / $0.02 per M tokens, 200M free) | Not verified. Check Voyage's pricing page before Phase 1 ships |

The empty lexicon excerpts are worse than the issue says. On the **answer path** they go into
`buildContextBlock()` as `[LEXICON — "term" — PI-coined]` with nothing under it, using one of the 8
source slots. In the issue's examples, 5 of the 20 results were bare terms.

## Phases

### Phase 0: fix what the reranker would otherwise inherit (do first, independently useful)

1. **Lexicon text.** `ingest/sync_lexicon.py` should store the definition in metadata. That is a
   metadata update on 560 vectors: no re-embed and no Voyage cost, and the embedding text is
   unchanged. Then confirm `search_corpus` returns definitions with excerpts.
2. **Empty-excerpt guard.** In `mergeResults()`, drop items whose `excerpt` is empty before the
   slice, so no future metadata gap can take a source slot silently.
3. **Text-level near-dup collapse.** After merge, collapse items whose normalized first ~300 chars
   match, keeping the higher-weighted one (the gitbook / summerofprotocols "Sufficiently Mortal"
   pair). A reranker pulls near-duplicates up together, so this matters more once it lands.
4. **Probe set: `bin/probe_rerank.py`.** Per [[feedback_save_probe_sets]]: the issue's 5 queries
   plus ~10 of ours (symposium, SIG, person-named, definitional, off-corpus). For each query the
   script prints the cosine order and the reranked order side by side, with scores, from a
   **local** rerank of the live `search_corpus` pool, so the decision rests on our own data before
   any worker change.

### Phase 1: `search_corpus` only, opt-in

- Split `mergeResults()` into `mergePool()`, which returns the weighted and deduped pool, and the
  existing slice. All 4 call sites keep today's behaviour. (`mergeResults` has 4 call sites, and a
  signature change has broken the MCP one before; see session 54.)
- `rerankItems(query, items, env)`:
  - POST `https://api.voyageai.com/v1/rerank`. Each document is the title, a blank line, then
    `excerpt[:2000]`. Excerpts go up to 6,000 chars since session 55's cap change, and tokens scale
    with length.
  - `AbortController` timeout of about 1.5s. On any failure, return cosine order and set
    `rerank: "failed"` in the response, never an error.
- `search_corpus` gets `rerank: boolean` (default **false** in Phase 1). The response carries the
  `rerank_score` per item so callers can see the "1 good match and 19 neighbours" signal the issue
  describes.
- Cost and accounting: a `PRICE_VOYAGE_RERANK` constant and a `trackCost`-style counter. This path
  is open and unauthenticated. The existing 100/day/IP limit bounds it, but rerank tokens are about
  300× an embed (~25K versus ~80), so they need their own line in `/stats`.

### Phase 2: the answer path (`/query`, Discord, `ask_c3po`). Only if Phase 1 probes show a clear win

The answer path is where the gain is, because a better top 8 means a better answer. It also has
interactions `search_corpus` does not:

- **Symposium scoping and pinning.** Pinned workshops (plural list questions) must stay pinned.
  Rerank only the unpinned remainder. The per-record chunk cap applies after the rerank.
- **Transcript cache.** The 0.52 / 0.60 thresholds stay on cosine (the issue is right). Extract
  cache hits before the rerank.
- **Intro handler.** `c3po_bot.py` takes its "Suggested reading" and "Worth watching" links from
  `sources` order, so reranking changes intro recommendations too. Watch `intro_quality_log`.
- **Latency.** About +0.2-1s on a path that already waits for Claude. Acceptable, but measure it.
- **Prompt caching.** No effect: the rerank changes the user message (excerpts), not `SYSTEM_PROMPT`.

### Phase 3: tuning (each one is a separate decision, made on probe data)

- **Pool width.** `TOP_K_EACH` 5 → 8 gives the reranker more to work with, but it is the egress cost
  session 49 trimmed. Measure with the existing egress accounting before and after. Query results are
  cached for 6h, which softens it.
- **Instruction.** `rerank-2.5` accepts an instruction ("relevant = PI's own research on protocols…").
  Test it on the probe set.
- **Minimum score.** Off at first, as the issue suggests. Log rerank scores per query for a few
  weeks and set any floor from that distribution, not from another corpus's numbers.

## Decisions for VGR

1. **Model.** `rerank-2.5` (instruction-capable) or `rerank-3`. Proposal: let the Phase 0 probe decide.
2. **Combining with tier weights.** Proposal: `rerank_score × tier_weight` to start. The tier weights
   encode editorial priority (PI primary > community), which a reranker knows nothing about. They were
   tuned against cosine's narrow band (~0.3-0.6), and rerank scores spread over 0-1, so the weights
   will have *more* effect than before. The probe should show both orderings.
3. **Phase 1 default.** Opt-in (proposed) or on by default for `search_corpus`.
4. **Issue reply.** Acknowledge on #10 with what checked out, and split the lexicon bug into its own
   issue (it ships in Phase 0 regardless)?

## Not in scope

Changing the embedding model; hybrid / BM25 retrieval; reranking inside individual namespace queries.
