'''Unit tests for podarcis CLI subcommands and non-interactive status/configuration actions.'''

import importlib
import json
import pytest
from argparse import Namespace
from pathlib import Path
from podarcis.cli import cmd_status, cmd_config_enable, cmd_config_disable, cmd_config_repo


def test_cli_status_json(capsys):
    '''Verify podarcis status --json produces valid JSON output.'''
    args = Namespace(json=True)
    res = cmd_status(args)
    assert res == 0

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert 'mcp_servers' in data
    assert 'skills' in data
    assert 'agents' in data
    assert 'repositories' in data


def test_cli_config_rejects_skill_and_agent_toggles(tmp_path, monkeypatch):
    """Skills and agents are not toggleable; the CLI must say so, not rewrite frontmatter."""
    from podarcis import cli
    monkeypatch.setattr(cli, 'ROOT_DIR', tmp_path)

    skills_dir = tmp_path / '.apm' / 'skills' / 'sample-skill'
    skills_dir.mkdir(parents=True)
    skill_file = skills_dir / 'SKILL.md'
    original = '---\ndescription: Sample Skill\n---\n\nSample body.'
    skill_file.write_text(original, encoding='utf-8')

    for ctype in ('skill', 'agent'):
        assert cmd_config_disable(Namespace(type=ctype, name='sample-skill')) == 1
        assert cmd_config_enable(Namespace(type=ctype, name='sample-skill')) == 1

    # The git-tracked source file must be left byte-for-byte untouched.
    assert skill_file.read_text(encoding='utf-8') == original


def test_cli_config_repo(tmp_path, monkeypatch):
    '''Test repo configuration CLI command for wiki, user, workspace, and custom paths.'''
    from podarcis import cli
    monkeypatch.setattr(cli, 'ROOT_DIR', tmp_path)

    # Configure wiki
    args_repo = Namespace(repo_name='wiki', url='https://github.com/example/wiki.git', path=None, local=False)
    res = cmd_config_repo(args_repo)
    assert res == 0

    from podarcis.repos import get_repo_url
    assert get_repo_url(tmp_path, 'wiki') == 'https://github.com/example/wiki.git'

    # Configure workspace repository via path or url
    args_ws = Namespace(repo_name='workspace', url='https://github.com/example/workspace.git', path=None, local=False)
    res_w = cmd_config_repo(args_ws)
    assert res_w == 0
    assert get_repo_url(tmp_path, 'workspace') == 'https://github.com/example/workspace.git'


    # Set to local
    args_local = Namespace(repo_name='wiki', url=None, path=None, local=True)
    res_loc = cmd_config_repo(args_local)
    assert res_loc == 0
    assert get_repo_url(tmp_path, 'wiki') == ''


def test_cli_diagnose(tmp_path, monkeypatch, capsys):
    '''Test podarcis diagnose subcommand output.'''
    from podarcis import cli
    monkeypatch.setattr(cli, 'ROOT_DIR', tmp_path)

    # Copy actual diagnose_session.py to tmp_path structure so import works in test
    script_dir = tmp_path / '.apm' / 'skills' / 'self-improvement' / 'scripts'
    script_dir.mkdir(parents=True)
    real_script = Path(__file__).resolve().parent.parent.parent / '.apm' / 'skills' / 'self-improvement' / 'scripts' / 'diagnose_session.py'
    (script_dir / 'diagnose_session.py').write_text(real_script.read_text(encoding='utf-8'), encoding='utf-8')

    # Run status check when no issues exist
    args = Namespace(json=True, clear=False, log_session=None)
    res = cli.cmd_diagnose(args)
    assert res == 0

    captured = capsys.readouterr()
    assert captured.out.strip() == '[]'


def test_config_frontend_obsidian(tmp_path, monkeypatch):
    '''Test configuring frontend to obsidian.'''
    from podarcis import cli
    monkeypatch.setattr(cli, 'ROOT_DIR', tmp_path)

    args = Namespace(frontend_name='obsidian')
    res = cli.cmd_config_frontend(args)
    assert res == 0
    from podarcis.common import get_config_value
    assert get_config_value(tmp_path, 'frontend') == 'obsidian'




def test_cli_default_opens_frontend(tmp_path, monkeypatch):
    '''Verify that running podarcis without subcommands opens frontend directly.'''
    from podarcis import cli
    monkeypatch.setattr(cli, 'ROOT_DIR', tmp_path)

    opened = []
    monkeypatch.setattr(cli, 'cmd_frontend', lambda args: opened.append(True) or 0)
    monkeypatch.setattr('sys.argv', ['podarcis'])

    try:
        cli.main()
    except SystemExit as e:
        assert e.code == 0

    assert len(opened) == 1


def test_cli_research_search_json(capsys, monkeypatch):
    '''Test podarcis research search --json subcommand.'''
    from podarcis import cli
    res_script = cli.ROOT_DIR / '.agents' / 'mcp' / 'research' / 'server.py'
    import importlib.util
    spec = importlib.util.spec_from_file_location('research_mcp_server', res_script)
    research_server = importlib.util.module_from_spec(spec)
    import sys
    sys.modules['research_mcp_server'] = research_server
    spec.loader.exec_module(research_server)


    async def fake_search(query, limit=5, provider='all'):
        return [{'paperId': 'test:123', 'title': 'Test Paper', 'year': 2024}]

    monkeypatch.setattr(research_server, 'literature_search', fake_search)

    args = Namespace(research_action='search', query='test', limit=2, provider='all', json=True)
    res = cli.cmd_research(args)
    assert res == 0

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert len(data) == 1
    assert data[0]['title'] == 'Test Paper'



def test_cli_diagnose_resolve_id(tmp_path, monkeypatch):
    '''Test resolving specific pain point by ID via podarcis diagnose --resolve.'''
    from podarcis import cli
    monkeypatch.setattr(cli, 'ROOT_DIR', tmp_path)

    # Setup pain point file
    diag_dir = tmp_path / '.podarcis' / 'diagnostics'
    diag_dir.mkdir(parents=True)
    pain_file = diag_dir / 'pain_points.jsonl'
    rec = {'id': 'diag-test-1', 'summary': 'Test pain point', 'resolved': False}
    pain_file.write_text(json.dumps(rec) + '\n', encoding='utf-8')

    script_dir = tmp_path / '.apm' / 'skills' / 'self-improvement' / 'scripts'
    script_dir.mkdir(parents=True)
    real_script = Path(__file__).resolve().parent.parent.parent / '.apm' / 'skills' / 'self-improvement' / 'scripts' / 'diagnose_session.py'
    (script_dir / 'diagnose_session.py').write_text(real_script.read_text(encoding='utf-8'), encoding='utf-8')

    args = Namespace(resolve='diag-test-1', json=False, clear=False, log_session=None)
    res = cli.cmd_diagnose(args)
    assert res == 0

    lines = pain_file.read_text().strip().split('\n')
    data = json.loads(lines[0])
    assert data['resolved'] is True









# ── configuration is one file ────────────────────────────────────────────────

def test_single_config_file_and_legacy_state_migration(tmp_path):
    '''state.yaml folds into config.yaml, with state's values winning.

    The split cost correctness: the MCP servers read engines.qmd and
    sources_backend from config.yaml while the TUI wrote them to state.yaml,
    so neither setting ever reached the server that reads it.
    '''
    from podarcis.common import get_config_value, load_config, save_yaml, set_config_value

    pod = tmp_path / '.podarcis'
    pod.mkdir()
    save_yaml(pod / 'config.yaml', {'sources_backend': 'local', 'apis': {'k': 'v'}})
    save_yaml(pod / 'state.yaml', {'sources_backend': 'gdrive', 'jobs': {'a': {'enabled': True}}})

    cfg = load_config(tmp_path)
    assert cfg['sources_backend'] == 'gdrive'      # state took read priority
    assert cfg['apis']['k'] == 'v'                 # config-only keys survive
    assert cfg['jobs']['a']['enabled'] is True
    assert not (pod / 'state.yaml').exists()

    # Writes that used to be routed to state.yaml now land where servers read.
    set_config_value(tmp_path, True, 'engines', 'qmd')
    assert load_config(tmp_path)['engines']['qmd'] is True
    assert get_config_value(tmp_path, 'sources_backend') == 'gdrive'


def test_engine_is_imported_under_exactly_one_name():
    '''`.podarcis/` is the package `podarcis` and nothing else.

    It was previously also inserted onto sys.path by seven modules, so every
    module had two instances (`jobs.agent` and `podarcis.jobs.agent`) with
    separate copies of module-level state.
    '''
    import subprocess
    import sys

    probe = (
        'import podarcis.cli, podarcis.jobs.agent, podarcis.gateway.router, sys;'
        'shadowed = [n for n in ("cli", "common", "console", "components", "repos",'
        ' "jobs", "banner", "install", "uninstall") if n in sys.modules];'
        'print(shadowed)'
    )
    out = subprocess.run([sys.executable, '-c', probe], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == '[]', f'engine modules shadowed under bare names: {out.stdout}'
