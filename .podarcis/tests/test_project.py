'''Tests for multi-project resolution and management in Podarcis.'''

import os
from pathlib import Path
import pytest

from podarcis.project import (
    Project,
    get_xdg_config_dir,
    get_xdg_data_dir,
    get_projects_dir,
    load_global_config,
    save_global_config,
    new_project,
    list_projects,
    register_project,
    unregister_project,
    resolve_project,
    set_active_project,
    is_project_root,
)
from podarcis.root import find_wiki_root, is_wiki_root


@pytest.fixture
def mock_xdg(tmp_path, monkeypatch):
    '''Set up isolated XDG directories for testing.'''
    cfg_dir = tmp_path / 'config' / 'podarcis'
    data_dir = tmp_path / 'data' / 'podarcis'
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'data'))
    cfg_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    return {'config': cfg_dir, 'data': data_dir}


def test_xdg_directories(mock_xdg):
    assert get_xdg_config_dir() == mock_xdg['config']
    assert get_xdg_data_dir() == mock_xdg['data']
    assert get_projects_dir() == mock_xdg['data'] / 'projects'


def test_project_creation_and_listing(mock_xdg):
    proj = new_project('neuroscience', description='Neuro research')
    assert proj.exists()
    assert (proj.wiki / '_index.md').is_file()
    assert (proj.workspace / '_index.md').is_file()
    assert (proj.sources / '_index.md').is_file()
    assert (proj.root / 'podarcis.yaml').is_file()

    projects = list_projects()
    assert 'neuroscience' in projects
    assert projects['neuroscience']['active'] is True
    assert projects['neuroscience']['description'] == 'Neuro research'


def test_project_switching(mock_xdg):
    new_project('proj_a')
    new_project('proj_b')

    projects = list_projects()
    assert projects['proj_b']['active'] is True

    set_active_project('proj_a')
    assert list_projects()['proj_a']['active'] is True


def test_resolve_project_priority(mock_xdg, tmp_path, monkeypatch):
    # Setup two projects
    p1 = new_project('p1')
    p2 = new_project('p2')

    # 1. Fallback to active project (when CWD is not in a project)
    set_active_project('p1')
    resolved = resolve_project(cwd=tmp_path)
    assert resolved.name == 'p1'
    assert resolved.root == p1.root

    # 2. CWD walk has priority over active project
    nested_dir = p2.wiki / 'subfolder'
    nested_dir.mkdir(parents=True)
    resolved_cwd = resolve_project(cwd=nested_dir)
    assert resolved_cwd.name == 'p2'
    assert resolved_cwd.root == p2.root

    # 3. Environment variable has priority over CWD
    resolved_env = resolve_project(cwd=nested_dir, environ={'PODARCIS_PROJECT': 'p1'})
    assert resolved_env.name == 'p1'
    assert resolved_env.root == p1.root

    # 4. Explicit parameter has highest priority
    resolved_explicit = resolve_project(explicit='p2', environ={'PODARCIS_PROJECT': 'p1'})
    assert resolved_explicit.name == 'p2'
    assert resolved_explicit.root == p2.root


def test_find_wiki_root_with_project(mock_xdg):
    proj = new_project('biotech')
    assert is_wiki_root(proj.root)

    # Resolve by explicit name
    found = find_wiki_root(explicit='biotech')
    assert found == proj.root

    # Resolve from inside project
    found_inside = find_wiki_root(cwd=proj.workspace)
    assert found_inside == proj.root


def test_unregister_project(mock_xdg):
    proj = new_project('temporary')
    assert 'temporary' in list_projects()

    # Unregistering an internal project in projects_dir cleans up directory so it's not re-discovered
    unregister_project('temporary', purge=False)
    assert 'temporary' not in list_projects()
    assert not proj.exists()


def test_unregister_external_project_keeps_files(mock_xdg, tmp_path):
    ext_dir = tmp_path / 'ext_project'
    ext_dir.mkdir()
    (ext_dir / 'wiki').mkdir()
    (ext_dir / 'workspace').mkdir()

    register_project('ext', ext_dir)
    assert 'ext' in list_projects()

    unregister_project('ext', purge=False)
    assert 'ext' not in list_projects()
    # External files are preserved
    assert ext_dir.is_dir()


def test_ensure_and_close_herdr_space(monkeypatch, tmp_path):
    commands_run = []

    def mock_run(cmd, *args, **kwargs):
        commands_run.append(list(cmd))
        class MockResult:
            returncode = 0
            stdout = '{"result": {"workspaces": [{"workspace_id": "w1", "label": "other", "focused": false}]}}'
        return MockResult()

    monkeypatch.setattr('shutil.which', lambda x: '/usr/bin/herdr')
    monkeypatch.setattr('pathlib.Path.exists', lambda self: True)
    monkeypatch.setattr('subprocess.run', mock_run)

    from podarcis.project import ensure_herdr_space, close_herdr_space
    ensure_herdr_space('myproj', tmp_path / 'myproj', focus=True)
    assert any('workspace' in c and 'create' in c and 'myproj' in c for c in commands_run)

    close_commands = []
    def mock_run_close(cmd, *args, **kwargs):
        close_commands.append(list(cmd))
        class MockResult:
            returncode = 0
            stdout = '{"result": {"workspaces": [{"workspace_id": "w2", "label": "myproj", "focused": false}]}}'
        return MockResult()

    monkeypatch.setattr('subprocess.run', mock_run_close)
    close_herdr_space('myproj')
    assert any('workspace' in c and 'close' in c and 'w2' in c for c in close_commands)

