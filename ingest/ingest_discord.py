"""
Phase 4 stub: Discord message ingestion.

Will capture messages from designated Protocol Institute Discord channels,
chunk by thread, and embed into Pinecone with channel/author/timestamp metadata.

Not implemented yet. Requires:
  - DISCORD_BOT_TOKEN in .env
  - DISCORD_GUILD_ID in .env
  - List of channel IDs to index (whitelist — never DMs)
  - discord.py: pip install discord.py

Design notes:
  - Index only public/designated channels, never DMs
  - Chunk by thread (group replies under parent message)
  - Discord messages are lower-priority in retrieval than peer-reviewed papers
    (enforce via metadata filter or score adjustment in the Worker)
  - Run incrementally: track last-indexed message ID per channel in data/discord_state.json
  - Privacy: store author handle (not user ID) in metadata; no DM capture ever
"""

raise NotImplementedError("Discord ingestion is Phase 4 — not yet implemented")
