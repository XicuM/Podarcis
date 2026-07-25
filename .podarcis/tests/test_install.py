'''Unit tests for install.py interactive setup routines and helpers.'''

from pathlib import Path
import pytest
from components import run_mcp_setup
from install import (
    _create_podarcis_yaml,
    _configure_global_command,
    _hr, _say
)


def test_create_podarcis_yaml(tmp_path, monkeypatch):
    '''Verify _create_podarcis_yaml creates default config.yaml.'''
    import install
    monkeypatch.setattr(install, 'root', tmp_path)

    pod_dir = tmp_path / '.podarcis'
    pod_dir.mkdir(parents=True)

    _create_podarcis_yaml()

    yaml_file = pod_dir / 'config.yaml'
    assert yaml_file.exists()


def test_run_mcp_setup_wiki(tmp_path):
    '''Verify run_mcp_setup executes setup_wiki if available.'''
    wiki_dir = tmp_path / '.agents' / 'mcp' / 'wiki'
    wiki_dir.mkdir(parents=True)
    setup_file = wiki_dir / 'setup.py'
    setup_file.write_text('def setup_wiki(root):\n    pass\n', encoding='utf-8')

    res = run_mcp_setup(tmp_path, 'wiki-mcp')
    assert res is True


def test_setup_wiki_execution(tmp_path, monkeypatch):
    '''Verify setup_wiki in .agents/mcp/wiki/setup.py executes without NameError.'''
    import importlib.util
    spec = importlib.util.spec_from_file_location('wiki_setup', Path(__file__).resolve().parent.parent.parent / '.agents' / 'mcp' / 'wiki' / 'setup.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Mock questionary select
    class DummyPrompt:
        def ask(self): return 'no'
    monkeypatch.setattr('questionary.select', lambda *args, **kwargs: DummyPrompt())

    mod.setup_wiki(tmp_path)


def test_configure_global_command_no(tmp_path, monkeypatch):
    '''Verify _configure_global_command when user selects no.'''
    import install
    monkeypatch.setattr(install, 'root', tmp_path)
    monkeypatch.setattr(install, '_select', lambda prompt, choices, default=None, qmark='?': 'no')

    _configure_global_command()


def test_helpers_run(capsys):
    '''Verify _say and _hr run without error.'''
    _say('hello')
    _hr()
    captured = capsys.readouterr()
    assert 'hello' in captured.out
