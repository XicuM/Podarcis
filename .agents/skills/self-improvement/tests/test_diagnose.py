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
    clear_issues,
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

    cleared = clear_issues(base_dir=tmp_path)
    assert cleared == 2
    assert len(get_active_issues(base_dir=tmp_path)) == 0
