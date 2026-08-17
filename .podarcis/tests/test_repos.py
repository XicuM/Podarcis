'''Unit tests for repository management and URL conversion.'''

from pathlib import Path
from repos import (
    DEFAULT_REPO_NAMES,
    get_repo_names,
    get_repo_url,
    load_repos_config,
    save_repos_config,
    set_repo_url,
)


def test_repos_config_load_and_save(tmp_path: Path):
    config = load_repos_config(tmp_path)
    assert config == {'repositories': {}}

    config['repositories']['wiki'] = 'https://example.com/wiki.git'
    config['repositories']['workspace'] = 'git@example.com:repo.git'
    save_repos_config(tmp_path, config)

    reloaded = load_repos_config(tmp_path)
    assert reloaded['repositories']['wiki'] == 'https://example.com/wiki.git'
    assert reloaded['repositories']['workspace'] == 'git@example.com:repo.git'


def test_set_repo_url_and_get(tmp_path: Path):
    url = 'git@example.com:user/wiki.git'
    set_repo_url(tmp_path, 'wiki', url, update_remote=False)
    assert load_repos_config(tmp_path)['repositories']['wiki'] == url
    assert get_repo_url(tmp_path, 'wiki') == url


def test_get_repo_status_and_sync(tmp_path: Path):
    from repos import ensure_local_git_repo, get_repo_status, sync_repos_full, push_repos

    set_repo_url(tmp_path, 'sources', 'gdrive', update_remote=False)
    set_repo_url(tmp_path, 'wiki', 'local', update_remote=False)
    ensure_local_git_repo(tmp_path, 'wiki')

    statuses = get_repo_status(tmp_path)
    status_dict = {s['repo']: s for s in statuses}

    assert status_dict['sources']['type'] == 'gdrive'
    assert status_dict['sources']['status'] == 'gdrive_managed'
    assert status_dict['wiki']['type'] == 'git'
    assert status_dict['wiki']['is_git'] is True

    # Test sync repos full
    sync_res = sync_repos_full(tmp_path)
    assert 'wiki' in sync_res
    assert 'sources' in sync_res

    # Test push repos
    push_res = push_repos(tmp_path, auto_commit=True)
    assert 'wiki' in push_res




