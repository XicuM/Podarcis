'''Unit tests for TUI token calculator and banner features.'''

from pathlib import Path
from banner import display_project_banner
from components import count_tokens, discover_components


def test_count_tokens():
    '''Verify token count helper returns reasonable counts.'''
    text = 'Hello world, token count verification test.'
    count = count_tokens(text)
    assert isinstance(count, int)
    assert count > 0


def test_discover_components_token_fields(tmp_path):
    '''Verify discover_components populates token fields for discovered skills and agents.'''
    skills_dir = tmp_path / '.agents' / 'skills' / 'demo-skill'
    skills_dir.mkdir(parents=True)
    skill_file = skills_dir / 'SKILL.md'
    skill_file.write_text('---\ndescription: Test Skill\n---\n\nDemo skill instructions.', encoding='utf-8')

    agents_dir = tmp_path / '.agents' / 'agents'
    agents_dir.mkdir(parents=True)
    agent_file = agents_dir / 'demo_agent.md'
    agent_file.write_text('---\ndescription: Test Agent\n---\n\nDemo agent instructions.', encoding='utf-8')

    _, skills, agents = discover_components(tmp_path)

    assert 'demo-skill' in skills
    info = skills['demo-skill']
    assert 'tokens' in info
    assert info['tokens'] > 0
    assert 'decl_tokens' in info
    assert info['decl_tokens'] > 0
    assert info['chars'] > 0
    assert info['words'] > 0

    assert 'demo_agent' in agents
    agent_info = agents['demo_agent']
    assert 'tokens' in agent_info
    assert agent_info['tokens'] > 0
    assert 'decl_tokens' in agent_info
    assert agent_info['decl_tokens'] > 0


def test_banner_and_calculator_run(tmp_path, capsys):
    """Banner renders the gateway tool budget and the on-demand summary line."""
    skills_dir = tmp_path / '.agents' / 'skills' / 'test-skill'
    skills_dir.mkdir(parents=True)
    (skills_dir / 'SKILL.md').write_text('# Test Skill\nSome content.', encoding='utf-8')

    agents_dir = tmp_path / '.agents' / 'agents'
    agents_dir.mkdir(parents=True)
    (agents_dir / 'test_agent.md').write_text('# Test Agent\nSome instructions.', encoding='utf-8')

    display_project_banner(tmp_path)
    captured = capsys.readouterr()
    assert 'Gateway tools' in captured.out
    # Skills and personas are not shown at all: nothing to toggle, nothing to budget.
    assert 'Skills' not in captured.out
    assert 'personas' not in captured.out


def test_mcp_discovery_ignores_dirs_without_server(tmp_path):
    """A leftover build dir (e.g. stale __pycache__) is not a phantom tool module."""
    from components import discover_components

    mcp = tmp_path / '.agents' / 'mcp'
    (mcp / 'ghost' / '__pycache__').mkdir(parents=True)
    (mcp / 'ghost' / '__pycache__' / 'server.cpython-312.pyc').write_bytes(b'\x00')
    real = mcp / 'live'
    real.mkdir(parents=True)
    (real / 'server.py').write_text('"""Live - a real module."""\n', encoding='utf-8')

    mcp_servers, _, _ = discover_components(tmp_path)
    assert set(mcp_servers) == {'live-mcp'}


def test_schedule_formatting_fits_status_column():
    """systemd OnCalendar expressions are compacted so they cannot break the border."""
    from banner import _fmt_schedule

    assert _fmt_schedule('Sun *-*-* 03:00:00') == 'Sun 03:00'
    assert _fmt_schedule('*-*-* 03:00:00') == 'daily 03:00'
    assert _fmt_schedule('daily') == 'daily'
    assert _fmt_schedule('') == ''
    assert len(_fmt_schedule('Mon,Tue,Wed,Thu,Fri *-*-* 03:00:00')) <= 14


def test_repo_rows_render_tracking_state(tmp_path):
    """Repo rows show live branch/dirty/ahead state, not just the configured URL."""
    from banner import _repo_rows, _repo_state, _short_remote

    assert _short_remote('git@github.com:XicuM/podarcis-wiki.git') == 'XicuM/podarcis-wiki'
    assert _short_remote('') == 'local'

    label, _ = _repo_state({'status': 'modified', 'changes': 1, 'ahead': 2})
    assert label == '1 change ↑2'
    assert _repo_state({'status': 'modified', 'changes': 3})[0] == '3 changes'
    assert _repo_state({'status': 'behind', 'behind': 4})[0] == '↓4'
    assert _repo_state({'status': 'synced'})[0] == 'synced'

    # Rows must fit the banner's inner width exactly, or the border breaks.
    from banner import INNER_W
    from rich.cells import cell_len
    for row in _repo_rows(tmp_path):
        assert cell_len(row.plain) == INNER_W
