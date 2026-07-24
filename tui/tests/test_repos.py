'''Unit tests for repository management, Git clone protocol conversion, and config persistence.'''

import json
from pathlib import Path
from tui.repos import (
    convert_url_protocol, load_repos_config, save_repos_config,
    set_repo_protocol, set_repo_url, get_repo_url
)


def test_convert_url_protocol_ssh_to_https():
    ssh_url = 'git@gitlab-internal.bsc.es:xmaripra/hpdsa-wiki-wiki.git'
    converted = convert_url_protocol(ssh_url, 'https')
    assert converted == 'https://gitlab-internal.bsc.es/xmaripra/hpdsa-wiki-wiki.git'


def test_convert_url_protocol_https_to_ssh():
    https_url = 'https://gitlab-internal.bsc.es/xmaripra/hpdsa-wiki-wiki.git'
    converted = convert_url_protocol(https_url, 'ssh')
    assert converted == 'git@gitlab-internal.bsc.es:xmaripra/hpdsa-wiki-wiki.git'


def test_convert_url_protocol_same_protocol():
    ssh_url = 'git@github.com:XicuM/agentic-wiki-builder.git'
    assert convert_url_protocol(ssh_url, 'ssh') == ssh_url

    https_url = 'https://github.com/XicuM/agentic-wiki-builder.git'
    assert convert_url_protocol(https_url, 'https') == https_url


def test_repos_config_load_and_save(tmp_path: Path):
    config = load_repos_config(tmp_path)
    assert config['protocol'] == 'ssh'
    assert 'wiki' in config['repositories']

    config['protocol'] = 'https'
    config['repositories']['wiki'] = 'https://example.com/wiki.git'
    save_repos_config(tmp_path, config)

    reloaded = load_repos_config(tmp_path)
    assert reloaded['protocol'] == 'https'
    assert reloaded['repositories']['wiki'] == 'https://example.com/wiki.git'


def test_set_repo_protocol_and_url(tmp_path: Path):
    set_repo_protocol(tmp_path, 'https', update_existing_remotes=False)
    assert load_repos_config(tmp_path)['protocol'] == 'https'
    assert get_repo_url(tmp_path, 'wiki').startswith('https://')

    set_repo_url(tmp_path, 'wiki', 'git@example.com:user/wiki.git', update_remote=False)
    assert load_repos_config(tmp_path)['repositories']['wiki'] == 'git@example.com:user/wiki.git'

    # get_repo_url without explicit protocol returns URL as configured
    effective_url = get_repo_url(tmp_path, 'wiki')
    assert effective_url == 'git@example.com:user/wiki.git'

    # get_repo_url with explicit protocol converts on demand
    converted_url = get_repo_url(tmp_path, 'wiki', protocol='https')
    assert converted_url == 'https://example.com/user/wiki.git'
