# Event and Time Awareness — Plan

**Status:** draft for VGR review (session 56, 2026-10-02). Nothing built.

**Goal (VGR, session 56):** c3po should know about specific events and answer
questions about the content belonging to them — *"what came out of the 2024
symposium?"*, *"what happened at Edge Esmeralda last year?"*, *"what has
SIGPSY covered since the summer?"*. Two halves:

1. **Calendar awareness** — c3po knows which events exist, when they ran or will
   run, and where, with the main website as the source of truth.
2. **Event membership in the archive** — every item that belongs to an event
   says so in its metadata, so retrieval can be scoped to one event.

**Generalizes session 55.** The symposium scoping built for Sept 2026 is one
hard-coded instance of this: a regex for the event name, one namespace, one year.
This plan replaces it with a registry-driven version that works for any event,
then retires the special case.

---

## What exists today (checked 2026-10-02)

**The website already publishes a calendar.** It's static JSON that the
`/events` page renders, owned and maintained on the website side:

| File | Holds |
|---|---|
| `protocol-institute.org/data/events.json` | 10 institute events, 2023–2026: 3 symposia, 5 workshops (Edge Esmeralda ×2, Edge Lanna, Datus Nusas, Khlongs & Subaks), a retreat, Bridge Atlas at Devconnect. `id`, `type`, `date_start`/`date_end`, `location`, `short_description` |
| `protocol-institute.org/data/sig-meetings.json` | Per-SIG recurring schedule with dated `occurrences` (UTC) |
| `protocol-institute.org/data/symposium-2026-recordings.json` | The 2026 recordings |

c3po already has a partial calendar of its own: Discord scheduled events go into
`config/discord_channels.json` as `cadence` / `next_event_time` per SIG channel.

**Archive metadata has no event field, and no usable time field:**

| Namespace | Date field today | Event signal today |
|---|---|---|
| `symposium` | `scheduled_date` (string) | `event` = "Protocol Symposium 2026" on everything |
| `videos` | **none** | `series` (`symposium-2024`, `protocol-school-2025`, …) |
| `sig` | `timestamp` (string) | `sig_display` — each meeting is itself a dated occurrence |
| `discord`, `discord_links` | `timestamp` / `fetch_date` | `channel_name` (some channels are event-specific) |
| `substack`, `pdfs` | `date` (string) | none — a recap of an event is just a post |

Pinecone range filters (`$gte`/`$lte`) work only on **numbers**, so "last
month" or "during the 2025 symposium" cannot be expressed as a filter today.
Retrieval has been deliberately unfiltered by date (session 55), and this plan
keeps that as the default.

---

## Design

### 1. Event registry — `ingest/sync_events.py` (daemon step)

- Pull `events.json` and `sig-meetings.json` from the live site each cycle.
  These are public static files, so no credential is needed, and the step is
  content-hashed like `sync_symposium`.
- Write `data/events_registry.json`: one record per event with its `id`,
  dates, location, description and **aliases** ("Protocol Symposium 2025",
  "2025 symposium", "PS25"). Aliases are generated from title + year and can be
  extended in `config/event_aliases.json`.
- SIG meetings are recurring events: the series is `sig:{slug}` and each
  occurrence `sig:{slug}:{date}`.
- Embed one `event_overview` chunk per event into a new **`events`**
  namespace, so "what was Edge Lanna?" retrieves the event itself.
- **c3po reads, never writes.** The website owns the calendar, the same
  boundary as `website-interface.md`. A missing event is a website fix.

### 2. A compact calendar in context — only when the question needs it

The worker already gets the current time in the user message. Add a
**known-events digest** (one line per event: name, dates, place, past or
upcoming; roughly 600 tokens for today's 10 events plus each SIG's next
meeting). It's injected only when the question names an event (registry alias
match) or carries a temporal cue ("last year", "upcoming", "since June"), so
ordinary questions pay nothing.

It goes in the **user message**, never `SYSTEM_PROMPT`, which carries
`cache_control` (see `worker_prompt_cache_boundary`). The registry reaches the
worker through KV, written by `sync_events`.

This deliberately reverses one session-54 decision: the schedule digest was cut
because the symposium bot was "a content oracle, not a scheduling app". VGR has
now asked for calendar awareness. The no-call-to-action rule is unchanged:
"the next SIGPSY meeting is Oct 8" is a fact, never an invitation.

### 3. Event membership + a numeric time field on every vector

Two metadata fields, added with Pinecone `update(set_metadata=…)`. That means
**no re-embedding**, so the only cost is write units:

- `event_id` — the registry id, or a list when an item belongs to more than
  one event (a recap that compares two symposia).
- `ts` — Unix seconds for the item's own date, normalized from the four
  current field names. The videos get one from their upload date (the
  symposium recordings use the talk date).

How each namespace gets its `event_id`, from deterministic to judgment:

| Namespace | Rule | Confidence |
|---|---|---|
| `symposium` | all → `protocol-symposium-2026` | exact |
| `videos` | `series` → event (`symposium-2024` → `protocol-symposium-2024`) via a mapping in `config/event_aliases.json`. Series that are *programs* rather than events (Bridge Atlas podcast, Town Halls, guest talks) get none | exact, from a reviewed table |
| `sig` | meeting chunks → `sig:{slug}:{date}`; series → `sig:{slug}` | exact |
| `discord` / `discord_links` | event-specific channels (by channel id in a reviewed list) → that event; everything else none | exact for listed channels |
| `substack` / `pdfs` | candidates: published within ±30 days of an event **and** mentioning it. A Sonnet pass confirms "is this about event X?" Disagreement or ambiguity escalates to a review file, same rule as deck matching: **never guess** | reviewed |

The no-guess rule matters most for the last row. A post misfiled under the
wrong event gets answered from confidently, which is worse than an untagged
post.

### 4. Retrieval: event scoping by filter, not by namespace

- Replace `symposiumScope()` with `eventScope()`: match the question against
  the registry's aliases (with the session-55 guards kept: an explicit
  different year cancels a match; a reach for prior work keeps the whole corpus
  in play).
- A matched event filters **every namespace** by `event_id`, instead of
  querying one namespace. "What came out of the 2024 symposium?" then gets the
  2024 salons (videos), recap posts (substack) and session chatter (discord)
  together, which the namespace-based scope could never do.
- Time phrases ("since June", "last month") become a `ts` range filter, **only
  when the phrase is explicit**. Default retrieval stays unfiltered by date.
- The list-question pinning from session 55 generalizes: "what workshops ran
  at X?" pins that event's records by `chunk_type`.
- The `symposium` namespace stays as storage; its special-case code goes once
  `eventScope()` reproduces the session-55 probe (the 15 phrasings) for 2026.

---

## Phases

| Phase | What | Depends on |
|---|---|---|
| **A** | `sync_events.py` registry + `events` namespace + KV copy; digest injection in the worker | nothing; the website data is live |
| **B** | `ts` backfill on all namespaces; `event_id` on the deterministic namespaces (symposium, videos, sig) | A |
| **C** | `eventScope()` in the worker for tagged namespaces; retire `symposiumScope()` after the probe passes | B |
| **D** | `event_id` for discord channels (reviewed list) and substack/pdfs (Sonnet + review queue) | B |
| **E** | Explicit time-phrase → `ts` range filters | B |

Each ingest script also learns to write both fields at ingest time, so new
content arrives tagged. Phase B fixes only the backlog.

**Cost:** metadata updates are write units only. That's about 35K vectors once
for `ts`, and far fewer for `event_id`. Phase D's Sonnet pass covers only the
date-window candidates, likely a few hundred posts and papers, a few dollars.

---

## Open questions for VGR

1. **Does `events.json` cover the events you care about?** It has the
   symposia, the workshops and one retreat. It does not have Protocol School
   2025, the Summer of Protocols cohorts (2023–25), the researcher salons, or
   Town Halls. If those should be askable as events, they should be added on
   the website side, since c3po only reads that file.
2. **How far should calendar awareness go forward?** Answering "when is the
   next SIGPSY meeting?" is in scope under this plan as a stated fact. Anything
   more (reminders, agenda arithmetic) stays out, as in session 54. Confirm.
3. **Are SIG meetings events?** The plan treats each SIG as a recurring event
   series, so "what did SIGFPT cover in August?" scopes cleanly. That's the
   main reason to include `sig-meetings.json`.
4. **Retire the `symposium` namespace's special-case code** once
   `eventScope()` passes the session-55 probe? (Proposed: yes.)
