import json
import subprocess
import sys
from pathlib import Path

from ingest.base import BaseSource, IngestResult

_ROOT = Path(__file__).parent.parent.parent
_INGEST_SCRIPT = _ROOT / "ingest" / "ingest_pdfs.py"
_ENRICH_SCRIPT = _ROOT / "ingest" / "enrich_pdfs.py"
_STATE = _ROOT / "sources" / "pdfs" / "registry.json"


class PDFSource(BaseSource):
    source_id = "pdfs"
    namespace = "pdfs"
    ownership = "owned"

    def run(self, dry_run: bool = False, **kwargs) -> IngestResult:
        """
        Pipeline: enrich_pdfs (optional) → ingest_pdfs.
        Pass enrich=False to skip enrichment and ingest from existing enriched_meta.json.
        """
        if kwargs.get("enrich", False):
            subprocess.run([sys.executable, str(_ENRICH_SCRIPT)], check=True, cwd=_ROOT)

        cmd = [sys.executable, str(_INGEST_SCRIPT)]
        if dry_run:
            cmd.append("--dry-run")
        if pdf := kwargs.get("pdf"):
            cmd += ["--pdf", pdf]
        if type_ := kwargs.get("type"):
            cmd += ["--type", type_]
        subprocess.run(cmd, check=True, cwd=_ROOT)
        return IngestResult(source_id=self.source_id, dry_run=dry_run)

    def status(self) -> dict:
        if not _STATE.exists():
            return {}
        data = json.loads(_STATE.read_text())
        return {
            "vector_count": data.get("vector_count"),
            "last_ingested": data.get("last_ingested"),
        }
