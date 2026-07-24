'''Unit tests for repository management and URL conversion.'''

from pathlib import Path
from tui.repos import (
    convert_url_protocol, get_repo_url,
    load_repos_config, save_repos_config, set_repo_url,
)


def test_convert_url_protocol_ssh_to_https():
    assert convert_url_protocol(
        'git@example.com:org/repo.git', 'https') == (
        'https://example.com/org/repo.git')


def test_convert_url_protocol_https_to_ssh():
    assert convert_url_protocol(
        'https://example.com/org/repo.git', 'ssh') == (
        'git@example.com:org/repo.git')


def test_convert_url_protocol_same_protocol():
    assert convert_url_protocol(
        'git@example.com:org/repo.git', 'ssh') == (
        'git@example.com:org/repo.git')
    assert convert_url_protocol(
        'https://example.com/org/repo.git', 'https') == (
        'https://example.com/org/repo.git')


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
