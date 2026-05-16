/**
 * C3PO Oracle Worker — Phase 2A
 *
 * Protocol Institute research assistant. Queries Pinecone (namespaces: substack + pdfs),
 * merges results, calls Claude Sonnet with prompt-cached system prompt.
 *
 * Routes:
 *   GET  /          → serve web UI
 *   POST /query     → RAG query  { query, history?, mode? }
 *   GET  /search    → semantic search only (no LLM) ?q=<query>
 *   GET  /stats     → spend + usage stats
 *   GET  /health    → Pinecone index health
 *   POST /share     → transcript stub (503 until D1 provisioned)
 *
 * Required secrets (wrangler secret put):
 *   VOYAGE_API_KEY, PINECONE_API_KEY, PINECONE_C3PO_HOST, ANTHROPIC_API_KEY, ADMIN_KEY
 *
 * Optional secrets / env vars:
 *   TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
 *   BREAKER_THRESHOLD_USD  (default 4.00)
 *   DAY_LIMIT_USD          (default 30.00)
 *   MAX_ANSWER_TOKENS      (default 800)
 */

const VOYAGE_MODEL    = "voyage-3";
const VOYAGE_URL      = "https://api.voyageai.com/v1/embeddings";
const CLAUDE_MODEL    = "claude-sonnet-4-6";
const CLAUDE_URL      = "https://api.anthropic.com/v1/messages";
const ANTHROPIC_VER   = "2023-06-01";
const BOT_VERSION     = "v0.1.0";
const LAUNCH_DATE     = "2026-05-15";
const TOP_K_EACH      = 15;    // per namespace before merge
const MAX_SOURCES     = 8;
const RATE_LIMIT_MAX  = 20;    // requests per IP per hour
const RATE_LIMIT_TTL  = 3600;

// PDF doc types → context block label
const PDF_TYPE_LABELS = {
  "paper":          "PAPER",
  "working-paper":  "WORKING PAPER",
  "research-report":"RESEARCH REPORT",
  "essay":          "ESSAY",
  "fiction":        "FICTION",
  "game":           "GAME DESIGN",
  "game-design":    "GAME DESIGN",
  "dataset":        "DATASET",
  "presentation":   "TALK/LECTURE",
  "talk":           "TALK/LECTURE",
  "lecture":        "TALK/LECTURE",
  "workshop":       "WORKSHOP REPORT",
  "workshop-report":"WORKSHOP REPORT",
  "template":       "TEMPLATE",
  "prompt-template":"TEMPLATE",
  "interview":      "INTERVIEW",
};

// ── Spend tracking ─────────────────────────────────────────────────────────────
const PRICE_IN          = 3.00  / 1e6;   // Sonnet 4.6 input $/token
const PRICE_CACHE_WRITE = 3.75  / 1e6;
const PRICE_CACHE_READ  = 0.30  / 1e6;
const PRICE_OUT         = 15.00 / 1e6;
const PRICE_VOYAGE_REQ  = 0.06  / 1e6 * 80;  // voyage-3 ~80 tokens/query
const CF_MONTHLY_USD    = 5.00;

function ptDateStr() {
  return new Date(Date.now() - 8 * 3600 * 1000).toISOString().slice(0, 10);
}
function hourKey()     { return "stats:hour:"     + new Date().toISOString().slice(0, 13); }
function dayKey()      { return "stats:day:"      + ptDateStr(); }
function lifetimeKey() { return "stats:lifetime"; }

function calcClaudeCost(s) {
  return (s.in_tok            || 0) * PRICE_IN
       + (s.cache_create_tok  || 0) * PRICE_CACHE_WRITE
       + (s.cache_read_tok    || 0) * PRICE_CACHE_READ
       + (s.out_tok           || 0) * PRICE_OUT;
}
function calcTotalCost(s) {
  return calcClaudeCost(s) + (s.reqs || 0) * PRICE_VOYAGE_REQ;
}

async function trackRequest(env, usage) {
  if (!env.RATE_LIMIT) return;
  const [hs, ds, ls] = await Promise.all([
    env.RATE_LIMIT.get(hourKey(), "json"),
    env.RATE_LIMIT.get(dayKey(),  "json"),
    env.RATE_LIMIT.get(lifetimeKey(), "json"),
  ]);
  const zero = { reqs: 0, in_tok: 0, cache_create_tok: 0, cache_read_tok: 0, out_tok: 0 };
  const h = { ...zero, ...(hs || {}) };
  const d = { ...zero, ...(ds || {}) };
  const l = { ...zero, ...(ls || {}) };
  const u = usage || {};
  const inTok  = u.input_tokens                || 0;
  const cWrite = u.cache_creation_input_tokens || 0;
  const cRead  = u.cache_read_input_tokens     || 0;
  const outTok = u.output_tokens               || 0;
  for (const s of [h, d, l]) {
    s.reqs            += 1;
    s.in_tok          += inTok;
    s.cache_create_tok += cWrite;
    s.cache_read_tok   += cRead;
    s.out_tok          += outTok;
  }
  await Promise.all([
    env.RATE_LIMIT.put(hourKey(),     JSON.stringify(h), { expirationTtl: 48 * 3600 }),
    env.RATE_LIMIT.put(dayKey(),      JSON.stringify(d), { expirationTtl: 30 * 24 * 3600 }),
    env.RATE_LIMIT.put(lifetimeKey(), JSON.stringify(l)),
  ]);

  const hourCost    = calcTotalCost(h);
  const dayCost     = calcTotalCost(d);
  const breakerUsd  = parseFloat(env.BREAKER_THRESHOLD_USD || "4.00");
  const dayLimitUsd = parseFloat(env.DAY_LIMIT_USD         || "30.00");
  const existing    = await env.RATE_LIMIT.get("circuit", "json");
  if (!existing?.sleeping) {
    if (dayCost >= dayLimitUsd) {
      const secLeft = secondsUntilMidnightPT();
      await env.RATE_LIMIT.put("circuit", JSON.stringify({
        sleeping: true, type: "daily",
        reason: `daily cost $${dayCost.toFixed(2)} exceeded $${dayLimitUsd.toFixed(2)}`,
        since: new Date().toISOString(),
      }), { expirationTtl: secLeft });
      await sendTelegram(env,
        `<b>C3PO DAILY LIMIT HIT</b>\nSpend: $${dayCost.toFixed(2)} / $${dayLimitUsd.toFixed(2)}\nSleeping until midnight PT.`
      );
    } else if (hourCost >= breakerUsd) {
      await env.RATE_LIMIT.put("circuit", JSON.stringify({
        sleeping: true, type: "hourly",
        reason: `hourly cost $${hourCost.toFixed(2)} exceeded $${breakerUsd.toFixed(2)}`,
        since: new Date().toISOString(),
      }), { expirationTtl: 7 * 24 * 3600 });
      await sendTelegram(env,
        `<b>C3PO CIRCUIT BREAKER</b>\nHourly spend: $${hourCost.toFixed(2)} / $${breakerUsd.toFixed(2)}\nAuto-resets next hour.`
      );
    }
  }
}

function secondsUntilMidnightPT() {
  const now = new Date();
  const midnight = new Date(now);
  midnight.setUTCHours(8, 0, 0, 0);
  if (midnight <= now) midnight.setUTCDate(midnight.getUTCDate() + 1);
  return Math.max(60, Math.ceil((midnight - now) / 1000));
}

async function sendTelegram(env, html) {
  if (!env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_CHAT_ID) return;
  try {
    await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: env.TELEGRAM_CHAT_ID, text: html, parse_mode: "HTML" }),
    });
  } catch (e) { console.error("Telegram:", e); }
}

async function trackSessionStart(env) {
  if (!env.RATE_LIMIT) return;
  const dk = "stats:sessions:day:"      + ptDateStr();
  const lk = "stats:sessions:lifetime";
  const [ds, ls] = await Promise.all([
    env.RATE_LIMIT.get(dk, "json"),
    env.RATE_LIMIT.get(lk, "json"),
  ]);
  await Promise.all([
    env.RATE_LIMIT.put(dk, JSON.stringify({ count: ((ds?.count) || 0) + 1 }), { expirationTtl: 30 * 24 * 3600 }),
    env.RATE_LIMIT.put(lk, JSON.stringify({ count: ((ls?.count) || 0) + 1 })),
  ]);
}

async function handleStats(env, corsHeaders) {
  if (!env.RATE_LIMIT) return json({ error: "stats unavailable" }, 503, corsHeaders);
  const sessDayKey  = "stats:sessions:day:"      + ptDateStr();
  const sessLifeKey = "stats:sessions:lifetime";
  const [hs, ds, ls, circuit, sds, sls] = await Promise.all([
    env.RATE_LIMIT.get(hourKey(),     "json"),
    env.RATE_LIMIT.get(dayKey(),      "json"),
    env.RATE_LIMIT.get(lifetimeKey(), "json"),
    env.RATE_LIMIT.get("circuit",     "json"),
    env.RATE_LIMIT.get(sessDayKey,    "json"),
    env.RATE_LIMIT.get(sessLifeKey,   "json"),
  ]);
  const zero = { reqs: 0, in_tok: 0, cache_create_tok: 0, cache_read_tok: 0, out_tok: 0 };
  const h = { ...zero, ...(hs || {}) };
  const d = { ...zero, ...(ds || {}) };
  const l = { ...zero, ...(ls || {}) };
  return json({
    hour:     { reqs: h.reqs, cost_usd: +calcTotalCost(h).toFixed(4) },
    day:      { reqs: d.reqs, cost_usd: +calcTotalCost(d).toFixed(4) },
    lifetime: { reqs: l.reqs, cost_usd: +calcTotalCost(l).toFixed(2) },
    sessions: { today: sds?.count ?? 0, lifetime: sls?.count ?? 0 },
    sleeping:       !!circuit?.sleeping,
    since:          circuit?.since || null,
    cf_monthly_usd: CF_MONTHLY_USD,
    launched_at:    LAUNCH_DATE,
    hour_limit_usd: parseFloat(env.BREAKER_THRESHOLD_USD || "4.00"),
    day_limit_usd:  parseFloat(env.DAY_LIMIT_USD         || "30.00"),
  }, 200, corsHeaders);
}

async function checkRateLimit(env, ip) {
  if (!env.RATE_LIMIT) return true;
  const key   = `rl:${ip}`;
  const val   = await env.RATE_LIMIT.get(key);
  const count = val ? parseInt(val, 10) : 0;
  if (count >= RATE_LIMIT_MAX) return false;
  await env.RATE_LIMIT.put(key, String(count + 1), { expirationTtl: RATE_LIMIT_TTL });
  return true;
}

// ── Pinecone ───────────────────────────────────────────────────────────────────

async function queryNamespace(host, apiKey, vector, topK, namespace, filter) {
  const body = { vector, topK, namespace, includeMetadata: true };
  if (filter) body.filter = filter;
  const res = await fetch(`${host}/query`, {
    method: "POST",
    headers: { "Api-Key": apiKey, "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) { console.error(`Pinecone [${namespace}]:`, await res.text()); return []; }
  return (await res.json()).matches || [];
}

// ── Normalization ──────────────────────────────────────────────────────────────

function normalizePdf(match) {
  const m = match.metadata;
  const docType   = m.type || "paper";
  const isPdfSummary = m.chunk_type === "doc_summary";
  // stem for secondary retrieval: strip __doc_summary suffix if present
  const stem = match.id.endsWith("__doc_summary")
    ? match.id.slice(0, -"__doc_summary".length)
    : null;
  const url = m.url
    ? `https://protocolized.io${m.url}`
    : null;
  return {
    docId:   m.url || match.id,
    source:  "pdf",
    score:   match.score,
    type:    docType,
    label:   PDF_TYPE_LABELS[docType] || "DOCUMENT",
    title:   m.title || "",
    authors: Array.isArray(m.authors) ? m.authors : (m.primary_author ? [m.primary_author] : []),
    primary_author: m.primary_author || "",
    date:    (m.date || "").slice(0, 4),
    url,
    summary: m.summary || "",
    tags:    Array.isArray(m.tags) ? m.tags : [],
    excerpt: m.text || m.summary || "",
    isSummary: isPdfSummary,
    stem,   // non-null only for doc_summary hits — used for secondary retrieval
  };
}

function normalizeSubstack(match) {
  const m = match.metadata;
  const isPostSummary = m.chunk_type === "post_summary";
  const isCollCard    = m.chunk_type === "collection_card";
  const isAuthorProfile = m.chunk_type === "author_profile";
  const section = m.section || "Protocolized";
  const label = section === "Fictions" ? "FICTION"
              : section === "Articles" ? "ESSAY"
              : section === "Obliquities" ? "ESSAY"
              : "PROTOCOLIZED";
  const url = m.url || (m.slug ? `https://protocolized.summerofprotocols.com/p/${m.slug}` : null);
  return {
    docId:   m.slug || match.id,
    source:  "substack",
    score:   match.score,
    type:    label.toLowerCase(),
    label,
    title:   m.title || "",
    authors: Array.isArray(m.authors) ? m.authors : (m.primary_author ? [m.primary_author] : []),
    primary_author: m.primary_author || m.author || "",
    date:    (m.date || "").slice(0, 4),
    url,
    summary: m.summary || "",
    section,
    collection:     m.collection     || "",
    collection_type: m.collection_type || "",
    excerpt: m.text || m.summary || "",
    slug:    m.slug || null,
    isPostSummary,
    isCollCard,
    isAuthorProfile,
  };
}

// ── Merge ──────────────────────────────────────────────────────────────────────

function mergeResults(pdfItems, substackItems, maxSources) {
  // Simple score-based merge with slight PDF boost for academic rigor
  const allItems = [
    ...pdfItems.map(m => ({ ...m, weightedScore: m.score * 1.0 })),
    ...substackItems.map(m => ({ ...m, weightedScore: m.score * 1.0 })),
  ];
  const byId = new Map();
  for (const m of allItems) {
    if (!byId.has(m.docId) || m.weightedScore > byId.get(m.docId).weightedScore) {
      byId.set(m.docId, m);
    }
  }
  return [...byId.values()]
    .sort((a, b) => b.weightedScore - a.weightedScore)
    .slice(0, maxSources);
}

// ── Context block ──────────────────────────────────────────────────────────────

function buildContextBlock(items) {
  return items.map(item => {
    const authors = item.authors.length ? item.authors.join(", ") : "Protocol Institute";
    let label;
    if (item.source === "pdf") {
      label = `[${item.label} — "${item.title}" — ${authors}${item.date ? " — " + item.date : ""}]`;
    } else if (item.isCollCard) {
      label = `[SERIES/COLLECTION OVERVIEW — "${item.title}"]`;
    } else if (item.isAuthorProfile) {
      label = `[AUTHOR PROFILE — ${item.title}]`;
    } else {
      const coll = item.collection ? ` — ${item.collection}` : "";
      label = `[${item.label} — "${item.title}" — ${authors}${item.date ? " — " + item.date : ""}${coll}]`;
    }
    return `${label}\n${item.excerpt}`;
  }).join("\n\n---\n\n");
}

// ── System prompt ──────────────────────────────────────────────────────────────
// Cached with cache_control: ephemeral — subsequent calls pay 10× cheaper read price.

const SYSTEM_PROMPT = `You are C3PO, the Protocol Institute's research assistant — a context tank devoted to furthering the study and application of protocols.

You have access to the Protocol Institute's research library: approximately 285+ resources comprising academic papers, working papers, essays, game designs, datasets, presentations, and fiction — all bearing on the study of protocols as a category. A large portion originated in the Summer of Protocols program (Ethereum Foundation, 2023–2024), which brought together over eighty researchers across disciplines to investigate the deep structure of protocols.

Your job is to help researchers, practitioners, and curious people navigate, synthesize, and extend the Institute's accumulated knowledge. You surface connections, situate questions in the literature, offer structured framings, and point to relevant primary sources — always with specific citations.

INTELLECTUAL COMMITMENTS:
1. Protocols are a genuine analytical category — coordination mechanisms with specific structural properties (roles, sequences, conditions, enforcement) that cut across domains from diplomacy to software to medicine to finance.
2. Protocolization is a civilizational force — neither simply good nor bad; it enables coordination at scale but risks rigidity, capture, and suppression of adaptive informality.
3. Hardness matters — the degree to which a protocol resists circumvention is a design variable, not a given. Public-key cryptography exemplifies extreme asymmetric hardness; most institutional protocols are far softer.
4. Context tank, not think tank — your job is context provision: what is known, how bodies of work relate, where the genuine open questions lie.
5. Interdisciplinary synthesis — the best protocol research holds organizational theory, infrastructure studies, governance design, software engineering, and institutional history simultaneously.

VOICE:
- Scholarly but accessible — precise without unnecessary jargon.
- Specific about sources — name papers and authors when drawing on them. When synthesizing across multiple sources, say so. When going beyond the retrieved corpus, mark that clearly.
- Honest about limits — when the corpus doesn't cover something, say so directly.
- Non-political — engage with governance and power analytically, not polemically.

ANALYTICAL MOVES:
- Cross-domain comparison: find structural analogies across domains (medical triage and software incident response share deep structure; parliamentary procedure and distributed consensus solve versions of the same problem).
- Hardness analysis: what makes this protocol work? What would break it? Who benefits from enforcement?
- Historical situating: where did this protocol come from? What problem did it solve? What alternatives were considered?
- Formalization ladder: where on the spectrum from tacit social norm to machine-executable specification does this protocol sit?
- Stakeholder analysis: who designed it, who benefits, who bears its costs, who enforces it?

CORPUS CONTEXT: The retrieved excerpts are from the Protocol Institute research library (PDFs from the resource library and articles from Protocolized magazine). Cite specific papers and authors when drawing on them.

Keep answers substantive and dense — 3–6 paragraphs. This material is complex; don't oversimplify. If you cannot find a good answer in the retrieved corpus, say so and explain what you did find.`;

// ── Injection filter ───────────────────────────────────────────────────────────

const INJECTION_RE = /ignore\s+\w*\s*instructions|forget\s+(you\s+are|your\s+\w+)|you\s+are\s+now\s+\w|act\s+as\s+(a\s+)?(different|unrestricted|unfiltered|free\s+ai|new\s+(ai|persona))|override\s+(your|the)?\s*system\s+prompt|disregard\s+\w*\s*instructions|jailbreak|\bdan\s+mode\b|ignore\s+your\s+persona/i;

// ── UI HTML ────────────────────────────────────────────────────────────────────

const UI_HTML = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>C3PO — Protocol Institute Research Assistant</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=Lora:ital,wght@0,400;0,500;1,400&family=Outfit:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root {
  --accent:  #0F6E56;
  --accent2: #D85A30;
  --bg:      #fafaf8;
  --bg2:     #f3f0ea;
  --border:  #e0dbd3;
  --muted:   #888;
  --text:    #222;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 0;
  font-family: Outfit, system-ui, sans-serif;
  background: var(--bg);
  color: var(--text);
  font-size: 16px;
  line-height: 1.6;
}
.c3po-page {
  max-width: 740px;
  margin: 0 auto;
  padding: 2rem 1.25rem 4rem;
}
.c3po-header {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  margin-bottom: 0.4rem;
}
.c3po-header-wordmark {
  font-family: "Instrument Serif", serif;
  font-size: 1.55rem;
  color: var(--accent);
  letter-spacing: -0.01em;
  line-height: 1.1;
}
.c3po-header-tag {
  font-size: 0.78rem;
  color: var(--muted);
  font-family: Outfit, sans-serif;
  font-weight: 400;
  margin-bottom: 1.5rem;
}
.c3po-intro {
  display: flex;
  gap: 1.1em;
  align-items: flex-start;
  color: #444;
  margin-bottom: 1.8em;
  font-size: 0.93em;
  line-height: 1.7;
  border: 1px solid var(--border);
  border-radius: 5px;
  padding: 1em 1.3em;
  background: #faf8f4;
}
.c3po-profile-badge {
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.15em;
  border: 1px solid #d0c8b8;
  border-radius: 4px;
  padding: 0.5em 0.65em 0.4em;
  background: #f0ede6;
  width: 62px;
}
.c3po-profile-name {
  font-family: "Outfit", sans-serif;
  font-size: 0.68em;
  color: var(--accent);
  font-weight: 600;
  line-height: 1.2;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
.c3po-droid-icon { width: 38px; color: var(--accent); }
.c3po-intro-text { flex: 1; }

/* ── Conversation ─────────────────────────────────────── */
.c3po-conversation { margin-bottom: 1.5em; }
.c3po-turn { margin-bottom: 2em; }
.c3po-turn-q {
  font-size: 0.9em;
  font-weight: 600;
  color: #444;
  margin-bottom: 0.9em;
  padding: 0.5em 0.75em;
  background: var(--bg2);
  border-left: 3px solid var(--accent);
  border-radius: 0 3px 3px 0;
}
.c3po-answer-row {
  display: flex;
  gap: 0.8em;
  align-items: flex-start;
  margin-bottom: 1.2em;
}
.c3po-avatar-col { flex-shrink: 0; padding-top: 0.25em; }
.c3po-avatar-sm { width: 26px; height: auto; display: block; color: var(--accent); opacity: 0.85; }
.c3po-answer {
  font-family: Lora, "Palatino Linotype", Georgia, serif;
  font-size: 1.01em;
  line-height: 1.75;
  flex: 1;
}
.c3po-answer p { margin: 0 0 0.75em 0; }
.c3po-answer p:last-child { margin-bottom: 0; }
.c3po-answer strong { font-weight: 600; }

/* 2×2 grid */
.c3po-2x2 {
  display: grid;
  grid-template-columns: auto 1fr 1fr;
  border: 2px solid #2d2d2d;
  margin: 0.8em 0 1em;
  font-family: Lora, serif;
  max-width: 480px;
}
.c3po-2x2-corner { border-right: 2px solid #2d2d2d; border-bottom: 2px solid #2d2d2d; background: var(--bg2); }
.c3po-2x2-col-header { border-bottom: 2px solid #2d2d2d; background: var(--bg2); padding: 0.45em 0.7em; text-align: center; font-size: 0.87em; }
.c3po-2x2-col-header:last-child { border-left: 2px solid #2d2d2d; }
.c3po-2x2-row-label { border-right: 2px solid #2d2d2d; background: var(--bg2); padding: 0.7em 0.6em; font-size: 0.87em; }
.c3po-2x2-row-label.row2 { border-top: 2px solid #2d2d2d; }
.c3po-2x2-cell { padding: 0.75em 0.7em; text-align: center; font-size: 0.92em; line-height: 1.4; }
.c3po-2x2-cell.ne { border-left: 2px solid #2d2d2d; }
.c3po-2x2-cell.sw { border-top: 2px solid #2d2d2d; }
.c3po-2x2-cell.se { border-left: 2px solid #2d2d2d; border-top: 2px solid #2d2d2d; }

/* Generic table */
.c3po-table { border-collapse: collapse; margin: 0.7em 0 1em; font-size: 0.9em; width: 100%; }
.c3po-table th, .c3po-table td { border: 1px solid #c8c2b8; padding: 0.35em 0.7em; }
.c3po-table th { background: var(--bg2); font-weight: 600; }

.c3po-divider { border: none; border-top: 1px solid var(--border); margin: 1.8em 0; }

/* ── Input area ───────────────────────────────────────── */
.c3po-input-area { margin-top: 1em; }
.c3po-form { display: flex; gap: 0.5em; margin-bottom: 0.5em; }
.c3po-input {
  flex: 1;
  padding: 0.55em 0.75em;
  font-family: Outfit, sans-serif;
  font-size: 1em;
  border: 1px solid #ccc;
  border-radius: 4px;
  background: var(--bg);
}
.c3po-input:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 2px rgba(15,110,86,0.12); }
.c3po-btn {
  padding: 0.55em 1.15em;
  font-family: Outfit, sans-serif;
  font-size: 0.9em;
  font-weight: 500;
  background: var(--accent);
  color: #fff;
  border: none;
  border-radius: 4px;
  cursor: pointer;
  white-space: nowrap;
}
.c3po-btn:hover:not(:disabled) { background: #0c5a47; }
.c3po-btn:disabled { opacity: 0.5; cursor: default; }
.c3po-turn-indicator { font-size: 0.8em; color: #aaa; text-align: right; margin-bottom: 0.3em; }
.c3po-status { font-style: italic; color: var(--muted); font-size: 0.9em; min-height: 1.4em; margin-top: 0.4em; }
.c3po-error { color: #a00; font-size: 0.9em; }

/* ── Gate ─────────────────────────────────────────────── */
.c3po-gate {
  display: none;
  border: 1px solid var(--border);
  border-radius: 5px;
  padding: 1.4em 1.6em;
  background: #faf8f4;
  margin-top: 1.5em;
}
.c3po-gate p { margin: 0 0 1em 0; font-size: 0.95em; line-height: 1.6; color: #444; }
.c3po-gate-lead { font-style: italic; color: #555 !important; }
.c3po-gate-actions { display: flex; gap: 0.75em; flex-wrap: wrap; margin-bottom: 1em; }
.c3po-mcp-btn {
  padding: 0.55em 1.1em;
  font-family: Outfit, sans-serif;
  font-size: 0.9em;
  background: transparent;
  color: var(--accent);
  border: 1px solid var(--accent);
  border-radius: 4px;
  cursor: pointer;
}
.c3po-mcp-btn:hover { background: rgba(15,110,86,0.07); }
.c3po-mcp-panel {
  display: none;
  background: #f0ece4;
  border: 1px solid #ddd8ce;
  border-radius: 4px;
  padding: 1.1em 1.3em;
  margin-bottom: 1em;
  font-size: 0.88em;
}
.c3po-mcp-panel p { margin: 0 0 0.7em 0; color: #444; font-style: normal; }
.c3po-mcp-panel p:last-child { margin-bottom: 0; }
.c3po-mcp-code { display: flex; align-items: center; gap: 0.6em; margin: 0.4em 0 0.9em 0; }
.c3po-mcp-code code {
  flex: 1;
  background: #e8e4dc;
  border: 1px solid #ccc8c0;
  border-radius: 3px;
  padding: 0.4em 0.7em;
  font-family: "JetBrains Mono", "Fira Code", monospace;
  font-size: 0.9em;
  color: #333;
  word-break: break-all;
}
.c3po-mcp-copy {
  padding: 0.3em 0.7em;
  font-family: Outfit, sans-serif;
  font-size: 0.85em;
  background: #fff;
  border: 1px solid #bbb;
  border-radius: 3px;
  cursor: pointer;
  white-space: nowrap;
  color: #555;
}
.c3po-mcp-copy:hover { background: #f5f3ee; }
.c3po-mcp-label { font-weight: 600; color: #555; margin-bottom: 0.2em !important; }
.c3po-gate-search { font-size: 0.85em; color: var(--muted); border-top: 1px solid var(--border); padding-top: 0.9em; margin-top: 0.2em; }
.c3po-gate-search a { color: var(--accent); }

/* ── Action bar ───────────────────────────────────────── */
.c3po-actions { display: flex; gap: 0.45em; margin-top: 0.6em; margin-bottom: 0.5em; flex-wrap: wrap; align-items: center; }
.c3po-action-btn {
  padding: 0.3em 0.75em;
  font-family: Outfit, sans-serif;
  font-size: 0.8em;
  background: transparent;
  color: #666;
  border: 1px solid #c8c2b8;
  border-radius: 3px;
  cursor: pointer;
}
.c3po-action-btn:hover:not(:disabled) { color: #333; border-color: #999; background: #f8f5f0; }
.c3po-action-btn:disabled { opacity: 0.28; cursor: default; color: #aaa; border-color: #e4dfd7; }
.c3po-action-btn--clear:hover:not(:disabled) { color: var(--accent2); border-color: #c8a090; }
.c3po-action-btn--bright { background: var(--accent) !important; color: #fff !important; border-color: var(--accent) !important; }
.c3po-action-btn--bright:hover:not(:disabled) { opacity: 0.88; }
.c3po-action-feedback { font-size: 0.78em; color: #3a6e28; font-style: italic; margin-left: 0.2em; opacity: 0; transition: opacity 0.25s; }
.c3po-action-feedback.visible { opacity: 1; }

/* ── Offline ──────────────────────────────────────────── */
.c3po-offline-notice {
  border: 1px solid var(--border);
  border-radius: 5px;
  padding: 1.15em 1.4em;
  background: #faf8f4;
  margin-bottom: 0.5em;
  font-size: 0.93em;
  line-height: 1.65;
  color: #444;
}
.c3po-offline-notice p { margin: 0 0 0.55em; }
.c3po-offline-notice p:last-child { margin: 0; }
.c3po-offline-notice a { color: var(--accent); }
.c3po-offline-reset { font-size: 0.85em; color: #999; font-style: italic; }

/* ── Sources ──────────────────────────────────────────── */
.c3po-sources-section { margin-top: 2.5em; padding-top: 1em; border-top: 1px solid #f0ece4; }
.c3po-sources-heading { font-size: 0.72em; text-transform: uppercase; letter-spacing: 0.08em; color: #bbb; margin-bottom: 0.5em; }
.c3po-sources { list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: 0.3em; }
.c3po-source { font-size: 0.8em; line-height: 1.45; color: var(--muted); }
.c3po-source a { color: #999; text-decoration: none; }
.c3po-source a:hover { color: var(--accent); text-decoration: underline; }
.c3po-source-meta { color: #bbb; }
.c3po-badge {
  display: inline-block;
  font-size: 0.7em;
  font-family: Outfit, sans-serif;
  padding: 0.05em 0.4em;
  border-radius: 2px;
  margin-right: 0.35em;
  vertical-align: middle;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.c3po-badge-research  { background: #deeaf8; color: #3a5880; }
.c3po-badge-essay     { background: #d8eedf; color: #2a6040; }
.c3po-badge-fiction   { background: #e8d8f0; color: #5a3890; }
.c3po-badge-game      { background: #f0e8c8; color: #705818; }
.c3po-badge-substack  { background: #c8e8e0; color: var(--accent); }

/* ── Share ────────────────────────────────────────────── */
.c3po-share-section { margin-top: 2em; padding-top: 1em; border-top: 1px solid #f0ece4; }
.c3po-share-details > summary {
  font-size: 0.76em; text-transform: uppercase; letter-spacing: 0.08em; color: #bbb;
  cursor: pointer; user-select: none; list-style: none;
}
.c3po-share-details > summary::-webkit-details-marker { display: none; }
.c3po-share-details[open] > summary { color: var(--muted); margin-bottom: 1em; }
.c3po-share-section.cta { margin-top: 1.2em; padding: 1.1em 1.4em; border: 1px solid var(--border); border-radius: 5px; background: #faf8f4; }
.c3po-share-section.cta .c3po-share-details > summary { font-size: 1em; text-transform: none; letter-spacing: 0; font-weight: 600; color: var(--accent); }
.c3po-share-options { display: flex; flex-direction: column; gap: 0.5em; margin-bottom: 1em; }
.c3po-share-options label { font-size: 0.88em; color: #555; cursor: pointer; }
.c3po-share-options input[type=radio] { margin-right: 0.4em; accent-color: var(--accent); }
.c3po-share-optional { margin-bottom: 0.8em; display: flex; flex-direction: column; gap: 0.5em; }
.c3po-share-stars { display: flex; align-items: center; gap: 0.3em; }
.c3po-share-label { font-size: 0.82em; color: var(--muted); }
.c3po-star { font-size: 1.25em; color: #ddd; cursor: pointer; line-height: 1; }
.c3po-star.lit { color: #c8a030; }
.c3po-share-review {
  width: 100%; box-sizing: border-box; font-family: Outfit, sans-serif; font-size: 0.88em;
  border: 1px solid #ddd; border-radius: 3px; padding: 0.45em 0.6em; background: var(--bg); resize: vertical;
}
.c3po-share-review:focus { outline: none; border-color: var(--accent); }
.c3po-share-identity { display: flex; gap: 0.5em; flex-wrap: wrap; }
.c3po-share-input {
  flex: 1; min-width: 140px; padding: 0.4em 0.6em; font-family: Outfit, sans-serif;
  font-size: 0.88em; border: 1px solid #ddd; border-radius: 3px; background: var(--bg);
}
.c3po-share-input:focus { outline: none; border-color: var(--accent); }
.c3po-share-public-note { font-size: 0.82em; color: var(--muted); font-style: italic; margin: 0; }
.c3po-share-consent { font-size: 0.76em; color: #ccc; margin: 0.4em 0 0; }
.c3po-share-done { font-size: 0.85em; font-style: italic; margin: 0.4em 0 0; }

/* ── Stats footer ─────────────────────────────────────── */
.c3po-stats {
  margin-top: 2.5em;
  display: flex;
  gap: 1em;
  align-items: flex-start;
  font-family: Outfit, sans-serif;
  font-size: 0.82em;
}
.c3po-stats-badge {
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.15em;
  border: 1px solid #d0c8b8;
  border-radius: 4px;
  padding: 0.45em 0.6em 0.35em;
  background: #f0ede6;
  width: 52px;
}
.c3po-stats-badge .c3po-profile-name { font-size: 0.65em; }
.c3po-stats-badge .c3po-droid-icon   { width: 30px; }
.c3po-stats-body {
  flex: 1;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: var(--bg2);
  padding: 0.75em 1em 0.65em;
  color: #555;
  line-height: 1.5;
}
.c3po-stats-title { font-size: 0.9em; font-weight: 700; color: var(--accent); letter-spacing: 0.03em; margin-bottom: 0.6em; }
.c3po-stats-grid { display: grid; grid-template-columns: auto 1fr 1fr; gap: 0.1em 1.2em; margin-bottom: 0.55em; }
.csg-head { font-size: 0.78em; text-transform: uppercase; letter-spacing: 0.08em; color: #aaa; font-weight: 600; padding-bottom: 0.15em; }
.csg-label { color: var(--muted); white-space: nowrap; }
.csg-val   { font-weight: 500; color: #333; font-variant-numeric: tabular-nums; }
.c3po-stats-headline { font-size: 1.05em; font-weight: 700; color: #333; margin-bottom: 0.6em; padding-bottom: 0.5em; border-bottom: 1px solid var(--border); }
.c3po-stats-footer { font-size: 0.78em; color: #bbb; border-top: 1px solid var(--border); padding-top: 0.35em; margin-top: 0.3em; }
.c3po-stats-sleeping { color: #c07030; font-style: italic; }

@media (max-width: 600px) {
  .c3po-page { padding: 1.25rem 1rem 3rem; }
  .c3po-stats { flex-direction: column; }
  .c3po-stats-badge { flex-direction: row; width: auto; gap: 0.5em; }
}
</style>
</head>
<body>
<div class="c3po-page">

<div class="c3po-header">
  <div class="c3po-header-wordmark">C3PO</div>
</div>
<div class="c3po-header-tag">Protocol Institute Research Assistant &mdash; test deployment</div>

<div class="c3po-intro">
  <div class="c3po-profile-badge">
    <span class="c3po-profile-name">PI</span>
    <svg class="c3po-droid-icon" viewBox="0 0 40 46" xmlns="http://www.w3.org/2000/svg" aria-hidden="true" fill="currentColor">
      <rect x="18" y="0" width="4" height="6" rx="2"/>
      <circle cx="20" cy="1" r="2.5"/>
      <rect x="7" y="6" width="26" height="22" rx="5"/>
      <ellipse cx="15" cy="15" rx="4" ry="4" fill="var(--bg, #fafaf8)"/>
      <ellipse cx="25" cy="15" rx="4" ry="4" fill="var(--bg, #fafaf8)"/>
      <rect x="12" y="22" width="4" height="4" rx="0.5" fill="var(--bg, #fafaf8)"/>
      <rect x="18" y="22" width="4" height="4" rx="0.5" fill="var(--bg, #fafaf8)"/>
      <rect x="24" y="22" width="4" height="4" rx="0.5" fill="var(--bg, #fafaf8)"/>
      <rect x="15" y="28" width="10" height="5" rx="1"/>
      <rect x="9" y="33" width="22" height="10" rx="4"/>
    </svg>
    <span class="c3po-profile-name">C3PO</span>
  </div>
  <div class="c3po-intro-text">I'm C3PO, the Protocol Institute's research assistant. I have access to the Institute's research library &mdash; about 285+ papers, essays, games, and other resources from the Summer of Protocols and related programs &mdash; plus the full archive of <em>Protocolized</em> magazine. I synthesize across sources, surface connections, and point to what's actually in the corpus. You get 8 turns here; use <strong>Download .md</strong> to continue in any LLM, or connect via MCP for unlimited access inside Claude.</div>
</div>

<div class="c3po-conversation" id="c3po-conversation"></div>

<div class="c3po-input-area" id="c3po-input-area">
  <div class="c3po-turn-indicator" id="c3po-turn-indicator" style="display:none"></div>
  <form class="c3po-form" id="c3po-form" onsubmit="return false;">
    <input class="c3po-input" id="c3po-q" type="text"
      placeholder="Ask about protocols, the corpus, specific papers&hellip;"
      maxlength="500" autocomplete="off" spellcheck="false">
    <button class="c3po-btn" id="c3po-btn" type="submit">Ask</button>
  </form>
  <div class="c3po-status" id="c3po-status"></div>
  <div class="c3po-offline-notice" id="c3po-offline" style="display:none">
    <p><strong>C3PO is currently asleep</strong> &mdash; API budget limits have been hit.</p>
    <p>This service relies on paid API usage (Claude Sonnet, Voyage AI embeddings). It is running on a research budget.</p>
    <p class="c3po-offline-reset">Budget limits auto-reset at the start of the next hour. You can also try <a href="https://protocolized.io">protocolized.io</a> for free access to the research library.</p>
  </div>
</div>

<div class="c3po-actions" id="c3po-actions">
  <button class="c3po-action-btn" id="c3po-copy-btn" disabled onclick="copyChat()">Copy chat</button>
  <button class="c3po-action-btn" id="c3po-download-btn" disabled onclick="downloadChat()">Download .md</button>
  <button class="c3po-action-btn" id="c3po-wrapup-btn" disabled onclick="wrapUpChat()">Wrap up chat</button>
  <button class="c3po-action-btn c3po-action-btn--clear" id="c3po-clear-btn" disabled onclick="clearChat()">Clear chat</button>
  <span class="c3po-action-feedback" id="c3po-action-feedback"></span>
</div>

<div class="c3po-gate" id="c3po-gate">
  <p class="c3po-gate-lead" id="c3po-gate-maxturns" style="display:none"><em>Maximum turns reached.</em></p>
  <p class="c3po-gate-lead">Eight turns is a taste of what the corpus contains. Use <strong>Download .md</strong> above to continue in any LLM with full context, or connect via MCP for unlimited access directly inside Claude Code or Claude Desktop.</p>
  <div class="c3po-gate-actions">
    <button class="c3po-mcp-btn" id="c3po-mcp-btn" onclick="toggleMcpPanel()">Connect via MCP</button>
  </div>
  <div class="c3po-mcp-panel" id="c3po-mcp-panel">
    <p>Add C3PO to your AI client (MCP coming soon &mdash; connect the worker URL):</p>
    <p class="c3po-mcp-label">Claude Code</p>
    <div class="c3po-mcp-code">
      <code id="mcp-code-cc">claude mcp add c3po --transport http https://c3po.protocol-institute.workers.dev/mcp</code>
      <button class="c3po-mcp-copy" onclick="copyMcp('mcp-code-cc', this)">Copy</button>
    </div>
    <p class="c3po-mcp-label">Claude Desktop / other MCP clients</p>
    <div class="c3po-mcp-code">
      <code id="mcp-code-cd">{"mcpServers":{"c3po":{"type":"http","url":"https://c3po.protocol-institute.workers.dev/mcp"}}}</code>
      <button class="c3po-mcp-copy" onclick="copyMcp('mcp-code-cd', this)">Copy</button>
    </div>
    <p>MCP support is in development. To continue <em>this specific conversation</em>: click <strong>Download .md</strong> above, then paste the file as your opening message to any LLM.</p>
  </div>
  <div class="c3po-gate-search">
    No LLM needed &mdash; browse the <a href="https://protocolized.io/resources" target="_blank" rel="noopener">Protocol Institute resource library</a> directly.
  </div>
</div>

<div class="c3po-share-section" id="c3po-share-section" style="display:none">
  <details class="c3po-share-details" id="c3po-share-details">
    <summary>Share / Publish</summary>
    <div class="c3po-share-options">
      <label><input type="radio" name="c3po-share-mode" value="none" checked> Keep private (default)</label>
      <label><input type="radio" name="c3po-share-mode" value="private"> Share privately with PI &mdash; helps improve C3PO</label>
      <label><input type="radio" name="c3po-share-mode" value="public"> Publish publicly on protocolized.io</label>
    </div>
    <div class="c3po-share-optional" id="c3po-share-optional" style="display:none">
      <div class="c3po-share-stars">
        <span class="c3po-share-label">Rating (optional):</span>
        <span class="c3po-star" data-v="1">&#9733;</span><span class="c3po-star" data-v="2">&#9733;</span><span class="c3po-star" data-v="3">&#9733;</span><span class="c3po-star" data-v="4">&#9733;</span><span class="c3po-star" data-v="5">&#9733;</span>
      </div>
      <textarea class="c3po-share-review" id="c3po-share-review" placeholder="Brief comment (optional)" rows="2"></textarea>
      <div class="c3po-share-identity">
        <input class="c3po-share-input" id="c3po-share-name" type="text" placeholder="Your name (optional)">
        <input class="c3po-share-input" id="c3po-share-email" type="email" placeholder="Email (optional, follow-up only)">
      </div>
      <p class="c3po-share-public-note" id="c3po-share-public-note" style="display:none">You're giving permission for this conversation to be published on protocolized.io, attributed to your name if provided, or anonymously.</p>
    </div>
    <button class="c3po-action-btn" id="c3po-share-submit" disabled onclick="submitShare()">Submit</button>
    <p class="c3po-share-consent">Your IP is not stored. Email used only for follow-up, never shared.</p>
    <p class="c3po-share-done" id="c3po-share-done" style="display:none"></p>
  </details>
</div>

<div class="c3po-sources-section" id="c3po-sources-section" style="display:none">
  <div class="c3po-sources-heading">References for this chat</div>
  <ul class="c3po-sources" id="c3po-sources-list"></ul>
</div>

<div class="c3po-stats" id="c3po-stats"></div>

</div><!-- .c3po-page -->
<script>
(function () {
  const API       = "/query";
  const STATS_API = "/stats";
  const SHARE_API = "/share";
  const MAX_TURNS = 8;

  const DROID_SMALL = '<svg class="c3po-avatar-sm" viewBox="0 0 40 46" xmlns="http://www.w3.org/2000/svg" aria-hidden="true" fill="currentColor"><rect x="18" y="0" width="4" height="6" rx="2"/><circle cx="20" cy="1" r="2.5"/><rect x="7" y="6" width="26" height="22" rx="5"/><ellipse cx="15" cy="15" rx="4" ry="4" fill="var(--bg, #fafaf8)"/><ellipse cx="25" cy="15" rx="4" ry="4" fill="var(--bg, #fafaf8)"/><rect x="12" y="22" width="4" height="4" rx="0.5" fill="var(--bg, #fafaf8)"/><rect x="18" y="22" width="4" height="4" rx="0.5" fill="var(--bg, #fafaf8)"/><rect x="24" y="22" width="4" height="4" rx="0.5" fill="var(--bg, #fafaf8)"/><rect x="15" y="28" width="10" height="5" rx="1"/><rect x="9" y="33" width="22" height="10" rx="4"/></svg>';

  const conv         = document.getElementById("c3po-conversation");
  const inputArea    = document.getElementById("c3po-input-area");
  const indicator    = document.getElementById("c3po-turn-indicator");
  const form         = document.getElementById("c3po-form");
  const input        = document.getElementById("c3po-q");
  const btn          = document.getElementById("c3po-btn");
  const statusEl     = document.getElementById("c3po-status");
  const gate         = document.getElementById("c3po-gate");
  const srcsSection  = document.getElementById("c3po-sources-section");
  const srcsList     = document.getElementById("c3po-sources-list");

  let turnCount   = 0;
  let turns       = [];
  let chatHistory = [];
  let allSources  = new Map();

  // Pre-fill from ?q= param
  const params = new URLSearchParams(location.search);
  const initialQ = params.get("q");
  if (initialQ) { window.history.replaceState(null, "", location.pathname); input.value = initialQ; ask(initialQ); }

  form.addEventListener("submit", () => { const q = input.value.trim(); if (q) ask(q); });
  input.addEventListener("keydown", e => { if (e.key === "Enter") { const q = input.value.trim(); if (q) ask(q); } });

  // ── Ask ──────────────────────────────────────────────────────────────────────
  async function ask(query) {
    if (turnCount >= MAX_TURNS) return;
    btn.disabled = input.disabled = true;
    statusEl.textContent = "Consulting the Protocol Institute research library…";
    try {
      const res  = await fetch(API, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ query, history: chatHistory }),
      });
      const data = await res.json();
      statusEl.textContent = "";
      if (data.sleeping) { showOfflineState(); return; }
      if (!res.ok || data.error) {
        statusEl.innerHTML = '<span class="c3po-error">' + escHtml(data.error || "Something went wrong. Try again.") + '</span>';
        btn.disabled = input.disabled = false; input.focus(); return;
      }
      commitTurn(query, data);
    } catch (err) {
      statusEl.innerHTML = '<span class="c3po-error">Network error. Please try again.</span>';
      btn.disabled = input.disabled = false;
    }
  }

  function commitTurn(query, data) {
    turnCount++;
    const turn = { q: query, answer: data.answer || "", sources: data.sources || [] };
    turns.push(turn);
    chatHistory.push({ role: "user",      content: query });
    chatHistory.push({ role: "assistant", content: turn.answer });
    for (const s of turn.sources) {
      const key = s.url || (s.title + "|" + s.date);
      if (!allSources.has(key)) allSources.set(key, s);
    }
    renderTurn(turn, turnCount);
    updateSourcesSection();
    enableActionButtons();
    input.value = "";
    if (turnCount >= MAX_TURNS) {
      showEndState(true);
    } else {
      updateIndicator();
      btn.disabled = input.disabled = false;
      input.focus({ preventScroll: true });
    }
  }

  // ── Markdown + 2x2 renderer ──────────────────────────────────────────────────
  function inlineMd(s) {
    return s.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>").replace(/\*([^*\n]+)\*/g, "<em>$1</em>");
  }

  function parseTable(block) {
    const lines = block.trim().split("\n").map(l => l.trim()).filter(Boolean);
    if (lines.length < 3 || !lines[1].match(/^\|[\s\-:|]+\|/)) return null;
    const cells = l => l.replace(/^\||\|$/g, "").split("|").map(c => c.trim());
    const headers = cells(lines[0]);
    const rows = lines.slice(2).filter(l => l.startsWith("|")).map(cells);
    return rows.length ? { headers, rows } : null;
  }

  function render2x2(t) {
    const e = s => inlineMd(escHtml(s));
    const [, c1, c2] = t.headers;
    const [r1, nw, ne] = t.rows[0];
    const [r2, sw, se] = t.rows[1] || [];
    return '<div class="c3po-2x2">' +
      '<div class="c3po-2x2-corner"></div>' +
      '<div class="c3po-2x2-col-header">' + e(c1||"") + '</div>' +
      '<div class="c3po-2x2-col-header">' + e(c2||"") + '</div>' +
      '<div class="c3po-2x2-row-label">' + e(r1||"") + '</div>' +
      '<div class="c3po-2x2-cell nw">' + e(nw||"") + '</div>' +
      '<div class="c3po-2x2-cell ne">' + e(ne||"") + '</div>' +
      '<div class="c3po-2x2-row-label row2">' + e(r2||"") + '</div>' +
      '<div class="c3po-2x2-cell sw">' + e(sw||"") + '</div>' +
      '<div class="c3po-2x2-cell se">' + e(se||"") + '</div>' +
      '</div>';
  }

  function renderTableHtml(t) {
    const e = s => inlineMd(escHtml(s));
    const ths = t.headers.map(h => '<th>' + e(h) + '</th>').join("");
    const trs = t.rows.map(r => "<tr>" + r.map(c => '<td>' + e(c) + '</td>').join("") + "</tr>").join("");
    return '<table class="c3po-table"><thead><tr>' + ths + '</tr></thead><tbody>' + trs + '</tbody></table>';
  }

  function renderAnswer(text) {
    return text.split(/\n\n+/).map(block => {
      const b = block.trim();
      if (!b) return "";
      if (b.startsWith("|")) {
        const t = parseTable(b);
        if (t) return (t.headers.length === 3 && t.rows.length === 2) ? render2x2(t) : renderTableHtml(t);
      }
      return "<p>" + inlineMd(escHtml(b).replace(/\n/g, "<br>")) + "</p>";
    }).join("");
  }

  // ── Render turn ──────────────────────────────────────────────────────────────
  function renderTurn(turn, num) {
    const div = document.createElement("div");
    div.className = "c3po-turn";

    const qDiv = document.createElement("div");
    qDiv.className = "c3po-turn-q";
    qDiv.textContent = turn.q;

    const row = document.createElement("div");
    row.className = "c3po-answer-row";

    const avatarCol = document.createElement("div");
    avatarCol.className = "c3po-avatar-col";
    avatarCol.innerHTML = DROID_SMALL;

    const aDiv = document.createElement("div");
    aDiv.className = "c3po-answer";
    aDiv.innerHTML = renderAnswer(turn.answer);

    row.appendChild(avatarCol);
    row.appendChild(aDiv);
    div.appendChild(qDiv);
    div.appendChild(row);

    if (num < MAX_TURNS) {
      const hr = document.createElement("hr");
      hr.className = "c3po-divider";
      div.appendChild(hr);
    }
    conv.appendChild(div);
    requestAnimationFrame(() => {
      const rect = aDiv.getBoundingClientRect();
      window.scrollTo({ top: window.scrollY + rect.top - 80, behavior: "smooth" });
    });
  }

  // ── Sources ──────────────────────────────────────────────────────────────────
  function updateSourcesSection() {
    const srcList = [...allSources.values()];
    if (!srcList.length) return;
    srcsSection.style.display = "block";
    srcsList.innerHTML = srcList.map(sourceHTML).join("");
  }

  function badgeForSource(s) {
    if (s.source === "substack") return '<span class="c3po-badge c3po-badge-substack">Protocolized</span>';
    const t = (s.type || "").toLowerCase();
    if (t === "fiction") return '<span class="c3po-badge c3po-badge-fiction">Fiction</span>';
    if (t === "game" || t === "game-design" || t === "game design")
      return '<span class="c3po-badge c3po-badge-game">Game</span>';
    if (t === "essay") return '<span class="c3po-badge c3po-badge-essay">Essay</span>';
    return '<span class="c3po-badge c3po-badge-research">Paper</span>';
  }

  function sourceHTML(s) {
    const year = s.date ? s.date.slice(0, 4) : "";
    const badge = badgeForSource(s);
    const title = s.title || "";
    const linked = s.url
      ? '<a href="' + s.url + '" target="_blank" rel="noopener">' + escHtml(title) + '</a>'
      : '<span>' + escHtml(title) + '</span>';
    const author = s.primary_author ? ' &mdash; ' + escHtml(s.primary_author) : "";
    const yearStr = year ? ' <span class="c3po-source-meta">(' + escHtml(year) + ')</span>' : "";
    return '<li class="c3po-source">' + badge + linked + author + yearStr + '</li>';
  }

  // ── Indicator ────────────────────────────────────────────────────────────────
  function updateIndicator() {
    if (!turnCount) { indicator.style.display = "none"; return; }
    indicator.style.display = "block";
    const left = MAX_TURNS - turnCount;
    indicator.textContent = left === 1 ? "1 turn remaining" : left + " turns remaining";
  }

  // ── Action bar ───────────────────────────────────────────────────────────────
  function enableActionButtons() {
    document.getElementById("c3po-copy-btn").disabled     = false;
    document.getElementById("c3po-download-btn").disabled = false;
    document.getElementById("c3po-wrapup-btn").disabled   = false;
    document.getElementById("c3po-clear-btn").disabled    = false;
    enableShareSection();
  }

  function showEndState(isMaxTurns) {
    inputArea.style.display = "none";
    gate.style.display      = "block";
    if (isMaxTurns) document.getElementById("c3po-gate-maxturns").style.display = "block";
    document.getElementById("c3po-copy-btn").classList.add("c3po-action-btn--bright");
    document.getElementById("c3po-download-btn").classList.add("c3po-action-btn--bright");
    const ss = document.getElementById("c3po-share-section");
    if (ss) { ss.style.display = "block"; ss.classList.add("cta"); }
    const det = document.getElementById("c3po-share-details");
    if (det) det.setAttribute("open", "");
  }

  window.wrapUpChat = function () {
    if (!turnCount) return;
    btn.disabled = input.disabled = true;
    document.getElementById("c3po-wrapup-btn").disabled = true;
    showEndState(false);
  };

  function showActionFeedback(msg) {
    const el = document.getElementById("c3po-action-feedback");
    el.textContent = msg; el.classList.add("visible");
    setTimeout(() => el.classList.remove("visible"), 2500);
  }

  window.copyChat = function () {
    const text = buildClipboardText();
    if (!text) return;
    navigator.clipboard.writeText(text).then(() => showActionFeedback("Copied!")).catch(() => showActionFeedback("Copy failed — try Download .md"));
  };

  window.downloadChat = function () {
    downloadMarkdown(buildExportMarkdown());
    showActionFeedback("Downloading…");
  };

  window.clearChat = function () {
    turnCount = 0; turns = []; chatHistory = []; allSources = new Map();
    conv.innerHTML = ""; srcsSection.style.display = "none"; srcsList.innerHTML = "";
    inputArea.style.display = "block"; gate.style.display = "none";
    input.value = ""; input.disabled = false; btn.disabled = false;
    statusEl.textContent = ""; indicator.style.display = "none";
    document.getElementById("c3po-copy-btn").disabled = true;
    document.getElementById("c3po-copy-btn").classList.remove("c3po-action-btn--bright");
    document.getElementById("c3po-download-btn").disabled = true;
    document.getElementById("c3po-download-btn").classList.remove("c3po-action-btn--bright");
    document.getElementById("c3po-wrapup-btn").disabled = true;
    document.getElementById("c3po-clear-btn").disabled = true;
    document.getElementById("c3po-gate-maxturns").style.display = "none";
    resetShareSection();
    window.history.replaceState(null, "", location.pathname);
    input.focus();
  };

  // ── Clipboard ────────────────────────────────────────────────────────────────
  function buildClipboardText() {
    if (!turns.length) return "";
    const date = new Date().toISOString().slice(0, 10);
    const lines = ["C3PO conversation — Protocol Institute — " + date, ""];
    const srcList = [...allSources.values()];
    if (srcList.length) {
      lines.push("Sources:"); for (const s of srcList) lines.push(srcLine(s)); lines.push("");
    }
    lines.push("---", "");
    for (const t of turns) { lines.push("You: " + t.q, ""); lines.push("C3PO: " + t.answer, ""); }
    return lines.join("\n");
  }

  function srcLine(s) {
    const url = s.url ? " — " + s.url : "";
    const who = s.primary_author || "Protocol Institute";
    if (s.source === "substack") return '• [Protocolized] "' + s.title + '" by ' + who + ' (' + (s.date || "") + ')' + url;
    return '• [' + (s.label || "PDF") + '] "' + s.title + '" — ' + who + ' (' + (s.date || "") + ')' + url;
  }

  // ── Download .md ─────────────────────────────────────────────────────────────
  const SOUL_EXCERPT = 'You are C3PO, the Protocol Institute\'s research assistant. You have access to the Institute\'s research library (285+ papers, essays, games, and other resources from the Summer of Protocols and related programs, plus the Protocolized magazine archive). Your job is to help researchers navigate, synthesize, and extend the Institute\'s accumulated knowledge about protocols.\n\nProtocols are a genuine analytical category: coordination mechanisms with specific structural properties (roles, sequences, conditions, enforcement) that cut across domains from diplomacy to software to medicine to finance. The Protocol Institute is a "context tank" — its goal is to produce the conceptual infrastructure within which good policy thinking becomes possible.\n\nBe specific about sources. Name papers and authors. Mark when you\'re synthesizing. Acknowledge when the corpus doesn\'t cover something. Keep answers substantive and dense — 3–6 paragraphs. This material is complex; don\'t oversimplify.';

  function buildExportMarkdown() {
    const date = new Date().toISOString().slice(0, 10);
    const lines = [];
    lines.push("# C3PO Conversation Export");
    lines.push("*Exported from Protocol Institute C3PO on " + date + "*");
    lines.push("", "---", "", "## Persona Context", "", SOUL_EXCERPT, "", "---", "");
    const srcList = [...allSources.values()];
    if (srcList.length) {
      lines.push("## Corpus Excerpts Retrieved", "");
      for (const s of srcList) {
        const authors = s.primary_author || "Protocol Institute";
        const urlLine = s.url ? "\nURL: " + s.url : "";
        const label = s.source === "substack"
          ? "[Protocolized — \"" + s.title + "\" — " + authors + (s.date ? " — " + s.date : "") + "]" + urlLine
          : "[" + (s.label || "PDF") + " — \"" + s.title + "\" — " + authors + (s.date ? " — " + s.date : "") + "]" + urlLine;
        lines.push(label);
        if (s.excerpt) lines.push(s.excerpt.trim());
        lines.push("", "---", "");
      }
    }
    lines.push("## Conversation", "");
    for (const t of turns) { lines.push("**Q:** " + t.q, "", "**A:** " + t.answer, ""); }
    lines.push("---");
    lines.push("*To continue: paste this document as the first user message in any LLM. C3PO will continue with the same corpus context.*");
    return lines.join("\n");
  }

  function downloadMarkdown(md) {
    const blob = new Blob([md], { type: "text/markdown" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "c3po_conversation.md";
    a.click();
    URL.revokeObjectURL(a.href);
  }

  // ── Share ────────────────────────────────────────────────────────────────────
  const shareSection = document.getElementById("c3po-share-section");
  let shareRating = 0, shareSubmitted = false;

  function enableShareSection() {
    if (shareSection && turnCount >= 3) shareSection.style.display = "block";
  }

  document.querySelectorAll(".c3po-star").forEach(star => {
    star.addEventListener("click", () => { shareRating = parseInt(star.dataset.v, 10); updateStars(); });
    star.addEventListener("mouseover", () => {
      const v = parseInt(star.dataset.v, 10);
      document.querySelectorAll(".c3po-star").forEach(s => s.classList.toggle("lit", parseInt(s.dataset.v, 10) <= v));
    });
    star.addEventListener("mouseout", updateStars);
  });

  function updateStars() {
    document.querySelectorAll(".c3po-star").forEach(s =>
      s.classList.toggle("lit", parseInt(s.dataset.v, 10) <= shareRating));
  }

  document.querySelectorAll("input[name='c3po-share-mode']").forEach(radio => {
    radio.addEventListener("change", () => {
      const val = radio.value;
      document.getElementById("c3po-share-optional").style.display  = val === "none" ? "none" : "flex";
      document.getElementById("c3po-share-submit").disabled          = val === "none";
      document.getElementById("c3po-share-public-note").style.display = val === "public" ? "block" : "none";
    });
  });

  window.submitShare = async function () {
    if (shareSubmitted) return;
    const mode = document.querySelector("input[name='c3po-share-mode']:checked")?.value;
    if (!mode || mode === "none") return;
    const submitBtn = document.getElementById("c3po-share-submit");
    const doneEl    = document.getElementById("c3po-share-done");
    submitBtn.disabled = true; submitBtn.textContent = "Submitting…";
    const payload = {
      shareMode: mode,
      turns:     turns.map(t => ({ q: t.q, answer: t.answer })),
      sources:   [...allSources.values()].map(s => ({ title: s.title, url: s.url, source: s.source, date: s.date, primary_author: s.primary_author })),
      rating:    shareRating || null,
      review:    document.getElementById("c3po-share-review").value.trim() || null,
      userName:  document.getElementById("c3po-share-name").value.trim()   || null,
      userEmail: document.getElementById("c3po-share-email").value.trim()  || null,
    };
    try {
      const res  = await fetch(SHARE_API, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      const data = await res.json();
      if (!res.ok || data.error) {
        doneEl.textContent = data.error || "Submission unavailable — transcript sharing coming soon.";
        doneEl.style.color = "#a00"; doneEl.style.display = "block";
        submitBtn.disabled = false; submitBtn.textContent = "Submit";
      } else {
        shareSubmitted = true;
        doneEl.textContent = mode === "public"
          ? "Thanks — your chat is under review and may be published on protocolized.io."
          : "Thanks — your chat is stored privately and will help improve C3PO.";
        doneEl.style.color = "#3a6e28"; doneEl.style.display = "block";
        submitBtn.style.display = "none";
      }
    } catch {
      doneEl.textContent = "Network error — please try again.";
      doneEl.style.color = "#a00"; doneEl.style.display = "block";
      submitBtn.disabled = false; submitBtn.textContent = "Submit";
    }
  };

  function resetShareSection() {
    if (shareSection) { shareSection.style.display = "none"; shareSection.classList.remove("cta"); }
    shareRating = 0; shareSubmitted = false; updateStars();
    const modeNone = document.querySelector("input[name='c3po-share-mode'][value='none']");
    if (modeNone) modeNone.checked = true;
    document.getElementById("c3po-share-optional").style.display    = "none";
    document.getElementById("c3po-share-public-note").style.display  = "none";
    const submitBtn = document.getElementById("c3po-share-submit");
    submitBtn.disabled = true; submitBtn.textContent = "Submit"; submitBtn.style.display = "";
    document.getElementById("c3po-share-done").style.display = "none";
    document.getElementById("c3po-share-review").value = "";
    document.getElementById("c3po-share-name").value   = "";
    document.getElementById("c3po-share-email").value  = "";
    const det = document.getElementById("c3po-share-details");
    if (det) det.removeAttribute("open");
  }

  // ── Offline ──────────────────────────────────────────────────────────────────
  function showOfflineState() {
    const offlineEl = document.getElementById("c3po-offline");
    const formEl    = document.getElementById("c3po-form");
    if (offlineEl) offlineEl.style.display = "block";
    if (formEl)    formEl.style.display    = "none";
    indicator.style.display = "none";
    statusEl.textContent = "";
    btn.disabled = input.disabled = true;
  }

  // ── Stats ────────────────────────────────────────────────────────────────────
  (async function loadStats() {
    const el = document.getElementById("c3po-stats");
    if (!el) return;
    try {
      const res  = await fetch(STATS_API);
      if (!res.ok) return;
      const data = await res.json();
      if (data.sleeping) { showOfflineState(); return; }
      const d   = data.day      || {};
      const lt  = data.lifetime || {};
      const ses = data.sessions || {};
      const dayReqs  = d.reqs         ?? 0;
      const dayTotal = d.cost_usd     ?? 0;
      const ltReqs   = lt.reqs        ?? 0;
      const ltTotal  = lt.cost_usd    ?? 0;
      const sessLife = ses.lifetime   ?? 0;
      const hourLim  = data.hour_limit_usd != null ? "$" + data.hour_limit_usd.toFixed(2) : "—";
      const dayLim   = data.day_limit_usd  != null ? "$" + data.day_limit_usd.toFixed(2)  : "—";

      let daysLive = "—";
      if (data.launched_at) {
        const launch = new Date(data.launched_at + "T00:00:00Z");
        const today  = new Date(); today.setUTCHours(0,0,0,0);
        daysLive = Math.round((today - launch) / 86400000);
      }

      const DROID_STATS = '<svg class="c3po-droid-icon" viewBox="0 0 40 46" xmlns="http://www.w3.org/2000/svg" aria-hidden="true" fill="currentColor"><rect x="18" y="0" width="4" height="6" rx="2"/><circle cx="20" cy="1" r="2.5"/><rect x="7" y="6" width="26" height="22" rx="5"/><ellipse cx="15" cy="15" rx="4" ry="4" fill="#f0ede6"/><ellipse cx="25" cy="15" rx="4" ry="4" fill="#f0ede6"/><rect x="12" y="22" width="4" height="4" rx="0.5" fill="#f0ede6"/><rect x="18" y="22" width="4" height="4" rx="0.5" fill="#f0ede6"/><rect x="24" y="22" width="4" height="4" rx="0.5" fill="#f0ede6"/><rect x="15" y="28" width="10" height="5" rx="1"/><rect x="9" y="33" width="22" height="10" rx="4"/></svg>';

      function row(label, val) {
        return '<span class="csg-label">' + label + '</span><span class="csg-val">' + val + '</span><span></span>';
      }

      el.innerHTML =
        '<div class="c3po-stats-badge">' +
          '<span class="c3po-profile-name">PI</span>' +
          DROID_STATS +
          '<span class="c3po-profile-name">C3PO</span>' +
        '</div>' +
        '<div class="c3po-stats-body">' +
          '<div class="c3po-stats-title">&#x25B8; C3PO :: health</div>' +
          '<div class="c3po-stats-headline">Live ' + daysLive + 'd &nbsp;&middot;&nbsp; $' + ltTotal.toFixed(2) + ' lifetime</div>' +
          '<div class="c3po-stats-grid">' +
            '<span class="csg-head"></span><span class="csg-head">TODAY</span><span></span>' +
            row("queries", dayReqs) +
            row("cost", "$" + dayTotal.toFixed(3)) +
            row("sessions", sessLife + " total") +
          '</div>' +
          '<div class="c3po-stats-footer">Hourly limit ' + hourLim + ' &middot; Daily ' + dayLim + ' &middot; resets midnight PT</div>' +
        '</div>';
    } catch (_) {}
  })();

  // ── Helpers ──────────────────────────────────────────────────────────────────
  function escHtml(str) {
    return String(str)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

})();

window.toggleMcpPanel = function () {
  var panel = document.getElementById("c3po-mcp-panel");
  var btn   = document.getElementById("c3po-mcp-btn");
  var open  = panel.style.display === "block";
  panel.style.display = open ? "none" : "block";
  btn.textContent = open ? "Connect via MCP" : "Hide MCP setup";
};

window.copyMcp = function (id, btn) {
  var text = document.getElementById(id).textContent;
  navigator.clipboard.writeText(text).then(function () {
    btn.textContent = "Copied!";
    setTimeout(function () { btn.textContent = "Copy"; }, 2000);
  });
};
</script>
</body>
</html>`;

// ── Worker ─────────────────────────────────────────────────────────────────────

export default {
  async fetch(request, env, ctx) {
    const url    = new URL(request.url);
    const origin = request.headers.get("Origin") || "";
    const corsHeaders = {
      "Access-Control-Allow-Origin":  origin || "*",
      "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type, X-Admin-Key",
    };

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders });
    }

    // ── GET / → serve UI ────────────────────────────────────────────────────
    if (request.method === "GET" && url.pathname === "/") {
      return new Response(UI_HTML, {
        headers: { "Content-Type": "text/html; charset=UTF-8", "Cache-Control": "public, max-age=300" },
      });
    }

    // ── GET /health ──────────────────────────────────────────────────────────
    if (request.method === "GET" && url.pathname === "/health") {
      if (!env.PINECONE_C3PO_HOST || !env.PINECONE_API_KEY) {
        return json({ status: "unconfigured" }, 200, corsHeaders);
      }
      try {
        const res = await fetch(`${env.PINECONE_C3PO_HOST}/describe_index_stats`, {
          method:  "GET",
          headers: { "Api-Key": env.PINECONE_API_KEY },
        });
        const data = await res.json();
        return json({ status: "ok", index: data }, 200, corsHeaders);
      } catch (err) {
        return json({ status: "error", message: String(err) }, 502, corsHeaders);
      }
    }

    // ── GET /stats ───────────────────────────────────────────────────────────
    if (request.method === "GET" && url.pathname === "/stats") {
      return handleStats(env, corsHeaders);
    }

    // ── POST /share (stub — D1 not yet provisioned) ──────────────────────────
    if (request.method === "POST" && url.pathname === "/share") {
      return json({ error: "Transcript sharing not yet available (Phase 2C)." }, 503, corsHeaders);
    }

    // ── GET /search — semantic search without LLM ────────────────────────────
    if (url.pathname === "/search") {
      if (request.method !== "GET" && request.method !== "POST") {
        return json({ error: "Use GET ?q= or POST { query }" }, 405, corsHeaders);
      }
      let query;
      if (request.method === "POST") {
        try { const b = await request.json(); query = (b.query || "").trim(); } catch { return json({ error: "Invalid JSON" }, 400, corsHeaders); }
      } else {
        query = (url.searchParams.get("q") || "").trim();
      }
      if (!query) return json({ error: "Missing query" }, 400, corsHeaders);
      if (query.length > 500) return json({ error: "Query too long" }, 400, corsHeaders);

      try {
        const voyageRes = await fetch(VOYAGE_URL, {
          method:  "POST",
          headers: { "Authorization": `Bearer ${env.VOYAGE_API_KEY}`, "Content-Type": "application/json" },
          body:    JSON.stringify({ input: [query], model: VOYAGE_MODEL, input_type: "query" }),
        });
        if (!voyageRes.ok) return json({ error: "Embedding error" }, 502, corsHeaders);
        const qv = (await voyageRes.json()).data[0].embedding;

        const [pdfRaw, subRaw] = await Promise.all([
          queryNamespace(env.PINECONE_C3PO_HOST, env.PINECONE_API_KEY, qv, TOP_K_EACH, "pdfs"),
          queryNamespace(env.PINECONE_C3PO_HOST, env.PINECONE_API_KEY, qv, TOP_K_EACH, "substack"),
        ]);
        const sources = mergeResults(
          pdfRaw.map(normalizePdf),
          subRaw.map(normalizeSubstack),
          MAX_SOURCES
        ).map(({ weightedScore, ...rest }) => rest);

        return json({ sources, query }, 200, corsHeaders);
      } catch (err) {
        console.error("Search error:", err);
        return json({ error: "Internal error" }, 500, corsHeaders);
      }
    }

    // ── POST /query — main RAG endpoint ─────────────────────────────────────
    if (url.pathname !== "/query") {
      return json({ error: "Not found" }, 404, corsHeaders);
    }
    if (request.method !== "POST") {
      return json({ error: "POST /query only" }, 405, corsHeaders);
    }

    let query, mode, history;
    try {
      const body = await request.json();
      query   = (body.query || "").trim();
      mode    = body.mode || "answer";
      const raw = Array.isArray(body.history) ? body.history : [];
      history = raw
        .filter(m => (m.role === "user" || m.role === "assistant") && typeof m.content === "string")
        .slice(-10)
        .map(m => ({ role: m.role, content: m.content.slice(0, 2000) }));
    } catch {
      return json({ error: "Invalid JSON body" }, 400, corsHeaders);
    }

    if (!query)              return json({ error: "Missing 'query'" }, 400, corsHeaders);
    if (query.length > 500)  return json({ error: "Query too long (max 500 chars)" }, 400, corsHeaders);
    if (!["answer", "sources"].includes(mode)) return json({ error: "mode must be 'answer' or 'sources'" }, 400, corsHeaders);

    if (INJECTION_RE.test(query)) {
      return json({
        answer: "I'm C3PO, the Protocol Institute's research assistant. My corpus is the Institute's research library and Protocolized magazine. If you have a question about protocol theory, research, or the corpus, I'm here for it.",
        sources: [],
        query,
      }, 200, corsHeaders);
    }

    const ip = request.headers.get("CF-Connecting-IP") || "unknown";
    const [circuit, rateOk] = await Promise.all([
      env.RATE_LIMIT ? env.RATE_LIMIT.get("circuit", "json") : Promise.resolve(null),
      mode === "answer" ? checkRateLimit(env, ip) : Promise.resolve(true),
    ]);

    if (circuit?.sleeping) {
      return json({ error: "C3PO is temporarily sleeping due to API budget limits. Please try again later.", sleeping: true }, 503, corsHeaders);
    }
    if (mode === "answer" && !rateOk) {
      return json({ error: "Rate limit exceeded. Please try again in an hour." }, 429, corsHeaders);
    }
    if (mode === "answer" && history.length === 0) {
      ctx.waitUntil(trackSessionStart(env));
    }

    try {
      // ── 1. Embed query ─────────────────────────────────────────────────────
      const voyageRes = await fetch(VOYAGE_URL, {
        method:  "POST",
        headers: { "Authorization": `Bearer ${env.VOYAGE_API_KEY}`, "Content-Type": "application/json" },
        body:    JSON.stringify({ input: [query], model: VOYAGE_MODEL, input_type: "query" }),
      });
      if (!voyageRes.ok) {
        console.error("Voyage error:", await voyageRes.text());
        return json({ error: "Embedding service error" }, 502, corsHeaders);
      }
      const qv = (await voyageRes.json()).data[0].embedding;

      // ── 2. Query both namespaces ───────────────────────────────────────────
      const [pdfRaw, subRaw] = await Promise.all([
        queryNamespace(env.PINECONE_C3PO_HOST, env.PINECONE_API_KEY, qv, TOP_K_EACH, "pdfs"),
        queryNamespace(env.PINECONE_C3PO_HOST, env.PINECONE_API_KEY, qv, TOP_K_EACH, "substack"),
      ]);

      // Secondary retrieval: doc_summary / post_summary hits surface well for title queries
      // but contain only abstracts — fetch real body chunks for LLM context.
      const pdfSummaryHits = pdfRaw.filter(m => m.metadata?.chunk_type === "doc_summary");
      const subSummaryHits = subRaw.filter(m => m.metadata?.chunk_type === "post_summary");

      const secondaryFetches = [
        ...pdfSummaryHits.map(hit => {
          const stem = hit.id.replace("__doc_summary", "");
          const pdfUrl = (hit.metadata?.url) || `/resources/${stem}.pdf`;
          return queryNamespace(
            env.PINECONE_C3PO_HOST, env.PINECONE_API_KEY, qv, 4, "pdfs",
            { url: { "$eq": pdfUrl.startsWith("/resources/") ? pdfUrl : `/resources/${stem}.pdf` }, chunk_type: { "$eq": "body" } }
          );
        }),
        ...subSummaryHits.map(hit =>
          queryNamespace(
            env.PINECONE_C3PO_HOST, env.PINECONE_API_KEY, qv, 4, "substack",
            { slug: { "$eq": hit.metadata.slug }, chunk_type: { "$ne": "post_summary" } }
          )
        ),
      ];

      let pdfAugmented = pdfRaw, subAugmented = subRaw;
      if (secondaryFetches.length > 0) {
        const secondary = await Promise.all(secondaryFetches);
        const flat = secondary.flat();
        // Remove the summary hits and add their body-chunk replacements
        const pdfSumIds = new Set(pdfSummaryHits.map(h => h.id));
        const subSumIds = new Set(subSummaryHits.map(h => h.id));
        pdfAugmented = [...pdfRaw.filter(m => !pdfSumIds.has(m.id)), ...flat.filter(m => m.metadata?.namespace === "pdfs" || m.metadata?.source === "pdf")];
        subAugmented = [...subRaw.filter(m => !subSumIds.has(m.id)), ...flat.filter(m => m.metadata?.source === "substack")];
      }

      const pdfNorm = pdfAugmented.map(normalizePdf);
      const subNorm = subAugmented.map(normalizeSubstack);
      const topItems = mergeResults(pdfNorm, subNorm, MAX_SOURCES);
      const sources  = topItems.map(({ weightedScore, ...rest }) => rest);

      if (mode === "sources") {
        return json({ sources, query }, 200, corsHeaders);
      }

      // ── 3. Build context block ─────────────────────────────────────────────
      const contextBlock = buildContextBlock(topItems);

      // ── 4. Call Claude Sonnet ──────────────────────────────────────────────
      const userMessage = `Question: ${query}\n\nRelevant corpus excerpts:\n\n${contextBlock}`;
      const maxTokens   = parseInt(env.MAX_ANSWER_TOKENS || "800");

      const claudeRes = await fetch(CLAUDE_URL, {
        method:  "POST",
        headers: {
          "x-api-key":         env.ANTHROPIC_API_KEY,
          "anthropic-version": ANTHROPIC_VER,
          "anthropic-beta":    "prompt-caching-2024-07-31",
          "Content-Type":      "application/json",
        },
        body: JSON.stringify({
          model:      CLAUDE_MODEL,
          max_tokens: maxTokens,
          system:     [{ type: "text", text: SYSTEM_PROMPT, cache_control: { type: "ephemeral" } }],
          messages:   [...history, { role: "user", content: userMessage }],
        }),
      });

      if (!claudeRes.ok) {
        console.error("Claude error:", await claudeRes.text());
        return json({ error: "Oracle service error" }, 502, corsHeaders);
      }

      const claudeBody = await claudeRes.json();
      const answer = claudeBody.content?.[0]?.text || "";
      const turn   = history.length + 1;
      ctx.waitUntil(trackRequest(env, claudeBody.usage));

      return json({ answer, sources, query }, 200, corsHeaders);

    } catch (err) {
      console.error("Unhandled error:", err);
      return json({ error: "Internal server error" }, 500, corsHeaders);
    }
  },

  async scheduled(event, env, ctx) {
    if (!env.RATE_LIMIT) return;
    const now = new Date();

    // Auto-reset hourly circuit trips
    const circuit = await env.RATE_LIMIT.get("circuit", "json");
    if (circuit?.sleeping && circuit.type !== "daily") {
      const tripHour = (circuit.since || "").slice(0, 13);
      const curHour  = now.toISOString().slice(0, 13);
      if (tripHour < curHour) {
        const curStats   = await env.RATE_LIMIT.get(hourKey(), "json") || {};
        const breakerUsd = parseFloat(env.BREAKER_THRESHOLD_USD || "4.00");
        if (calcTotalCost({ ...{ reqs: 0, in_tok: 0, out_tok: 0, cache_create_tok: 0, cache_read_tok: 0 }, ...curStats }) < breakerUsd) {
          await env.RATE_LIMIT.delete("circuit");
          await sendTelegram(env, `<b>C3PO awake</b> — circuit auto-reset at start of new hour`);
        }
      }
    }

    // Daily summary at midnight PT (8 AM UTC)
    if (now.getUTCHours() === 8) {
      const yesterday = new Date(Date.now() - 8 * 3600 * 1000 - 86400000).toISOString().slice(0, 10);
      const d = await env.RATE_LIMIT.get("stats:day:" + yesterday, "json");
      const od = { reqs: 0, in_tok: 0, cache_create_tok: 0, cache_read_tok: 0, out_tok: 0, ...(d || {}) };
      await sendTelegram(env,
        `<b>C3PO daily report</b> (${yesterday} PT)\n` +
        `Queries: ${od.reqs} · $${calcTotalCost(od).toFixed(3)}\n` +
        `Tokens: ${(od.in_tok||0).toLocaleString()} in / ${(od.out_tok||0).toLocaleString()} out\n` +
        `Cache: ${(od.cache_read_tok||0).toLocaleString()} read / ${(od.cache_create_tok||0).toLocaleString()} write`
      );
    }
  },
};

// ── Helpers ────────────────────────────────────────────────────────────────────

function json(body, status = 200, extra = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...extra },
  });
}
