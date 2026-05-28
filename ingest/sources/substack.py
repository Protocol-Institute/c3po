import json
import subprocess
import sys
from pathlib import Path

from ingest.base import BaseSource, IngestResult

_ROOT = Path(__file__).parent.parent.parent
_SCRIPT = _ROOT / "ingest" / "sync_substack.py"
_STATE = _ROOT / "sources" / "substack" / "registry.json"


class SubstackSource(BaseSource):
    source_id = "substack"
    namespace = "substack"
    ownership = "owned"

    def run(self, dry_run: bool = False, **kwargs) -> IngestResult:
        cmd = [sys.executable, str(_SCRIPT)]
        if dry_run:
            cmd.append("--dry-run")
        if slug := kwargs.get("force_slug"):
            cmd += ["--force-slug", slug]
        subprocess.run(cmd, check=True, cwd=_ROOT)
        return IngestResult(source_id=self.source_id, dry_run=dry_run)

    def status(self) -> dict:
        if not _STATE.exists():
            return {}
        data = json.loads(_STATE.read_text())
        return {
            "vector_count": data.get("vector_count"),
            "last_ingested": data.get("last_ingested"),
            "last_sync_timestamp": data.get("last_sync_timestamp"),
        }

    def supports_incremental(self) -> bool:
        return True
