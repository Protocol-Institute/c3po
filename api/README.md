# api/ — Cloudflare Worker (Phase 2)

Not yet implemented.

Will contain:
- `worker.js` — main Worker: embed query → Pinecone retrieve → Claude generate with SOUL.md persona
- `wrangler.toml` — CF Worker config

Endpoint: `POST /api/query`
```json
{ "query": "...", "history": [...] }
```

Response:
```json
{ "answer": "...", "sources": [...] }
```

Worker secrets (set via wrangler):
- VOYAGE_API_KEY
- PINECONE_API_KEY
- PINECONE_C3PO_HOST
- ANTHROPIC_API_KEY
- ADMIN_KEY
