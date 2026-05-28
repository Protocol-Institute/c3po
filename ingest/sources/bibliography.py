import subprocess
import sys
from pathlib import Path

from ingest.base import BaseSource, IngestResult

_ROOT = Path(__file__).parent.parent.parent
_INGEST_SCRIPT = _ROOT / "ingest" / "ingest_bibliography.py"
_FETCH_SCRIPT = _ROOT / "ingest" / "fetch_refs.py"


class BibliographySource(BaseSource):
    source_id = "bibliography"
    namespace = "bibliography"
    ownership = "owned"

    def run(self, dry_run: bool = False, **kwargs) -> IngestResult:
        """
        Pipeline: fetch_refs (optional) → ingest_bibliography.
        Pass fetch=True to run Semantic Scholar enrichment first.
        """
        if kwargs.get("fetch", False):
            subprocess.run([sys.executable, str(_FETCH_SCRIPT)], check=True, cwd=_ROOT)

        cmd = [sys.executable, str(_INGEST_SCRIPT)]
        if dry_run:
            cmd.append("--dry-run")
        if ref_id := kwargs.get("id"):
            cmd += ["--id", ref_id]
        if type_ := kwargs.get("type"):
            cmd += ["--type", type_]
        subprocess.run(cmd, check=True, cwd=_ROOT)
        return IngestResult(source_id=self.source_id, dry_run=dry_run)
