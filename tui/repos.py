#!/usr/bin/env python3
'''Workspace repository configuration, Git clone protocol management, and remote sync.'''

import subprocess
import sys
from pathlib import Path

from tui.common import load_json, save_json, load_yaml, save_yaml, load_podarcis_config
from tui.console import HAS_RICH, console

DEFAULT_REPOS = {
    'wiki': 'git@gitlab-internal.bsc.es:xmaripra/hpdsa-wiki-wiki.git',
    'workspace': 'git@gitlab-internal.bsc.es:xmaripra/hpdsa-wiki-workspace.git',
}
DEFAULT_PROTOCOL = 'ssh'


def infer_protocol(url: str) -> str:
    '''Infer Git clone protocol (ssh/https) from URL scheme.'''
    url = url.strip()
    if url.startswith(('https://', 'http://')):
        return 'https'
    return 'ssh'


def get_repos_config_path(root: Path | str) -> Path:
    '''Resolve location of repos configuration file.'''
    return Path(root) / 'podarcis.yaml'


def load_repos_config(root: Path) -> dict:
    '''Load repository configuration from podarcis.yaml or return defaults.'''
    pod_cfg = load_podarcis_config(root)
    repos = pod_cfg.get('repositories', {})
    if not isinstance(repos, dict):
        repos = {}

    # Check legacy .agents/repos.json fallback if podarcis.yaml has no repositories
    if not repos:
        legacy_path = Path(root) / '.agents' / 'repos.json'
        legacy_cfg = load_json(legacy_path)
        if legacy_cfg and 'repositories' in legacy_cfg and isinstance(legacy_cfg['repositories'], dict):
            repos = legacy_cfg['repositories']

    for name, default_url in DEFAULT_REPOS.items():
        if name not in repos or not isinstance(repos[name], str) or not repos[name].strip():
            repos[name] = default_url

    # Infer protocol from first configured repository URL
    sample_url = repos.get('wiki') or repos.get('workspace') or ''
    protocol = infer_protocol(sample_url)

    return {
        'protocol': protocol,
        'repositories': repos
    }


def save_repos_config(root: Path, config: dict) -> None:
    '''Persist repository configuration to podarcis.yaml.'''
    yaml_path = Path(root) / 'podarcis.yaml'
    pod_cfg = load_yaml(yaml_path) if yaml_path.exists() else {}
    if not isinstance(pod_cfg, dict):
        pod_cfg = {}

    repos = config.get('repositories', {})
    pod_cfg['repositories'] = repos
    save_yaml(yaml_path, pod_cfg)



def convert_url_protocol(url: str, protocol: str) -> str:
    '''Convert a Git repository URL between SSH (git@...) and HTTPS (https://...).'''
    url = url.strip()
    if not url:
        return url
    proto = protocol.lower()

    if proto == 'https':
        if url.startswith('git@'):
            body = url[4:]
            if ':' in body:
                host, path = body.split(':', 1)
                return f'https://{host}/{path}'
        elif url.startswith('ssh://git@'):
            body = url[10:]
            if '/' in body:
                host, path = body.split('/', 1)
                return f'https://{host}/{path}'
    elif proto == 'ssh':
        if url.startswith('https://'):
            body = url[8:]
            if '/' in body:
                host, path = body.split('/', 1)
                return f'git@{host}:{path}'
        elif url.startswith('http://'):
            body = url[7:]
            if '/' in body:
                host, path = body.split('/', 1)
                return f'git@{host}:{path}'
    return url


def get_repo_url(root: Path, repo_name: str, protocol: str | None = None) -> str:
    '''Get protocol-converted repository URL for a workspace component.'''
    config = load_repos_config(root)
    target_proto = protocol or config.get('protocol', DEFAULT_PROTOCOL)
    raw_url = config.get('repositories', {}).get(repo_name, DEFAULT_REPOS.get(repo_name, ''))
    return convert_url_protocol(raw_url, target_proto)


def set_repo_protocol(root: Path | str, protocol: str, update_existing_remotes: bool = True) -> None:
    '''Set active clone protocol (ssh or https) and convert stored repository URLs.'''
    root_path = Path(root)
    config = load_repos_config(root_path)
    target_proto = protocol.lower()
    converted_repos = {}
    for name, url in config.get('repositories', {}).items():
        converted_repos[name] = convert_url_protocol(url, target_proto)
    config['repositories'] = converted_repos
    save_repos_config(root_path, config)

    if update_existing_remotes:
        sync_repos(root_path, clone_missing=False, update_remotes=True)


def set_repo_url(root: Path | str, repo_name: str, url: str, update_remote: bool = True) -> None:
    '''Set base repository URL for a workspace component.'''
    root_path = Path(root)
    config = load_repos_config(root_path)
    config['repositories'][repo_name] = url.strip()
    save_repos_config(root_path, config)

    if update_remote:
        repo_dir = root_path / repo_name
        if repo_dir.exists() and (repo_dir / '.git').exists():
            target_url = convert_url_protocol(url.strip(), config.get('protocol', DEFAULT_PROTOCOL))
            try:
                subprocess.run(['git', 'remote', 'set-url', 'origin', target_url], cwd=repo_dir, check=True)
                console.print(f'[green]✓ Updated origin remote for {repo_name} to {target_url}[/green]')
            except subprocess.CalledProcessError:
                subprocess.run(['git', 'remote', 'add', 'origin', target_url], cwd=repo_dir, check=False)


def sync_repos(root: Path | str | None = None, clone_missing: bool = True, update_remotes: bool = True) -> None:
    '''Synchronize decoupled workspace repositories (wiki, workspace).'''
    if root is None:
        from tui.common import get_root_dir
        root_path = get_root_dir()
    else:
        root_path = Path(root)

    config = load_repos_config(root_path)
    protocol = config.get('protocol', DEFAULT_PROTOCOL)

    for repo_name in ['wiki', 'workspace']:
        repo_dir = root_path / repo_name
        raw_url = config.get('repositories', {}).get(repo_name, DEFAULT_REPOS.get(repo_name, ''))
        target_url = convert_url_protocol(raw_url, protocol)

        if not repo_dir.exists() or not (repo_dir / '.git').exists():
            if clone_missing:
                console.print(f'[#29b8db]Cloning {repo_name} using {protocol.upper()} ({target_url})...[/#29b8db]')
                if repo_dir.exists() and not (repo_dir / '.git').exists():
                    # Directory exists but is not a git repo
                    console.print(f'[dim]Initializing git repo in existing directory {repo_name}...[/dim]')
                    subprocess.run(['git', 'init'], cwd=repo_dir, check=False)
                    subprocess.run(['git', 'remote', 'add', 'origin', target_url], cwd=repo_dir, check=False)
                else:
                    try:
                        subprocess.run(['git', 'clone', target_url, str(repo_dir)], check=True)
                        console.print(f'[bold green]✓ Successfully cloned {repo_name}.[/bold green]')
                    except subprocess.CalledProcessError as e:
                        console.print(f'[bold red]⚠️ Failed to clone {repo_name} from {target_url}: {e}[/bold red]')
        else:
            if update_remotes:
                try:
                    res = subprocess.run(['git', 'remote', 'get-url', 'origin'], cwd=repo_dir, capture_output=True, text=True, check=False)
                    current_url = res.stdout.strip()
                    if not current_url:
                        subprocess.run(['git', 'remote', 'add', 'origin', target_url], cwd=repo_dir, check=False)
                        console.print(f'[green]✓ Configured remote origin for {repo_name}: {target_url}[/green]')
                    elif current_url != target_url:
                        subprocess.run(['git', 'remote', 'set-url', 'origin', target_url], cwd=repo_dir, check=True)
                        console.print(f'[green]✓ Updated remote for {repo_name}: {target_url}[/green]')
                except Exception as e:
                    console.print(f'[dim]Could not update remote for {repo_name}: {e}[/dim]')
