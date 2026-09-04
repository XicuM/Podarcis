'''Unit tests for podarcis CLI subcommands and non-interactive status/configuration actions.'''

import json
from argparse import Namespace
from pathlib import Path
from cli import cmd_status, cmd_config_enable, cmd_config_disable, cmd_config_repo


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


def test_cli_config_enable_disable_skill(tmp_path, monkeypatch):
    '''Test enabling and disabling skills via CLI commands.'''
    import cli
    monkeypatch.setattr(cli, 'root_dir', tmp_path)

    skills_dir = tmp_path / '.agents' / 'skills' / 'sample-skill'
    skills_dir.mkdir(parents=True)
    skill_file = skills_dir / 'SKILL.md'
    skill_file.write_text('---\ndescription: Sample Skill\n---\n\nSample body.', encoding='utf-8')

    # Disable skill via CLI
    args_dis = Namespace(type='skill', name='sample-skill')
    res_dis = cmd_config_disable(args_dis)
    assert res_dis == 0
    content = skill_file.read_text(encoding='utf-8')
    assert 'disable-model-invocation: true' in content

    # Enable skill via CLI
    args_en = Namespace(type='skill', name='sample-skill')
    res_en = cmd_config_enable(args_en)
    assert res_en == 0
    content_en = skill_file.read_text(encoding='utf-8')
    assert 'disable-model-invocation: true' not in content_en


def test_cli_config_repo(tmp_path, monkeypatch):
    '''Test repo configuration CLI command for wiki, user, workspace, and custom paths.'''
    import cli
    monkeypatch.setattr(cli, 'root_dir', tmp_path)

    # Configure wiki
    args_repo = Namespace(repo_name='wiki', url='https://github.com/example/wiki.git', path=None, local=False)
    res = cmd_config_repo(args_repo)
    assert res == 0

    from repos import get_repo_url
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
    import cli
    monkeypatch.setattr(cli, 'root_dir', tmp_path)

    # Copy actual diagnose_session.py to tmp_path structure so import works in test
    script_dir = tmp_path / '.agents' / 'skills' / 'self-improvement' / 'scripts'
    script_dir.mkdir(parents=True)
    real_script = Path(__file__).resolve().parent.parent.parent / '.agents' / 'skills' / 'self-improvement' / 'scripts' / 'diagnose_session.py'
    (script_dir / 'diagnose_session.py').write_text(real_script.read_text(encoding='utf-8'), encoding='utf-8')

    # Run status check when no issues exist
    args = Namespace(json=True, clear=False, log_session=None)
    res = cli.cmd_diagnose(args)
    assert res == 0

    captured = capsys.readouterr()
    assert captured.out.strip() == '[]'


def test_config_frontend_obsidian(tmp_path, monkeypatch):
    '''Test configuring frontend to obsidian.'''
    import cli
    monkeypatch.setattr(cli, 'root_dir', tmp_path)

    args = Namespace(frontend_name='obsidian')
    res = cli.cmd_config_frontend(args)
    assert res == 0
    from common import get_config_value
    assert get_config_value(tmp_path, 'frontend') == 'obsidian'




def test_cli_default_opens_frontend(tmp_path, monkeypatch):
    '''Verify that running podarcis without subcommands opens frontend directly.'''
    import cli
    monkeypatch.setattr(cli, 'root_dir', tmp_path)

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
    import cli
    res_script = cli.root_dir / '.agents' / 'mcp' / 'research' / 'server.py'
    import importlib.util
    spec = importlib.util.spec_from_file_location('research_mcp_server', res_script)
    research_server = importlib.util.module_from_spec(spec)
    import sys
    sys.modules['research_mcp_server'] = research_server
    spec.loader.exec_module(research_server)


    async def fake_search(query, limit=5, provider='all'):
        return [{'paperId': 'test:123', 'title': 'Test Paper', 'year': 2024}]

    monkeypatch.setattr(research_server, 'search_literature', fake_search)

    args = Namespace(research_action='search', query='test', limit=2, provider='all', json=True)
    res = cli.cmd_research(args)
    assert res == 0

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert len(data) == 1
    assert data[0]['title'] == 'Test Paper'



def test_cli_diagnose_resolve_id(tmp_path, monkeypatch):
    '''Test resolving specific pain point by ID via podarcis diagnose --resolve.'''
    import cli
    monkeypatch.setattr(cli, 'root_dir', tmp_path)

    # Setup pain point file
    diag_dir = tmp_path / '.podarcis' / 'diagnostics'
    diag_dir.mkdir(parents=True)
    pain_file = diag_dir / 'pain_points.jsonl'
    rec = {'id': 'diag-test-1', 'summary': 'Test pain point', 'resolved': False}
    pain_file.write_text(json.dumps(rec) + '\n', encoding='utf-8')

    script_dir = tmp_path / '.agents' / 'skills' / 'self-improvement' / 'scripts'
    script_dir.mkdir(parents=True)
    real_script = Path(__file__).resolve().parent.parent.parent / '.agents' / 'skills' / 'self-improvement' / 'scripts' / 'diagnose_session.py'
    (script_dir / 'diagnose_session.py').write_text(real_script.read_text(encoding='utf-8'), encoding='utf-8')

    args = Namespace(resolve='diag-test-1', json=False, clear=False, log_session=None)
    res = cli.cmd_diagnose(args)
    assert res == 0

    lines = pain_file.read_text().strip().split('\n')
    data = json.loads(lines[0])
    assert data['resolved'] is True







