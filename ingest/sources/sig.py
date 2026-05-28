import json
import subprocess
import sys
from pathlib import Path

from ingest.base import BaseSource, IngestResult

_ROOT = Path(__file__).parent.parent.parent
_SCRIPT = _ROOT / "ingest" / "sync_sig.py"
_STATE = _ROOT / "data" / "sig_state.json"


class SIGSource(BaseSource):
    source_id = "sig"
    namespace = "sig"
    ownership = "owned"

    def run(self, dry_run: bool = False, **kwargs) -> IngestResult:
        cmd = [sys.executable, str(_SCRIPT)]
        if dry_run:
            cmd.append("--dry-run")
        if kwargs.get("backfill"):
            cmd.append("--backfill")
        if channel := kwargs.get("channel"):
            cmd += ["--channel", str(channel)]
        subprocess.run(cmd, check=True, cwd=_ROOT)
        return IngestResult(source_id=self.source_id, dry_run=dry_run)

    def status(self) -> dict:
        if not _STATE.exists():
            return {}
        data = json.loads(_STATE.read_text())
        return {
            "channel_count": len(data.get("channels", {})),
            "last_run": data.get("last_run"),
        }

    def supports_incremental(self) -> bool:
        return True
