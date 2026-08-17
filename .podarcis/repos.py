#!/usr/bin/env python3
'''Workspace repository configuration and remote sync.'''

import subprocess
from pathlib import Path

from common import load_json, load_yaml, save_yaml
from console import console

DEFAULT_REPO_NAMES = ['sources', 'wiki', 'workspace']



def load_repos_config(root: Path) -> dict:
    '''Load repository URLs from .podarcis/config.yaml.'''
    pod_cfg = load_yaml(root/'.podarcis'/'config.yaml')
    repos = pod_cfg.get('repositories', {})
    if not isinstance(repos, dict): repos = {}

    if not repos:
        legacy_path = Path(root) / '.agents' / 'repos.json'
        legacy_cfg = load_json(legacy_path)
        if legacy_cfg and 'repositories' in legacy_cfg and isinstance(legacy_cfg['repositories'], dict):
            repos = legacy_cfg['repositories']

    return {'repositories': repos}


def get_repo_names(root: Path | str | None = None) -> list[str]:
    '''Get configured repository names from .podarcis/config.yaml or default.'''
    if root is None:
        root_path = Path(__file__).resolve().parent.parent
    else:
        root_path = Path(root)
    config = load_repos_config(root_path)
    repos = config.get('repositories', {})
    names = list(DEFAULT_REPO_NAMES)
    if isinstance(repos, dict):
        for k in repos.keys():
            if k not in names:
                names.append(k)
    return names



def save_repos_config(root: Path, config: dict) -> None:
    '''Persist repository configuration to .podarcis/config.yaml.'''
    yaml_path = Path(root) / '.podarcis' / 'config.yaml'
    yaml_path.parent.mkdir(parents=True, exist_ok=True)
    pod_cfg = load_yaml(yaml_path) if yaml_path.exists() else {}
    if not isinstance(pod_cfg, dict): pod_cfg = {}

    repos = config.get('repositories', {})
    pod_cfg['repositories'] = repos
    save_yaml(yaml_path, pod_cfg)


def ensure_local_git_repo(root: Path | str, repo_name: str) -> None:
    '''Ensure local repository directory exists and has a git repository initialized.'''
    repo_dir = Path(root) / repo_name
    repo_dir.mkdir(parents=True, exist_ok=True)
    if not (repo_dir / '.git').exists():
        subprocess.run(['git', 'init'], cwd=repo_dir, capture_output=True, text=True, check=False)
        readme = repo_dir / 'index.md'
        if not readme.exists():
            readme.write_text(f'# {repo_name.title()}\n\nOKF v0.2 Knowledge Base\n', encoding='utf-8')
        subprocess.run(['git', 'add', '-A'], cwd=repo_dir, check=False)
        subprocess.run(['git', 'commit', '-m', f'chore: initialize {repo_name} repository'], cwd=repo_dir, check=False)
        console.print(f'[bold green]✓ Initialized local git repository for {repo_name}.[/bold green]')


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


def prompt_configure_repo(root: Path | str, repo_name: str, style=None) -> None:
    '''Interactive prompt to select and configure repository mode (Local, Remote, or GDrive for sources).'''
    import questionary
    from common import set_config_value
    root_path = Path(root)
    current_url = get_repo_url(root_path, repo_name)
    is_git = bool(current_url and current_url not in ('local', 'gdrive'))

    act_choices = [
        'Keep current configuration (Skip)',
        'Local Git repository (no remote)',
        'Remote Git repository (with origin URL)',
        'Cancel',
    ]

    if repo_name == 'sources':
        act_choices.insert(1, 'GDrive (no local repo)')

    act = questionary.select(
        'Select repository type:',
        choices=act_choices,
        default='Keep current configuration (Skip)',
        qmark=f'{repo_name} /',
        style=style,
    ).ask()

    if act == 'Keep current configuration (Skip)' or act == 'Cancel' or act is None:
        return

    if act == 'GDrive (no local repo)':
        set_repo_url(root_path, repo_name, 'gdrive')
        set_config_value(root_path, 'gdrive', 'sources_backend')
        console.print(f'[yellow]Set {repo_name} to GDrive (sources managed remotely).[/yellow]\n')

    elif act == 'Local Git repository (no remote)':
        set_repo_url(root_path, repo_name, 'local')
        ensure_local_git_repo(root_path, repo_name)
        if repo_name == 'sources':
            set_config_value(root_path, 'local', 'sources_backend')
        console.print(f'[yellow]Set {repo_name} to local Git repository (no remote).[/yellow]\n')

    elif act == 'Remote Git repository (with origin URL)':
        new_url = questionary.text(
            f'Remote Git URL for "{repo_name}":',
            default=current_url if is_git else '',
            qmark=f'{repo_name} /',
            style=style,
        ).ask()
        if new_url is not None:
            new_url_str = new_url.strip()
            set_repo_url(root_path, repo_name, new_url_str or 'local')
            ensure_local_git_repo(root_path, repo_name)
            if repo_name == 'sources':
                set_config_value(root_path, 'local', 'sources_backend')
            if new_url_str and new_url_str != 'local':
                console.print(f'[bold green]✓ Set remote for {repo_name} to {new_url_str}[/bold green]\n')
            else:
                console.print(f'[yellow]Set {repo_name} to local-only.[/yellow]\n')


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
        console.print('[dim]You can retry later with: podarcis config sync[/dim]')


def sync_repos(root: Path | str | None = None, clone_missing: bool = True, update_remotes: bool = True) -> None:
    '''Synchronize configured workspace repositories (clones missing, updates origin, pulls changes).'''
    if root is None:
        root_path = Path(__file__).resolve().parent.parent
    else:
        root_path = Path(root)

    config = load_repos_config(root_path)

    for repo_name in get_repo_names(root_path):
        repo_dir = root_path / repo_name
        url = config.get('repositories', {}).get(repo_name, '')

        if url == 'gdrive':
            console.print(f'[bold green]✓ {repo_name}: managed via Google Drive (no local repo).[/bold green]')
            continue

        # Ensure local repo exists if skipped or local
        if not repo_dir.exists() or not (repo_dir / '.git').exists():
            if url and clone_missing:
                console.print(f'[#29b8db]Cloning {repo_name} from {url}...[/#29b8db]')
                res = subprocess.run(['git', 'clone', url, str(repo_dir)], capture_output=True, text=True)
                if res.returncode == 0:
                    console.print(f'[bold green]✓ Cloned {repo_name}.[/bold green]')
                    continue
            ensure_local_git_repo(root_path, repo_name)
            console.print(f'[bold green]✓ Initialised local {repo_name} repository.[/bold green]')
            continue

        if not url:
            console.print(f'[bold green]✓ {repo_name}: local repository ready.[/bold green]')
            continue

        # Update remote and pull latest changes if remote is configured
        if update_remotes:
            res = subprocess.run(
                ['git', 'remote', 'get-url', 'origin'],
                cwd=repo_dir, capture_output=True, text=True, check=False
            )
            current = res.stdout.strip()
            if not current:
                subprocess.run(['git', 'remote', 'add', 'origin', url], cwd=repo_dir, check=False)
            elif current != url:
                subprocess.run(['git', 'remote', 'set-url', 'origin', url], cwd=repo_dir, check=False)
                console.print(f'[green]✓ Updated origin for {repo_name}.[/green]')

            # Perform git pull
            pull_res = subprocess.run(
                ['git', 'pull', '--rebase', 'origin', 'HEAD'],
                cwd=repo_dir, capture_output=True, text=True, check=False
            )
            if pull_res.returncode == 0:
                console.print(f'[bold green]✓ {repo_name}: synced with remote origin.[/bold green]')
            else:
                console.print(f'[bold green]✓ {repo_name}: remote origin configured ({url}).[/bold green]')


def get_repo_status(root: Path | str | None = None) -> list[dict]:
    '''Get detailed sync/git status for all configured workspace repositories.'''
    if root is None:
        root_path = Path(__file__).resolve().parent.parent
    else:
        root_path = Path(root)

    config = load_repos_config(root_path)
    repo_names = get_repo_names(root_path)
    status_list = []

    for name in repo_names:
        repo_dir = root_path / name
        url = config.get('repositories', {}).get(name, '')

        info = {
            'repo': name,
            'path': str(repo_dir),
            'url': url or 'local',
            'type': 'gdrive' if url == 'gdrive' else 'git',
            'exists': repo_dir.exists(),
            'is_git': (repo_dir / '.git').exists() if repo_dir.exists() else False,
            'branch': None,
            'clean': True,
            'ahead': 0,
            'behind': 0,
            'changes': 0,
            'status': 'ready',
        }

        if info['type'] == 'gdrive':
            info['status'] = 'gdrive_managed'
            status_list.append(info)
            continue

        if not info['exists'] or not info['is_git']:
            info['status'] = 'missing'
            status_list.append(info)
            continue

        # Get branch
        res_branch = subprocess.run(
            ['git', 'branch', '--show-current'],
            cwd=repo_dir, capture_output=True, text=True, check=False,
        )
        info['branch'] = res_branch.stdout.strip() or 'HEAD'

        # Get dirty state
        res_status = subprocess.run(
            ['git', 'status', '--porcelain'],
            cwd=repo_dir, capture_output=True, text=True, check=False,
        )
        status_lines = [l for l in res_status.stdout.splitlines() if l.strip()]
        info['changes'] = len(status_lines)
        info['clean'] = len(status_lines) == 0

        # Get ahead/behind count relative to upstream if tracking
        res_rev = subprocess.run(
            ['git', 'rev-list', '--left-right', '--count', 'HEAD...@{u}'],
            cwd=repo_dir, capture_output=True, text=True, check=False,
        )
        if res_rev.returncode == 0 and res_rev.stdout.strip():
            parts = res_rev.stdout.strip().split()
            if len(parts) == 2:
                info['ahead'] = int(parts[0])
                info['behind'] = int(parts[1])

        if not info['clean']:
            info['status'] = 'modified'
        elif info['ahead'] > 0:
            info['status'] = 'ahead'
        elif info['behind'] > 0:
            info['status'] = 'behind'
        else:
            info['status'] = 'synced'

        status_list.append(info)

    return status_list


def sync_repos_full(root: Path | str | None = None) -> dict:
    '''Synchronize all workspace repos (clones missing, pulls git remotes, ingests gdrive deltas).'''
    if root is None:
        root_path = Path(__file__).resolve().parent.parent
    else:
        root_path = Path(root)

    sync_repos(root_path, clone_missing=True, update_remotes=True)
    results = {}

    config = load_repos_config(root_path)
    for name in get_repo_names(root_path):
        repo_dir = root_path / name
        url = config.get('repositories', {}).get(name, '')

        if url == 'gdrive':
            try:
                from jobs import run_job
                job_res = run_job(root_path, 'gdrive_sync', dry_run=False)
                results[name] = {'status': 'ok', 'message': f'GDrive sync completed ({job_res.get("status", "ok")})'}
            except Exception as e:
                results[name] = {'status': 'error', 'message': f'GDrive sync error: {e}'}
            continue

        if not repo_dir.exists() or not (repo_dir / '.git').exists():
            results[name] = {'status': 'warning', 'message': 'Repo directory or .git missing'}
            continue

        if url and url != 'local':
            pull_res = subprocess.run(
                ['git', 'pull', '--rebase', 'origin', 'HEAD'],
                cwd=repo_dir, capture_output=True, text=True, check=False,
            )
            if pull_res.returncode == 0:
                results[name] = {'status': 'ok', 'message': 'Synced with remote origin'}
            else:
                results[name] = {'status': 'error', 'message': pull_res.stderr.strip() or 'Git pull failed'}
        else:
            results[name] = {'status': 'ok', 'message': 'Local repository ready'}

    return results


def push_repos(root: Path | str | None = None, auto_commit: bool = False, message: str = "chore: sync workspace changes") -> dict:
    '''Push local changes across all configured Git workspace repositories.'''
    if root is None:
        root_path = Path(__file__).resolve().parent.parent
    else:
        root_path = Path(root)

    config = load_repos_config(root_path)
    results = {}

    for name in get_repo_names(root_path):
        repo_dir = root_path / name
        url = config.get('repositories', {}).get(name, '')

        if url == 'gdrive':
            results[name] = {'status': 'skipped', 'message': 'Managed via Google Drive'}
            continue

        if not repo_dir.exists() or not (repo_dir / '.git').exists():
            results[name] = {'status': 'error', 'message': 'Local repository missing'}
            continue

        if not url or url == 'local':
            results[name] = {'status': 'skipped', 'message': 'No remote origin configured'}
            continue

        # Check for uncommitted changes
        if auto_commit:
            st = subprocess.run(['git', 'status', '--porcelain'], cwd=repo_dir, capture_output=True, text=True, check=False)
            if st.stdout.strip():
                subprocess.run(['git', 'add', '-A'], cwd=repo_dir, check=False)
                subprocess.run(['git', 'commit', '-m', message], cwd=repo_dir, check=False)

        # Push to origin
        push_res = subprocess.run(
            ['git', 'push', 'origin', 'HEAD'],
            cwd=repo_dir, capture_output=True, text=True, check=False,
        )
        if push_res.returncode == 0:
            results[name] = {'status': 'ok', 'message': 'Pushed to origin'}
        else:
            # Fall back to -u origin HEAD if upstream is not set
            push_u = subprocess.run(
                ['git', 'push', '-u', 'origin', 'HEAD'],
                cwd=repo_dir, capture_output=True, text=True, check=False,
            )
            if push_u.returncode == 0:
                results[name] = {'status': 'ok', 'message': 'Pushed and set upstream on origin'}
            else:
                results[name] = {'status': 'error', 'message': push_u.stderr.strip() or 'Push failed'}

    return results

