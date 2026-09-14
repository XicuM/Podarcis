'''Multi-project management and XDG-compliant project resolution for Podarcis.'''

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from podarcis.common import load_yaml, save_yaml
from podarcis.console import console


def get_xdg_config_dir() -> Path:
    '''Resolve Podarcis XDG configuration directory (~/.config/podarcis).'''
    base = os.environ.get('XDG_CONFIG_HOME')
    path = Path(base) if base else Path.home() / '.config'
    return path / 'podarcis'


def get_xdg_data_dir() -> Path:
    '''Resolve Podarcis XDG data directory (~/.local/share/podarcis).'''
    base = os.environ.get('XDG_DATA_HOME')
    path = Path(base) if base else Path.home() / '.local' / 'share'
    return path / 'podarcis'


def get_xdg_state_dir() -> Path:
    '''Resolve Podarcis XDG state directory (~/.local/state/podarcis).'''
    base = os.environ.get('XDG_STATE_HOME')
    path = Path(base) if base else Path.home() / '.local' / 'state'
    return path / 'podarcis'


def get_xdg_cache_dir() -> Path:
    '''Resolve Podarcis XDG cache directory (~/.cache/podarcis).'''
    base = os.environ.get('XDG_CACHE_HOME')
    path = Path(base) if base else Path.home() / '.cache'
    return path / 'podarcis'


def get_projects_dir() -> Path:
    '''Default directory containing user research projects (~/.local/share/podarcis/projects).'''
    return get_xdg_data_dir() / 'projects'


def global_config_path() -> Path:
    '''Path to the global Podarcis configuration file (~/.config/podarcis/config.yaml).'''
    return get_xdg_config_dir() / 'config.yaml'


def load_global_config() -> dict[str, Any]:
    '''Load the global user configuration.'''
    path = global_config_path()
    if not path.is_file():
        return {}
    return load_yaml(path) or {}


def save_global_config(data: dict[str, Any]) -> None:
    '''Save the global user configuration.'''
    path = global_config_path()
    save_yaml(path, data)


@dataclass
class Project:
    '''A Podarcis research project workspace (wiki, workspace, sources).'''
    name: str
    root: Path

    @property
    def wiki(self) -> Path:
        return self.root / 'wiki'

    @property
    def workspace(self) -> Path:
        return self.root / 'workspace'

    @property
    def sources(self) -> Path:
        return self.root / 'sources'

    @property
    def config_path(self) -> Path:
        pod_yaml = self.root / 'podarcis.yaml'
        if pod_yaml.is_file():
            return pod_yaml
        return self.root / '.podarcis' / 'config.yaml'

    def exists(self) -> bool:
        return self.root.is_dir()

    def load_config(self) -> dict[str, Any]:
        if self.config_path.is_file():
            return load_yaml(self.config_path) or {}
        return {}

    def save_config(self, data: dict[str, Any]) -> None:
        save_yaml(self.config_path, data)


def is_project_root(path: Path) -> bool:
    '''A directory is a project root if it has podarcis.yaml, .podarcis/config.yaml, or wiki/ + workspace/.'''
    p = Path(path).resolve()
    if not p.is_dir():
        return False
    if (p / 'podarcis.yaml').is_file():
        return True
    if (p / '.podarcis' / 'config.yaml').is_file():
        return True
    if (p / 'wiki').is_dir() and (p / 'workspace').is_dir():
        return True
    return False


def list_projects() -> dict[str, dict[str, Any]]:
    '''Return mapping of project names to details: {name: {path, active, description}}.'''
    gcfg = load_global_config()
    raw_projects = gcfg.get('projects') or {}
    active = gcfg.get('active_project', 'default')

    out: dict[str, dict[str, Any]] = {}
    if isinstance(raw_projects, dict):
        for name, entry in raw_projects.items():
            if isinstance(entry, dict):
                p_path = entry.get('path', '')
                desc = entry.get('description', '')
            else:
                p_path = str(entry)
                desc = ''
            out[name] = {
                'path': p_path,
                'description': desc,
                'active': (name == active),
                'exists': Path(p_path).is_dir() if p_path else False,
            }

    # Also detect any unlisted subdirectories inside default projects directory
    pdir = get_projects_dir()
    if pdir.is_dir():
        for child in sorted(pdir.iterdir()):
            if child.is_dir() and child.name not in out:
                if is_project_root(child):
                    out[child.name] = {
                        'path': str(child),
                        'description': '',
                        'active': (child.name == active),
                        'exists': True,
                    }
    return out


def get_active_project_name() -> str:
    '''Get the name of the currently active project.'''
    return load_global_config().get('active_project', 'default')


def herdr_env() -> dict[str, str]:
    '''Environment pinning a herdr invocation to the local machine.

    Herdr keeps its saved-SSH-machine catalog and the selected machine in
    `$XDG_STATE_HOME/herdr/client/`, shared by every herdr client on the box. A
    state home of our own means these workspace calls always address the local
    podarcis session, never whichever machine was last picked elsewhere. Kept in
    step with `client_state_home()` in tui/src/herdr/config.rs.
    '''
    base = os.environ.get('XDG_STATE_HOME')
    root = Path(base) if base and os.path.isabs(base) else Path.home() / '.local' / 'state'
    return {**os.environ, 'XDG_STATE_HOME': str(root / 'podarcis' / 'herdr-state')}


def ensure_herdr_space(name: str, path: Path, focus: bool = False) -> None:
    '''Ensure a Herdr workspace exists for the project if Herdr server is running.'''
    herdr_bin = shutil.which('herdr')
    if not herdr_bin:
        return
    sock = Path.home() / '.config' / 'herdr' / 'sessions' / 'podarcis' / 'herdr.sock'
    if not sock.exists():
        return
    try:
        res = subprocess.run(
            [herdr_bin, '--session', 'podarcis', 'workspace', 'list'],
            capture_output=True, text=True, timeout=2, check=False, env=herdr_env(),
        )
        if res.returncode != 0:
            return
        data = json.loads(res.stdout)
        workspaces = data.get('result', {}).get('workspaces', [])
        for ws in workspaces:
            if ws.get('label') == name:
                if focus and not ws.get('focused'):
                    ws_id = ws.get('workspace_id')
                    if ws_id:
                        subprocess.run(
                            [herdr_bin, '--session', 'podarcis', 'workspace', 'focus', ws_id],
                            capture_output=True, timeout=2, check=False, env=herdr_env(),
                        )
                return
        # Create workspace for project
        flag = '--focus' if focus else '--no-focus'
        subprocess.run(
            [herdr_bin, '--session', 'podarcis', 'workspace', 'create', '--label', name, '--cwd', str(path), flag],
            capture_output=True, timeout=2, check=False, env=herdr_env(),
        )
    except Exception:
        pass


def close_herdr_space(name: str) -> None:
    '''Close the Herdr workspace for the project if it exists.'''
    herdr_bin = shutil.which('herdr')
    if not herdr_bin:
        return
    sock = Path.home() / '.config' / 'herdr' / 'sessions' / 'podarcis' / 'herdr.sock'
    if not sock.exists():
        return
    try:
        res = subprocess.run(
            [herdr_bin, '--session', 'podarcis', 'workspace', 'list'],
            capture_output=True, text=True, timeout=2, check=False, env=herdr_env(),
        )
        if res.returncode != 0:
            return
        data = json.loads(res.stdout)
        workspaces = data.get('result', {}).get('workspaces', [])
        for ws in workspaces:
            if ws.get('label') == name:
                ws_id = ws.get('workspace_id')
                if ws_id:
                    subprocess.run(
                        [herdr_bin, '--session', 'podarcis', 'workspace', 'close', ws_id],
                        capture_output=True, timeout=2, check=False, env=herdr_env(),
                    )
                break
    except Exception:
        pass


def set_active_project(name: str) -> None:
    '''Set the global active project by name.'''
    gcfg = load_global_config()
    projects = list_projects()
    if name not in projects:
        raise ValueError(f"Project '{name}' is not registered. Available: {', '.join(projects.keys()) or 'none'}")
    gcfg['active_project'] = name
    save_global_config(gcfg)
    p_path = projects[name].get('path')
    if p_path and Path(p_path).is_dir():
        ensure_herdr_space(name, Path(p_path), focus=True)


def resolve_project(
    explicit: str | Path | None = None,
    cwd: Path | None = None,
    environ: dict[str, str] | None = None,
) -> Project:
    '''Resolve the active project workspace.

    Resolution order:
    1. Explicit flag (--project)
    2. $PODARCIS_PROJECT or $PODARCIS_ROOT environment variables
    3. CWD walk up (detecting project root)
    4. Active project in global config (~/.config/podarcis/config.yaml)
    5. Fallback to default project (~/.local/share/podarcis/projects/default)
    '''
    env = os.environ if environ is None else environ

    # 1. Explicit flag
    if explicit is not None and str(explicit).strip():
        val = str(explicit).strip()
        # Check if it matches a registered project name
        projects = list_projects()
        if val in projects and projects[val]['path']:
            return Project(name=val, root=Path(projects[val]['path']).resolve())
        path = Path(val).expanduser().resolve()
        name = path.name
        return Project(name=name, root=path)

    # 2. Environment variable
    env_proj = (env.get('PODARCIS_PROJECT') or env.get('PODARCIS_ROOT') or '').strip()
    if env_proj:
        projects = list_projects()
        if env_proj in projects and projects[env_proj]['path']:
            return Project(name=env_proj, root=Path(projects[env_proj]['path']).resolve())
        path = Path(env_proj).expanduser().resolve()
        return Project(name=path.name, root=path)

    # 3. CWD walk
    start = Path(cwd) if cwd is not None else Path.cwd()
    start = start.resolve()
    for candidate in (start, *start.parents):
        if is_project_root(candidate):
            # Check if registered
            for pname, pinfo in list_projects().items():
                if pinfo['path'] and Path(pinfo['path']).resolve() == candidate:
                    return Project(name=pname, root=candidate)
            return Project(name=candidate.name, root=candidate)

    # 4. Active project in global config
    gcfg = load_global_config()
    active_name = gcfg.get('active_project', 'default')
    projects = list_projects()
    if active_name in projects and projects[active_name]['path']:
        p_path = Path(projects[active_name]['path']).resolve()
        if p_path.is_dir():
            return Project(name=active_name, root=p_path)

    # 5. Fallback: default project in XDG data dir
    default_root = get_projects_dir() / 'default'
    return Project(name='default', root=default_root.resolve())


def new_project(
    name: str,
    path: Path | str | None = None,
    description: str = '',
    wiki_remote: str = '',
    workspace_remote: str = '',
    sources_remote: str = '',
    sources_backend: str = 'local',
) -> Project:
    '''Initialize a new research project workspace.'''
    target_root = Path(path).expanduser().resolve() if path else (get_projects_dir() / name).resolve()
    target_root.mkdir(parents=True, exist_ok=True)

    # 1. Initialize wiki, workspace, sources
    wiki_dir = target_root / 'wiki'
    workspace_dir = target_root / 'workspace'
    sources_dir = target_root / 'sources'

    for d, title in [
        (wiki_dir, 'Wiki Knowledge Base'),
        (workspace_dir, 'Workspace Deliverables'),
        (sources_dir, 'Raw Sources & Literature'),
    ]:
        d.mkdir(parents=True, exist_ok=True)
        idx = d / '_index.md'
        if not idx.exists():
            idx.write_text(f'# {title}\n\nOKF v0.2 Knowledge Base for {name}.\n', encoding='utf-8')
        if not (d / '.git').exists():
            subprocess.run(['git', 'init'], cwd=d, capture_output=True, check=False)

    # Literature subdirectory in sources
    (sources_dir / 'literature').mkdir(parents=True, exist_ok=True)

    # 2. Write podarcis.yaml
    cfg_data = {
        'name': name,
        'description': description or f'{name} research project',
        'repositories': {
            'wiki': wiki_remote or 'local',
            'workspace': workspace_remote or 'local',
            'sources': sources_remote or ('gdrive' if sources_backend == 'gdrive' else 'local'),
        },
        'sources_backend': sources_backend,
    }
    save_yaml(target_root / 'podarcis.yaml', cfg_data)

    # 3. Register in global config
    register_project(name, target_root, description=description, set_active=True)

    # 4. Ensure space exists in Herdr if server is running
    ensure_herdr_space(name, target_root, focus=False)

    return Project(name=name, root=target_root)


def register_project(
    name: str,
    path: Path | str,
    description: str = '',
    set_active: bool = False,
) -> None:
    '''Register an existing directory as a known project in global config.'''
    path_resolved = Path(path).expanduser().resolve()
    gcfg = load_global_config()
    projects = gcfg.setdefault('projects', {})
    projects[name] = {
        'path': str(path_resolved),
        'description': description,
    }
    if set_active or 'active_project' not in gcfg:
        gcfg['active_project'] = name
    save_global_config(gcfg)
    ensure_herdr_space(name, path_resolved, focus=set_active)


def unregister_project(name: str, purge: bool = False) -> None:
    '''Unregister a project. If purge is True, delete its files on disk.'''
    gcfg = load_global_config()
    projects = gcfg.get('projects', {})
    if name not in projects:
        # Check if in default projects dir
        pdir = get_projects_dir() / name
        if pdir.is_dir() and purge:
            shutil.rmtree(pdir, ignore_errors=True)
            close_herdr_space(name)
            return
        raise ValueError(f"Project '{name}' not found in registry.")

    p_info = projects.pop(name)
    if gcfg.get('active_project') == name:
        gcfg['active_project'] = next(iter(projects.keys()), 'default')
    save_global_config(gcfg)

    if purge:
        close_herdr_space(name)
        if p_info and p_info.get('path'):
            p_path = Path(p_info['path'])
            if p_path.is_dir():
                shutil.rmtree(p_path, ignore_errors=True)


def migrate_checkout_to_projects_dir(
    checkout_root: Path,
    project_name: str = 'default',
) -> Project:
    '''Migrate existing wiki/, workspace/, sources/ from checkout into ~/.local/share/podarcis/projects/<name>/.'''
    target = (get_projects_dir() / project_name).resolve()
    target.mkdir(parents=True, exist_ok=True)

    for item in ['wiki', 'workspace', 'sources']:
        src = checkout_root / item
        dst = target / item
        if src.is_dir() and not dst.exists():
            shutil.move(str(src), str(dst))

    # Copy config
    src_cfg = checkout_root / '.podarcis' / 'config.yaml'
    if src_cfg.is_file():
        cfg_data = load_yaml(src_cfg)
        save_yaml(target / 'podarcis.yaml', {
            'name': project_name,
            'description': 'Migrated workspace',
            'repositories': cfg_data.get('repositories', {}),
            'sources_backend': cfg_data.get('sources_backend', 'local'),
        })

    register_project(project_name, target, description='Default migrated workspace', set_active=True)
    return Project(name=project_name, root=target)
