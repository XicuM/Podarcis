'''Unit tests for modular Podarcis jobs engine and crontab synchronization.'''

from pathlib import Path
import sys

root_dir = Path(__file__).resolve().parent.parent.parent
pod_dir = root_dir / '.podarcis'
if str(pod_dir) not in sys.path:
    sys.path.insert(0, str(pod_dir))

from jobs import discover_jobs, run_job, set_job_status


def test_discover_jobs():
    jobs = discover_jobs(root_dir)
    assert 'gdrive_sync' in jobs
    assert 'audit_wiki' in jobs
    assert jobs['gdrive_sync']['type'] == 'python'
    assert jobs['audit_wiki']['type'] == 'shell'


def test_run_job_dry_run():
    res_gdrive = run_job(root_dir, 'gdrive_sync', dry_run=True)
    assert res_gdrive['status'] == 'dry_run'

    res_audit = run_job(root_dir, 'audit_wiki', dry_run=True)
    assert res_audit['status'] == 'dry_run'


def test_job_state_toggle(tmp_path):
    # Ensure state directory exists
    (tmp_path / '.podarcis').mkdir(parents=True, exist_ok=True)
    (tmp_path / '.agents' / 'jobs').mkdir(parents=True, exist_ok=True)

    # Create dummy job spec
    spec_path = tmp_path / '.agents' / 'jobs' / 'test_job.yaml'
    spec_path.write_text(
        'name: test_job\ndescription: Test Job\nschedule: "0 1 * * *"\nenabled: false\ntype: shell\ncommand: echo hello\n',
        encoding='utf-8',
    )

    discovered = discover_jobs(tmp_path)
    assert 'test_job' in discovered
    assert discovered['test_job']['enabled'] is False

    ok, msg = set_job_status(tmp_path, 'test_job', True)
    assert ok or 'crontab' in msg.lower()

    discovered_after = discover_jobs(tmp_path)
    assert discovered_after['test_job']['enabled'] is True
