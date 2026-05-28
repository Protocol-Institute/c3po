import subprocess
import sys
from pathlib import Path

from ingest.base import BaseSource, IngestResult

_ROOT = Path(__file__).parent.parent.parent
_SCRIPT = _ROOT / "ingest" / "sync_lexicon.py"


class DefinitionsSource(BaseSource):
    source_id = "definitions"
    namespace = "definitions"
    ownership = "owned"

    def run(self, dry_run: bool = False, **kwargs) -> IngestResult:
        cmd = [sys.executable, str(_SCRIPT)]
        if dry_run:
            cmd.append("--dry-run")
        subprocess.run(cmd, check=True, cwd=_ROOT)
        return IngestResult(source_id=self.source_id, dry_run=dry_run)
