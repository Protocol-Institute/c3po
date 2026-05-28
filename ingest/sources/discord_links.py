import subprocess
import sys
from pathlib import Path

from ingest.base import BaseSource, IngestResult

_ROOT = Path(__file__).parent.parent.parent
_FETCH_SCRIPT = _ROOT / "ingest" / "fetch_discord_links.py"
_ENRICH_SCRIPT = _ROOT / "ingest" / "enrich_discord_links.py"


class DiscordLinksSource(BaseSource):
    """
    Two-step pipeline: fetch_discord_links (fetch pending URLs) →
    enrich_discord_links (score, prune, embed).

    Links are extracted from Discord messages by sync_discord.py and
    appended to data/discord_links_registry.json. This source processes
    the pending queue.
    """
    source_id = "discord_links"
    namespace = "discord_links"
    ownership = "owned"

    def run(self, dry_run: bool = False, **kwargs) -> IngestResult:
        fetch_cmd = [sys.executable, str(_FETCH_SCRIPT)]
        if dry_run:
            fetch_cmd.append("--dry-run")
        subprocess.run(fetch_cmd, check=True, cwd=_ROOT)

        enrich_cmd = [sys.executable, str(_ENRICH_SCRIPT)]
        if dry_run:
            enrich_cmd.append("--dry-run")
        subprocess.run(enrich_cmd, check=True, cwd=_ROOT)

        return IngestResult(source_id=self.source_id, dry_run=dry_run)

    def supports_incremental(self) -> bool:
        return True
