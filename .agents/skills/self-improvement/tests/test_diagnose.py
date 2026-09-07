'''Unit tests for self-improvement session diagnosis script.'''

import json, pytest
from pathlib import Path
from argparse import Namespace

import sys
SCRIPT_DIR = Path(__file__).resolve().parent.parent / 'scripts'
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from diagnose_session import (
    ensure_diagnostics_dirs,
    parse_transcript,
    log_pain_points,
    get_active_issues,
    resolve_issues,
)


def test_ensure_diagnostics_dirs(tmp_path):
    '''Test creation of .podarcis/diagnostics directories.'''
    diag_dir = ensure_diagnostics_dirs(tmp_path)
    assert diag_dir.exists()
    assert (diag_dir / 'sessions').exists()


def test_parse_transcript_and_logging(tmp_path):
    '''Test parsing transcript lines and writing pain points to .podarcis/diagnostics.'''
    t_file = tmp_path / 'transcript.jsonl'
    lines = [
        json.dumps({'type': 'USER_INPUT', 'content': 'Hello'}),
        json.dumps({'type': 'PLANNER_RESPONSE', 'status': 'ERROR', 'content': 'The command failed with exit code: 1\nTraceback...'}),
        json.dumps({'type': 'USER_INPUT', 'content': 'No, wrong format!'})
    ]
    t_file.write_text('\n'.join(lines), encoding='utf-8')

    points = parse_transcript(t_file)
    assert len(points) == 2
    assert points[0]['category'] == 'command_failure'
    assert points[1]['category'] == 'user_correction'

    log_file = log_pain_points(points, base_dir=tmp_path)
    assert log_file.exists()

    issues = get_active_issues(base_dir=tmp_path)
    assert len(issues) == 2
    assert issues[0]['resolved'] is False

    cleared = len(resolve_issues(base_dir=tmp_path, sweep=True))
    assert cleared == 2
    assert len(get_active_issues(base_dir=tmp_path)) == 0


def test_pr_scope_validation():
    '''Test that PR scope forbids workspace, wiki, sources, and .env files.'''
    from sanitizer import validate_pr_scope

    # Valid platform changes
    valid, violations = validate_pr_scope([
        '.agents/agents/researcher.md',
        '.agents/skills/self-improvement/SKILL.md',
        '.podarcis/cli.py',
        'AGENTS.md'
    ])
    assert valid is True
    assert len(violations) == 0

    # Forbidden domain / secret changes
    invalid, violations = validate_pr_scope([
        '.agents/agents/researcher.md',
        'workspace/profile.md',
        'wiki/nutrition/creatine.md',
        'sources/literature/paper.pdf',
        '.env.local'
    ])
    assert invalid is False
    assert len(violations) == 4


def test_prepare_pr_dry_run(tmp_path, monkeypatch):
    '''Test prepare_pr dry-run and sanitization.'''
    from prepare_pr import prepare_pr

    # Mock git status returning valid platform files
    monkeypatch.setattr('prepare_pr.get_git_status_files', lambda root: ['.agents/agents/researcher.md', '.podarcis/cli.py'])

    success, msg = prepare_pr(
        title='Fix api_key=sk-1234567890abcdef1234567890 in /home/xicu/test',
        description='Fixing token Bearer abcdef1234567890abcdef1234567890 for user@test.com',
        dry_run=True,
        base_dir=tmp_path
    )

    assert success is True
    assert '[DRY RUN] Scope valid' in msg
    assert 'sk-1234567890abcdef1234567890' not in msg
    assert '[REDACTED_API_KEY]' in msg
    assert 'user@test.com' not in msg
    assert '[REDACTED_EMAIL]' in msg
    assert '<HOME>' in msg



def _point(pid: str, category: str, summary: str) -> dict:
    '''A pain point in the shape parse_transcript and diagnostics_log write.'''
    return {
        'id': pid, 'timestamp': '2026-09-07T00:00:00+00:00', 'category': category,
        'summary': summary, 'details': '', 'severity': 'medium', 'resolved': False,
    }


def test_resolve_selectors_leave_unrelated_issues_untouched(tmp_path):
    '''Resolving by id or category must not sweep the whole log.'''
    log_pain_points([
        _point('diag-1', 'command_failure', 'lint crashed'),
        _point('diag-2', 'user_correction', 'wrong footnote style'),
        _point('diag-3', 'user_correction', 'missing frontmatter'),
    ], base_dir=tmp_path)
    assert len(get_active_issues(base_dir=tmp_path)) == 3

    assert resolve_issues(base_dir=tmp_path, ids=['diag-nope']) == []
    assert len(get_active_issues(base_dir=tmp_path)) == 3

    assert len(resolve_issues(base_dir=tmp_path, category='user_correction')) == 2
    remaining = get_active_issues(base_dir=tmp_path)
    assert [r['category'] for r in remaining] == ['command_failure']

    log_file = tmp_path / '.podarcis' / 'diagnostics' / 'pain_points.jsonl'
    records = [json.loads(l) for l in log_file.read_text().splitlines() if l.strip()]
    assert len(records) == 3, 'resolved records must be retained, not deleted'
    assert all('resolved_at' in r for r in records if r['resolved'])

    assert len(resolve_issues(base_dir=tmp_path, sweep=True)) == 1
    assert get_active_issues(base_dir=tmp_path) == []


def test_malformed_line_is_never_dropped_on_rewrite(tmp_path):
    '''A corrupt line must raise, not vanish when the log is rewritten.'''
    log_pain_points([_point('diag-1', 'friction', 'real issue')], base_dir=tmp_path)
    log_file = tmp_path / '.podarcis' / 'diagnostics' / 'pain_points.jsonl'
    with open(log_file, 'a', encoding='utf-8') as f:
        f.write('{not json at all\n')

    with pytest.raises(json.JSONDecodeError):
        resolve_issues(base_dir=tmp_path, sweep=True)

    text = log_file.read_text(encoding='utf-8')
    assert '{not json at all' in text and 'real issue' in text
