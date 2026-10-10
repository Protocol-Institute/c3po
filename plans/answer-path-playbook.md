# Answer-path playbook: speed, model, time and event awareness for RAG bots

**For:** the other bots built on the same pattern as PIBot: the Ribbonfarm oracle (`vgr_zirp`,
code in `Publishing/ribbonfarm_site/workers/oracle/`), the Mixture-of-VGRs oracle
(`Publishing/mixture-of-vgrs/workers/oracle/`, chat.venkateshrao.com), and Humboldt's web chat
(`protocol-institute/humboldt/humboldt-site/functions/chat.js`). All three are, as of
2026-10-09, a Cloudflare Worker (or Pages Function) that embeds the question, queries Pinecone
namespaces in parallel, and asks `claude-sonnet-4-6` for a non-streamed answer. PIBot was the
same until session 58. This file records what changed, what it measured, and how to
replicate it, with the PIBot code and commits to copy from.

**Source of truth:** this file, in the public `Protocol-Institute/pibot` repo
(https://github.com/Protocol-Institute/pibot/blob/main/plans/answer-path-playbook.md). Each
item names the PIBot commit and the place in `api/worker.js` to read. Copy the pattern, not
the file: the four bots differ in persona, namespaces and routing.

Do the items in order. 1-3 are independent of each other and each one is a few hours; 4-6
are larger and only worth it where the bot's corpus has dates and events.

---

## 0. Measure first: build a saved probe and an A/B harness

Every change below was checked against a saved, runnable probe, never against ad-hoc questions.

- **A probe script** (PIBot: `bin/probe_event_scope.py`): a list of questions, each with what
  the live worker must do: which sources may come back (`scoped_to`, `min_namespaces`,
  `dates_within`), and what the answer must or must not say (`answer_has_any`,
  `answer_lacks`: e.g. no calls to action, no invented dates). Exit non-zero on any failure.
- **Make it fast** (`782922f`): a case that checks only sources runs with
  `{"mode": "sources"}` (retrieval only, ~1 s, no model cost); only answer-text checks pay for
  an answer. Run cases in parallel (6). PIBot's 29 cases went from ~10 min to 43 s.
- **Admin conveniences on the worker, all gated on the admin key** (never on anything a
  public caller can send):
  - skip the per-IP rate limit (a full probe exceeds 20/hour);
  - `?now=ISO` to replay any day (for time/event features);
  - `variant` to pick an answer-model variant (for A/B);
  - report `_model` and `_usage` in the JSON response;
  - **keep admin requests out of the query log**, or test traffic pollutes whatever the log
    is used for (PIBot: reranker calibration data).
- **Send a real User-Agent** from Python: Cloudflare returns error 1010 to urllib's default.
- **Also an offline test** for pure logic (PIBot: `bin/test_event_scope.js` loads `worker.js`
  into Node's `vm` with `export default` rewritten, and calls functions directly). Free and
  instant; use it for parsers and decision rules.

## 1. Put the current date in the user message (session 54)

The bot had no idea what day it was: the system prompt is a static, cached template and the
user message was just the question plus excerpts. Add a line such as
`Current date and time: Thursday, October 9, 2026, 21:00 UTC.` to the **user message**,
never the system prompt: the system prompt carries `cache_control`, and a per-request value
there misses the prompt cache on every request (the MoV oracle has the related bug of
excerpts inside the cached system block; see `Code/devops/status.md`). Add one system-prompt
paragraph telling the model to use that line for anything about recency or upcoming/past,
and not to infer the date from the corpus. PIBot: `currentTimeLine()` in `api/worker.js`.

## 2. Stream the web answer (`0d6978c`)

**Measured:** a 472-word answer showed its first words at **3.6 s instead of ~21 s**. Total
time is unchanged; the wait people see is what changes.

- **Worker.** Accept `stream: true` on the query endpoint. Run retrieval as before, then call
  the Messages API with `"stream": true` and return a `text/event-stream` response that
  relays it as three event types: `meta` (sources, degraded flag; first), `delta` (`{text}`
  per chunk), `done` (or `error`). Parse the upstream SSE by splitting on blank lines and
  forwarding `content_block_delta` / `text_delta` only. PIBot: `relayAnswerStream()`.
- **Accounting moves to the end of the stream.** Usage arrives in `message_start` (input and
  cache tokens) and `message_delta` (output tokens); merge them and run the cost tracker and
  the query log inside `ctx.waitUntil` after the last event. Anything the non-streamed path
  did after receiving the answer (degraded notice, logging) must happen here too.
- **Errors stay JSON.** Rate limits, the budget circuit breaker, bad input: return the same
  JSON as before, before any streaming starts. The page checks the response
  `Content-Type` and falls back to the old path.
- **Page.** Render a provisional turn and repaint it from the accumulated text on each
  `delta` (throttled with `requestAnimationFrame`, reusing the existing markdown renderer).
  On `done`, remove it and call the existing "commit turn" function with the full text, so
  history, sharing, source lists and turn counting stay untouched.
- **Pitfall:** if the page's HTML lives in a JS template literal in the Worker, `\n` and
  `${` inside the page's script are interpreted by the Worker, not the browser. PIBot's page
  is `String.raw`, which keeps `\n` but still interpolates `${`; use string concatenation.
- **Keep non-streamed** for Discord (posts whole messages) and MCP.
- **Verify in a browser**, not just with curl: answer visible mid-stream, turn committed,
  follow-up question resolves pronouns from history, no console errors.

## 3. Switch the answer model to Sonnet 5.5, thinking off (`b899544` harness, `dd257dd` switch)

**Measured on PIBot (10 questions, same retrieval and prompt):**

| | Model | Median time | Median words | Cost per answer |
|---|---|---|---|---|
| A | `claude-sonnet-4-6` | 23.0 s | 562 | $0.021 |
| B | `claude-sonnet-5-5`, thinking off | 11.1 s | 545 | $0.022 |
| C | `claude-sonnet-5-5`, adaptive, effort low | 10.4 s | 514 | $0.020 |

About **2x faster** for the same length; **cost per answer flat** (the per-token price is a
third lower, $2/$10 per MTok vs $3/$15, but the new tokenizer uses ~50% more tokens per
word). Read side by side, 5.5 was at least as good and more candid about gaps; the probe
passed 29/29 with it. C never chose to think at low effort, so it behaves like B.

- **Run your own A/B before switching**, with your persona and corpus: PIBot's
  `bin/ab_model.py` plus an admin-only `variant` map in the worker (`AB_VARIANTS`). A few
  dollars.
- **Thinking must be turned off explicitly.** On Sonnet 4.6, omitting `thinking` meant no
  thinking; on Sonnet 5.5, omitting it means adaptive thinking (slower, more tokens). Send
  `thinking: {type: "between_tools"}` (thinks only between tool calls; an answer path with no
  tools never thinks). `{type: "disabled"}` returns 400 on 5.5.
- **Read text blocks, not `content[0]`.** A thinking block can come first on newer models:
  join every block with `type === "text"`.
- **Do not re-price history.** If the worker stores token counts and multiplies by price
  constants when stats are read, changing the constants silently re-prices all past usage.
  PIBot now accumulates dollars at write time, priced by the model that actually answered
  (`MODEL_PRICES`, `addClaudeUsage()`, field `claude_usd`); a record first touched after the
  switch is seeded from its existing tokens at the old rates, which is exact because every
  earlier token was the old model's. Budget circuit breakers then keep working.
- **Leave sub-tasks alone unless measured.** The Haiku calls in the ribbonfarm and MoV
  routers, and Humboldt's research pipeline models, are separate decisions with their own
  quality bar; this playbook measured only the user-facing answer.

## 4. Event and calendar awareness (`plans/event-awareness.md`, Phases A and F)

Worth doing where the bot is asked "when is X", "what's on", "what happened at Y". For PIBot
the sources are the Institute's two public Google calendars and the website's
`events.json`; for Humboldt the same Institute calendar applies; the ribbonfarm and MoV bots
probably have no event calendar and can skip this item.

- **Registry outside the worker** (`ingest/sync_events.py`, daemon step): read the public
  iCal feeds and the events list, expand recurring events over a window (`recurring_ical_events`),
  drop descriptions and locations (meeting links, emails), add aliases, and PUT the result
  to an admin endpoint that stores it in KV. Content-hash it, so a quiet cycle writes nothing.
- **Digest in the user message, gated** (`eventsDigest()`): a compact "KNOWN EVENTS" list
  (major events with computed state such as "ended 14 days ago", each calendar series' next
  occurrence, anything running or starting within 36 h), injected only when the question
  names an event or asks a when/what's-on question. Ordinary questions get nothing.
- **Live window** (`liveEvents()`, `liveEventBlock()`): a significant event is "live" from 14
  days before to 3 days after; the worker computes "day 3 of 5", so the model does no date
  arithmetic. Keep a no-calls-to-action rule: a time is a fact, "join us" is not.
- **Found by the probe:** "What's on today?" has no event noun, so a noun-and-time gate never
  fired and the bot said nothing was scheduled while two SIG calls ran that afternoon. Gate
  on strong forward-looking phrases ("what's on", "coming up", "upcoming") on their own.

## 5. Explicit time ranges as metadata filters (Phases B and E)

"What has X discussed since June?", "last month", "in 2025".

- **A numeric date on every vector** (`ts_unix`, Unix seconds). Pinecone range filters
  (`$gte`/`$lt`) work only on numbers; ISO strings cannot be range-filtered. Pick a name no
  namespace already uses (PIBot's `transcripts` already had `ts` as a string).
- **Backfill without re-embedding** (`bin/backfill_event_tags.py`):
  - `index.update(filter=..., set_metadata=...)` (Python client 9) tags every matching vector
    in one call with no reads: use it for anything decidable from existing metadata.
  - Discord message and thread ids are snowflakes that encode their own timestamp:
    `((id >> 22) + 1420070400000) / 1000`. `index.list()` returns ids only, so those vectors
    need no fetch at all.
  - `fetch` returns the 1024-float embedding with the metadata (~9 KB a vector): budget the
    egress quota (1 GB/month on the free plan) before fetching thousands.
  - Test on one vector first: `update` merges metadata (keeps every existing key), and a read
    right after a write is stale for a few seconds.
- **Tag new vectors at the upsert choke point** (`ingest/event_tags.py`, called from
  `_GuardedIndex.upsert` in `ingest/utils.py`), failing open so tagging can never break an
  ingest.
- **Parse only explicit phrases** (`timeRange()`): since <month|season|year>, in <month
  [year]|year>, before <year>, last/this week/month/year, the past N days/weeks/months,
  yesterday. "Recently" and "lately" are not ranges; "may" is a month only when capitalised.
  Default retrieval stays unfiltered by date.
- **Apply it by wrapping the namespace query** (`timedQuery()`): dated namespaces get the
  filter (ANDed with any existing filter; make sure the filter is part of any query cache
  key), undated ones are dropped while a range is active, timeless reference is kept, and a
  one-line note tells the model the period and to say so if little came back.
- **Humboldt note:** Humboldt queries PIBot's Pinecone index directly, so it can already use
  `ts_unix` and `event_id` filters on PIBot's namespaces with no backfill of its own.

## 6. Smaller items from the same sessions

- **Rerank the merged pool** (`plans/reranker.md`): Voyage `rerank-3` × the existing source
  weights, before the source cut, with a timeout and a kill switch.
- **Self-history** (`config/self_history.md` -> one vector in `meta`): a short first-person
  account of the bot's own history (launch, renames, why), so "what were you called before?"
  is answered in the bot's own voice instead of from third-person devlog entries.

---

## Status per bot (update this table when a bot adopts an item)

"—" = not adopted yet (a quick grep on 2026-10-09 found no date line, streaming or Sonnet 5.5 in the
three other bots, but confirm in each codebase before starting).

| Item | PIBot | Ribbonfarm oracle (vgr_zirp) | MoV oracle | Humboldt chat |
|---|---|---|---|---|
| 0 Probe + A/B harness | done | — | — | — |
| 1 Date in user message | done | — | — | — |
| 2 Streaming | done | — | — | — |
| 3 Sonnet 5.5, thinking off | done | — | — | — |
| 4 Event awareness | done | probably n/a | probably n/a | candidate |
| 5 Time-range filters | done | candidate | candidate | via PIBot's index |
| 6 Rerank / self-history | done | — | — | — |
