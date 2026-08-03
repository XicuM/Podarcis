'''Tests for GDrive delta sync engine and state management.'''

from pathlib import Path
import sys

root_dir = Path(__file__).resolve().parent.parent.parent
pod_dir = root_dir / '.podarcis'
if str(pod_dir) not in sys.path:
    sys.path.insert(0, str(pod_dir))

from gdrive_sync import (
    get_last_sync,
    update_last_sync,
    build_gdrive_query,
    run_gdrive_ingestion,
)


def test_gdrive_query_construction():
    q_empty = build_gdrive_query('')
    assert q_empty == 'trashed = false'

    q_ts = build_gdrive_query('2026-08-02T02:00:00Z')
    assert "modifiedTime > '2026-08-02T02:00:00Z'" in q_ts
    assert 'trashed = false' in q_ts

    q_folder = build_gdrive_query('2026-08-02T02:00:00Z', folder_id='folder_123')
    assert "'folder_123' in parents" in q_folder


def test_gdrive_state_persistence(tmp_path):
    assert get_last_sync(tmp_path) == ''

    ts = '2026-08-03T02:00:00Z'
    update_last_sync(tmp_path, ts)
    assert get_last_sync(tmp_path) == ts


def test_gdrive_dry_run(tmp_path):
    res = run_gdrive_ingestion(tmp_path, dry_run=True)
    assert res['status'] == 'dry_run'
    assert res['processed_files'] == 0
