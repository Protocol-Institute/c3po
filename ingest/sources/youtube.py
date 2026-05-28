import json
import subprocess
import sys
from pathlib import Path

from ingest.base import BaseSource, IngestResult

_ROOT = Path(__file__).parent.parent.parent
_INGEST_SCRIPT = _ROOT / "ingest" / "ingest_youtube.py"
_FETCH_SCRIPT = _ROOT / "ingest" / "fetch_youtube_meta.py"
_ENRICH_SCRIPT = _ROOT / "ingest" / "enrich_youtube.py"
_STATE = _ROOT / "sources" / "youtube" / "registry.json"


class YouTubeSource(BaseSource):
    source_id = "youtube"
    namespace = "videos"   # historical: namespace is 'videos', not 'youtube'
    ownership = "owned"

    def run(self, dry_run: bool = False, **kwargs) -> IngestResult:
        """
        Full pipeline: fetch_youtube_meta → enrich_youtube → ingest_youtube.
        Pass fetch=False to skip fetch+enrich and only run ingest on existing data.
        """
        if kwargs.get("fetch", True):
            fetch_cmd = [sys.executable, str(_FETCH_SCRIPT)]
            if dry_run:
                fetch_cmd.append("--dry-run")
            subprocess.run(fetch_cmd, check=True, cwd=_ROOT)

            enrich_cmd = [sys.executable, str(_ENRICH_SCRIPT)]
            subprocess.run(enrich_cmd, check=True, cwd=_ROOT)

        cmd = [sys.executable, str(_INGEST_SCRIPT)]
        if dry_run:
            cmd.append("--dry-run")
        if video := kwargs.get("video"):
            cmd += ["--video", video]
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
            "video_count_indexed": data.get("video_count_indexed"),
            "last_ingested": data.get("last_ingested"),
        }
