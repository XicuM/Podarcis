'''Google Drive API Delta Ingestion Job Handler.'''

from pathlib import Path
import sys

root_dir = Path(__file__).resolve().parent.parent.parent
podarcis_dir = root_dir / '.podarcis'
if str(podarcis_dir) not in sys.path:
    sys.path.insert(0, str(podarcis_dir))

from gdrive_sync import run_gdrive_ingestion


def run(root_dir: Path, dry_run: bool = False) -> dict:
    return run_gdrive_ingestion(root_dir, dry_run=dry_run)
