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
    '''Test repo configuration CLI command.'''
    import cli
    monkeypatch.setattr(cli, 'root_dir', tmp_path)

    args_repo = Namespace(repo_name='wiki', url='https://github.com/example/wiki.git', local=False)
    res = cmd_config_repo(args_repo)
    assert res == 0

    from repos import get_repo_url
    assert get_repo_url(tmp_path, 'wiki') == 'https://github.com/example/wiki.git'

    # Set to local
    args_local = Namespace(repo_name='wiki', url=None, local=True)
    res_loc = cmd_config_repo(args_local)
    assert res_loc == 0
    assert get_repo_url(tmp_path, 'wiki') == ''


def test_generate_opencode_json(tmp_path, monkeypatch):
    '''Test dynamic generation of opencode.json.'''
    import cli
    monkeypatch.setattr(cli, 'root_dir', tmp_path)

    mcp_dir = tmp_path / '.agents' / 'mcp' / 'sample'
    mcp_dir.mkdir(parents=True)
    (mcp_dir / 'server.py').write_text('# sample server', encoding='utf-8')

    from components import generate_opencode_json
    opencode_path = generate_opencode_json(tmp_path)
    assert opencode_path.exists()

    data = json.loads(opencode_path.read_text(encoding='utf-8'))
    assert 'mcp' in data
    assert 'sample-mcp' in data['mcp']
    assert data['mcp']['sample-mcp']['enabled'] is True


def test_generate_opencode_json_preserves_custom_config(tmp_path, monkeypatch):
    '''Test that generate_opencode_json non-destructively preserves user custom settings.'''
    import cli
    monkeypatch.setattr(cli, 'root_dir', tmp_path)

    # Pre-populate opencode.json with custom user settings and custom MCP server
    opencode_path = tmp_path / 'opencode.json'
    pre_data = {
        '$schema': 'https://opencode.ai/config.json',
        'model': 'custom-provider/custom-model',
        'mcp': {
            'user-custom-mcp': {
                'type': 'local',
                'command': ['node', 'custom.js'],
                'enabled': True
            }
        }
    }
    opencode_path.write_text(json.dumps(pre_data), encoding='utf-8')

    mcp_dir = tmp_path / '.agents' / 'mcp' / 'sample'
    mcp_dir.mkdir(parents=True)
    (mcp_dir / 'server.py').write_text('# sample server', encoding='utf-8')

    from components import generate_opencode_json
    generate_opencode_json(tmp_path)

    data = json.loads(opencode_path.read_text(encoding='utf-8'))
    # Verify custom top-level key preserved
    assert data.get('model') == 'custom-provider/custom-model'
    # Verify custom MCP server preserved
    assert 'user-custom-mcp' in data['mcp']
    # Verify project-managed MCP server added
    assert 'sample-mcp' in data['mcp']


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



