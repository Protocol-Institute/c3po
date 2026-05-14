# submissions/ — Submission Intake (Phase 3)

Not yet implemented.

Three submission paths:
1. URL submission — worker fetches, extracts, chunks, queues for review
2. PDF upload — stored in R2, parsed server-side, queued for review
3. GitHub PR — add markdown to protocolized-website/src/content/resources/; CI auto-ingests on merge

Review queue is admin-only. Approved submissions are indexed in Pinecone and
optionally added to the protocolized-website resource library.

See README.md Phase 3 section for full design spec.
