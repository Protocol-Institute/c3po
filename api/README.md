# api/ — C3PO Cloudflare Worker

## Files

| File | Description |
|------|-------------|
| `worker.js` | Oracle Worker — serves UI at `GET /`, RAG at `POST /query` |
| `wrangler.toml` | Cloudflare config |

## Routes

| Route | Description |
|-------|-------------|
| `GET /` | Web UI (embedded HTML — test page) |
| `POST /query` | RAG query: `{ query, history?, mode? }` → `{ answer, sources }` |
| `GET /search` | Semantic search only (no LLM): `?q=<query>` → `{ sources }` |
| `GET /stats` | Spend + usage stats (KV) |
| `GET /health` | Pinecone index health |
| `POST /share` | Transcript sharing (stub — 503 until D1 provisioned in Phase 2C) |

## First-time deploy

### 1. Install wrangler

```bash
npm install -g wrangler
wrangler login
```

### 2. Create KV namespace

```bash
wrangler kv namespace create RATE_LIMIT
# Copy the returned ID into wrangler.toml → [[kv_namespaces]] id = "..."
```

### 3. Set secrets

```bash
wrangler secret put VOYAGE_API_KEY
wrangler secret put PINECONE_API_KEY
wrangler secret put PINECONE_C3PO_HOST    # full URL: https://c3po-bwo39z7.svc.aped-4627-b74a.pinecone.io
wrangler secret put ANTHROPIC_API_KEY
wrangler secret put ADMIN_KEY             # generate per security-policy.md
```

Optional:
```bash
wrangler secret put TELEGRAM_BOT_TOKEN
wrangler secret put TELEGRAM_CHAT_ID
```

### 4. Deploy

```bash
cd api/
wrangler deploy
```

Worker URL: `https://c3po.<account-subdomain>.workers.dev`

## Development

```bash
wrangler dev
# Opens local dev server at http://localhost:8787
```

The UI at `GET /` will hit your local worker. API calls from the UI are same-origin (no CORS issues).

## Phase 2 roadmap

| Phase | Feature |
|-------|---------|
| 2A ✅ | Oracle Worker + web UI |
| 2B | MCP Worker (JSON-RPC 2.0) at `/mcp` |
| 2C | D1 database: query_log + transcripts + share endpoint |
| 2D | Telegram: daily cost report + circuit alerts |
| 2E | Custom domain: `c3po.protocolized.io` |

## Worker variables

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `BREAKER_THRESHOLD_USD` | env var | `4.00` | Hourly circuit breaker threshold |
| `DAY_LIMIT_USD` | env var | `30.00` | Daily spend hard limit |
| `MAX_ANSWER_TOKENS` | env var | `800` | Max tokens in Claude response |

These can be set in `wrangler.toml` under `[vars]` or as secrets.
