'''Unit tests for the Python surface of the Podarcis CLI.

The core subcommands (status, config, repo, lint, diagnose, test, wiki,
frontend) are implemented in the Rust `podarcis` binary and tested there; this
suite covers what still runs on the Python engine: external-skill detection,
the research search path, and the single-config-file invariant.
'''

import json
import pytest
from argparse import Namespace
from pathlib import Path


def _deploy(root, name, *, pyproject=None, package_json=None):
    """Write a skill bundle into the .agents/skills deploy root, as APM would."""
    d = root / '.agents' / 'skills' / name
    d.mkdir(parents=True)
    (d / 'SKILL.md').write_text(f'---\nname: {name}\n---\n', encoding='utf-8')
    if pyproject:
        (d / 'pyproject.toml').write_text(pyproject, encoding='utf-8')
    if package_json:
        (d / 'package.json').write_text(json.dumps(package_json), encoding='utf-8')
    return d


def test_external_skills_excludes_what_this_repo_authors(tmp_path):
    """.apm/skills is authored; the deploy roots hold authored and vendored alike."""
    from podarcis.components import external_skills

    (tmp_path / '.apm' / 'skills' / 'mine').mkdir(parents=True)
    _deploy(tmp_path, 'mine')          # same skill, deployed
    _deploy(tmp_path, 'theirs')        # a dependency

    assert set(external_skills(tmp_path)) == {'theirs'}


def test_external_skills_reads_python_console_scripts(tmp_path):
    from podarcis.components import external_skills

    _deploy(tmp_path, 'tool', pyproject=(
        '[project]\nname = "tool"\n\n[project.scripts]\n'
        'thing = "tool.cli:main"\nother = "tool.cli:other"\n'))
    assert set(external_skills(tmp_path)['tool']['executables']) == {'thing', 'other'}


@pytest.mark.parametrize('bin_field, expected', [
    ({'cli-name': 'dist/index.js'}, {'cli-name'}),
    ('dist/index.js', {'node-tool'}),
])
def test_external_skills_reads_node_bin(tmp_path, bin_field, expected):
    """package.json `bin` is a map of names, or a bare string.

    The string form names the binary after the package's `name`, not after the
    path it points at — reading the path as a command name would report a
    binary that could never exist.
    """
    from podarcis.components import external_skills

    _deploy(tmp_path, 'node-tool', package_json={'name': 'node-tool', 'bin': bin_field})
    assert set(external_skills(tmp_path)['node-tool']['executables']) == expected


def test_external_skills_reports_a_missing_executable(tmp_path):
    """The whole point: a deployed skill whose CLI was never installed.

    APM deploys files but installs no runtime, and a failing lifecycle script
    does not fail `apm install` — so nothing upstream of status notices.
    """
    from podarcis.components import external_skills

    _deploy(tmp_path, 'tool', pyproject=(
        '[project]\nname = "tool"\n\n[project.scripts]\n'
        'definitely-not-on-this-system = "tool.cli:main"\n'))
    assert external_skills(tmp_path)['tool']['executables'] == {
        'definitely-not-on-this-system': ''}


def test_external_skills_prefers_the_project_venv_over_path(tmp_path):
    """A CLI installed into .venv must win; PATH may hold a different version."""
    from podarcis.components import external_skills

    venv_bin = tmp_path / '.venv' / 'bin'
    venv_bin.mkdir(parents=True)
    (venv_bin / 'python').write_text('', encoding='utf-8')  # certainly also on PATH
    _deploy(tmp_path, 'tool', pyproject=(
        '[project]\nname = "tool"\n\n[project.scripts]\npython = "tool.cli:main"\n'))
    assert external_skills(tmp_path)['tool']['executables']['python'] == str(venv_bin / 'python')


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