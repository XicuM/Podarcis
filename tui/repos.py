#!/usr/bin/env python3
'''Workspace repository configuration and remote sync.'''

import subprocess
from pathlib import Path

from tui.common import load_json, load_yaml, save_yaml, load_podarcis_config
from tui.console import console

REPO_NAMES = ['wiki', 'workspace']


def get_repos_config_path(root: Path | str) -> Path:
    return Path(root) / 'podarcis.yaml'


def load_repos_config(root: Path) -> dict:
    '''Load repository URLs from podarcis.yaml.'''
    pod_cfg = load_podarcis_config(root)
    repos = pod_cfg.get('repositories', {})
    if not isinstance(repos, dict):
        repos = {}

    if not repos:
        legacy_path = Path(root) / '.agents' / 'repos.json'
        legacy_cfg = load_json(legacy_path)
        if legacy_cfg and 'repositories' in legacy_cfg and isinstance(legacy_cfg['repositories'], dict):
            repos = legacy_cfg['repositories']

    return {'repositories': repos}


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
    '''Convert a Git URL between SSH (git@...) and HTTPS (https://...).'''
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
    elif proto == 'ssh':
        if url.startswith('https://'):
            body = url[8:]
            if '/' in body:
                host, path = body.split('/', 1)
                return f'git@{host}:{path}'
    return url


def get_repo_url(root: Path, repo_name: str) -> str:
    '''Get configured repository URL.'''
    config = load_repos_config(root)
    return config.get('repositories', {}).get(repo_name, '')


def set_repo_url(root: Path | str, repo_name: str, url: str, update_remote: bool = True) -> None:
    '''Set repository URL and optionally update the local remote.'''
    root_path = Path(root)
    config = load_repos_config(root_path)
    config['repositories'][repo_name] = url.strip()
    save_repos_config(root_path, config)

    if update_remote:
        repo_dir = root_path / repo_name
        if repo_dir.exists() and (repo_dir / '.git').exists():
            try:
                subprocess.run(
                    ['git', 'remote', 'set-url', 'origin', url.strip()],
                    cwd=repo_dir, check=True)
                console.print(f'[green]✓ Updated origin for {repo_name}.[/green]')
            except subprocess.CalledProcessError:
                subprocess.run(
                    ['git', 'remote', 'add', 'origin', url.strip()],
                    cwd=repo_dir, check=False)


def setup_repo(root: Path | str, repo_name: str, url: str) -> None:
    '''Clone a remote repo into <root>/<repo_name>, or init locally and push if the remote is empty/new.'''
    root_path = Path(root)
    repo_dir = root_path / repo_name
    url = url.strip()

    if repo_dir.exists() and (repo_dir / '.git').exists():
        # Already a git repo — just ensure the remote is correct.
        res = subprocess.run(
            ['git', 'remote', 'get-url', 'origin'],
            cwd=repo_dir, capture_output=True, text=True, check=False,
        )
        current_url = res.stdout.strip()
        if not current_url:
            subprocess.run(['git', 'remote', 'add', 'origin', url], cwd=repo_dir, check=False)
        elif current_url != url:
            subprocess.run(['git', 'remote', 'set-url', 'origin', url], cwd=repo_dir, check=False)
        console.print(f'[green]✓ {repo_name} already initialised — remote updated.[/green]')
        return

    # Try to clone first (works when the remote already has content).
    console.print(f'[#29b8db]Cloning {repo_name} from {url}...[/#29b8db]')
    result = subprocess.run(
        ['git', 'clone', url, str(repo_dir)],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        console.print(f'[bold green]✓ Cloned {repo_name} successfully.[/bold green]')
        return

    # Clone failed — remote likely empty or brand-new.  Init locally and push.
    console.print(f'[yellow]Clone failed (remote may be empty). Initialising local repo and pushing...[/yellow]')
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(['git', 'init'], cwd=repo_dir, check=False)
    subprocess.run(['git', 'remote', 'add', 'origin', url], cwd=repo_dir, check=False)

    # Create an initial commit so there is something to push.
    readme = repo_dir / 'README.md'
    if not readme.exists():
        readme.write_text(f'# {repo_name}\n', encoding='utf-8')
        subprocess.run(['git', 'add', 'README.md'], cwd=repo_dir, check=False)
        subprocess.run(['git', 'commit', '-m', f'chore: initialise {repo_name} repository'], cwd=repo_dir, check=False)

    push_result = subprocess.run(
        ['git', 'push', '-u', 'origin', 'HEAD'],
        cwd=repo_dir, capture_output=True, text=True,
    )
    if push_result.returncode == 0:
        console.print(f'[bold green]✓ Pushed initial commit for {repo_name}.[/bold green]')
    else:
        console.print(f'[bold red]⚠️ Push failed for {repo_name}: {push_result.stderr.strip()}[/bold red]')
        console.print('[dim]You can retry later with: make sync[/dim]')


def sync_repos(root: Path | str | None = None, clone_missing: bool = True, update_remotes: bool = True) -> None:
    '''Synchronize configured workspace repositories.'''
    if root is None:
        from tui.common import get_root_dir
        root_path = get_root_dir()
    else:
        root_path = Path(root)

    config = load_repos_config(root_path)

    for repo_name in REPO_NAMES:
        repo_dir = root_path / repo_name
        url = config.get('repositories', {}).get(repo_name, '')
        if not url:
            continue

        if not repo_dir.exists() or not (repo_dir / '.git').exists():
            if clone_missing:
                console.print(f'[#29b8db]Cloning {repo_name} from {url}...[/#29b8db]')
                if repo_dir.exists():
                    console.print(f'[dim]Init git in existing {repo_name} dir...[/dim]')
                    subprocess.run(['git', 'init'], cwd=repo_dir, check=False)
                    subprocess.run(['git', 'remote', 'add', 'origin', url], cwd=repo_dir, check=False)
                else:
                    try:
                        subprocess.run(['git', 'clone', url, str(repo_dir)], check=True)
                        console.print(f'[bold green]✓ Cloned {repo_name}.[/bold green]')
                    except subprocess.CalledProcessError as e:
                        console.print(f'[bold red]⚠️ Failed to clone {repo_name}: {e}[/bold red]')
        elif update_remotes:
            try:
                res = subprocess.run(
                    ['git', 'remote', 'get-url', 'origin'],
                    cwd=repo_dir, capture_output=True, text=True, check=False)
                current = res.stdout.strip()
                if not current:
                    subprocess.run(
                        ['git', 'remote', 'add', 'origin', url],
                        cwd=repo_dir, check=False)
                    console.print(f'[green]✓ Set origin for {repo_name}.[/green]')
                elif current != url:
                    subprocess.run(
                        ['git', 'remote', 'set-url', 'origin', url],
                        cwd=repo_dir, check=True)
                    console.print(f'[green]✓ Updated origin for {repo_name}.[/green]')
            except Exception:
                pass
