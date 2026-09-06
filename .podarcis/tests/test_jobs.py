"""Unit tests for the modular jobs engine and its systemd timer backend."""

from pathlib import Path
import sys

root_dir = Path(__file__).resolve().parent.parent.parent
pod_dir = root_dir / '.podarcis'
if str(pod_dir) not in sys.path:
    sys.path.insert(0, str(pod_dir))

import pytest

from jobs import discover_jobs, run_job, scheduler
from jobs.agent import DENIED_TOOLS, READ_TOOLS, WRITE_TOOLS, _tools_for
from jobs.runners import RunSpec, get_runner
from jobs.runners.claude import ClaudeRunner


def test_discover_jobs():
    jobs = discover_jobs(root_dir)
    assert jobs['gdrive_sync']['type'] == 'python'
    assert jobs['audit_wiki']['type'] == 'shell'
    assert jobs['nightly_synthesis']['type'] == 'agent'


def test_run_job_dry_run():
    assert run_job(root_dir, 'gdrive_sync', dry_run=True)['status'] == 'dry_run'
    assert run_job(root_dir, 'audit_wiki', dry_run=True)['status'] == 'dry_run'


def test_shipped_schedules_are_valid_oncalendar():
    for name, job in discover_jobs(root_dir).items():
        ok, detail = scheduler.validate_schedule(job['schedule'])
        assert ok, f'{name}: {detail}'


def test_agent_job_dry_run_emits_argv():
    res = run_job(root_dir, 'nightly_synthesis', dry_run=True)
    assert res['status'] == 'dry_run'
    argv = res['argv']
    assert argv[:2] == ['claude', '--print']
    assert '--agent' in argv and argv[argv.index('--agent') + 1] == 'synthesizer'
    # Unattended runs must never block on a permission prompt.
    assert argv[argv.index('--permission-prompts') + 1] == 'none'


def test_agent_job_refuses_to_nest(monkeypatch):
    monkeypatch.setenv('PODARCIS_JOB_RUN', '1')
    res = run_job(root_dir, 'nightly_synthesis')
    assert res['status'] == 'error'
    assert 'nest' in res['message'].lower()


def test_report_autonomy_grants_no_write_tools():
    allowed, mode = _tools_for('report')
    assert set(allowed) == set(READ_TOOLS)
    assert mode == 'dontAsk'
    assert not set(allowed) & set(WRITE_TOOLS)


def test_write_autonomy_never_grants_bash():
    for level in ('branch', 'commit', 'push'):
        allowed, mode = _tools_for(level)
        assert mode == 'acceptEdits'
        assert set(WRITE_TOOLS) <= set(allowed)
        # git must stay with Podarcis so autonomy cannot be bypassed.
        assert 'Bash' not in allowed
    assert 'Bash' in DENIED_TOOLS


def test_unknown_autonomy_is_rejected(tmp_path):
    job = {
        'name': 'x', 'type': 'agent',
        'options': {'prompt': 'hi', 'autonomy': 'yolo'},
    }
    from jobs import agent
    res = agent.run(tmp_path, job)
    assert res['status'] == 'error' and 'yolo' in res['message']


def test_claude_argv_omits_unset_fields():
    argv = ClaudeRunner().build_argv(RunSpec(prompt='go', cwd=root_dir))
    assert '--agent' not in argv and '--model' not in argv
    assert '--max-budget-usd' not in argv
    assert argv[-1] == 'go'


def test_claude_parse_maps_error_flag():
    payload = ('{"is_error": true, "result": "boom", "total_cost_usd": 0.5,'
               ' "session_id": "s1"}')
    res = ClaudeRunner().parse(payload)
    assert res.status == 'error' and not res.ok
    assert res.cost_usd == 0.5 and res.session_id == 's1'


def test_get_runner_rejects_unknown_harness():
    with pytest.raises(RuntimeError, match='Unknown harness'):
        get_runner('nonexistent')


def test_unit_names_differ_per_checkout(tmp_path):
    a = scheduler.unit_name(tmp_path / 'one', 'job')
    b = scheduler.unit_name(tmp_path / 'two', 'job')
    assert a != b and a.endswith('-job') and b.endswith('-job')


def test_unit_rendering_is_persistent_and_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, 'UNIT_DIR', tmp_path / 'units')
    monkeypatch.setattr(scheduler, '_systemctl', lambda *a: (True, ''))
    (tmp_path / '.venv' / 'bin').mkdir(parents=True)
    (tmp_path / '.venv' / 'bin' / 'podarcis').touch()

    ok, msg = scheduler.install(tmp_path, 'demo', 'daily', timeout_s=120)
    assert ok, msg

    base = scheduler.unit_name(tmp_path, 'demo')
    timer = (tmp_path / 'units' / f'{base}.timer').read_text()
    service = (tmp_path / 'units' / f'{base}.service').read_text()
    # Persistent=true is the whole reason for choosing systemd over cron.
    assert 'Persistent=true' in timer and 'OnCalendar=daily' in timer
    assert 'RuntimeMaxSec=120' in service
    assert 'job run demo' in service
    assert scheduler.job_names(tmp_path) == ['demo']


def test_install_rejects_invalid_schedule(tmp_path):
    ok, msg = scheduler.install(tmp_path, 'demo', 'every other tuesday')
    assert not ok and 'every other tuesday' in msg or not ok


def _init_repo(path: Path) -> None:
    import subprocess
    path.mkdir(parents=True, exist_ok=True)
    for args in (
        ['init', '-b', 'master'], ['config', 'user.email', 't@t'],
        ['config', 'user.name', 't'],
    ):
        subprocess.run(['git', *args], cwd=path, capture_output=True, check=True)
    (path / 'seed.md').write_text('seed\n')
    subprocess.run(['git', 'add', '-A'], cwd=path, capture_output=True, check=True)
    subprocess.run(
        ['git', 'commit', '-m', 'seed'], cwd=path, capture_output=True, check=True,
    )


def test_branch_autonomy_commits_off_master(tmp_path, monkeypatch):
    """Agent output must land on jobs/<name>, leaving master untouched."""
    import subprocess
    from jobs import agent
    from jobs.runners import RunResult

    repo = tmp_path / 'wiki'
    _init_repo(repo)
    monkeypatch.setattr(agent, '_target_repos', lambda root: [repo])

    before = agent._worktree_state(tmp_path)
    (repo / 'new_note.md').write_text('agent output\n')

    res = agent._apply_autonomy(
        tmp_path, 'demo', 'branch', RunResult(status='success', text='done'), before,
    )
    assert res['branch'] == 'jobs/demo'

    head = subprocess.run(
        ['git', 'branch', '--show-current'], cwd=repo,
        capture_output=True, text=True,
    ).stdout.strip()
    assert head == 'master', 'must return to the original branch'
    assert not subprocess.run(
        ['git', 'status', '--porcelain'], cwd=repo, capture_output=True, text=True,
    ).stdout.strip(), 'worktree should be clean after committing to the branch'
    assert not (repo / 'new_note.md').exists(), 'output must not remain on master'

    files = subprocess.run(
        ['git', 'show', '--name-only', '--format=', 'jobs/demo'], cwd=repo,
        capture_output=True, text=True,
    ).stdout
    assert 'new_note.md' in files


def test_report_autonomy_ignores_preexisting_dirt(tmp_path, monkeypatch):
    """A tree that was already dirty is not the scheduled run's fault."""
    from jobs import agent
    from jobs.runners import RunResult

    repo = tmp_path / 'wiki'
    _init_repo(repo)
    monkeypatch.setattr(agent, '_target_repos', lambda root: [repo])

    (repo / 'seed.md').write_text('edited by the user\n')
    before = agent._worktree_state(tmp_path)

    res = agent._apply_autonomy(
        tmp_path, 'demo', 'report', RunResult(status='success', text='hi'), before,
    )
    assert 'status' not in res and Path(res['report']).exists()
