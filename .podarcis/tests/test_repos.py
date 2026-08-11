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


def test_get_repo_names(tmp_path: Path):
    assert get_repo_names(tmp_path) == DEFAULT_REPO_NAMES
    set_repo_url(tmp_path, 'custom_repo', 'https://example.com/custom.git', update_remote=False)
    names = get_repo_names(tmp_path)
    assert 'custom_repo' in names
    assert 'wiki' in names
    assert 'workspace' in names
    assert 'sources' in names



