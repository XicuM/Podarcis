'''Shared system, process, state, and configuration file utilities for Podarcis.'''

import json, subprocess, sys
from pathlib import Path
from console import console


def run_command(cmd: list[str], check: bool = True, cwd: Path | None = None) -> None:
    '''Execute shell command with status logging.'''
    console.print(f'[dim]Running command: {" ".join(cmd)}[/dim]')
    try: subprocess.run(cmd, check=check, cwd=cwd)
    except subprocess.CalledProcessError as e:
        console.print(f'[bold red]Error running command: {e}[/bold red]')
        if check: sys.exit(1)


def load_json(path: Path) -> dict:
    '''Safely read JSON file contents.'''
    if not path.exists(): return {}
    try:
        with open(path, 'r', encoding='utf-8') as f: return json.load(f)
    except Exception: return {}


def save_json(path: Path, data: dict) -> None:
    '''Persist dict to formatted JSON file.'''
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
            f.write('\n')
    except Exception: pass


def load_yaml(path: Path) -> dict:
    '''Safely read YAML file contents.'''
    if not path.exists(): return {}
    try:
        import yaml
        with open(path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
            return data if isinstance(data, dict) else {}
    except Exception: return {}


def save_yaml(path: Path, data: dict) -> None:
    '''Persist dict to YAML file.'''
    try:
        import yaml
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
    except Exception: pass


STATE_KEYS = {'engines', 'mcp_servers', 'gdrive_sync', 'sources_backend', 'jobs', 'frontend'}


def get_state_value(root_dir: Path, *keys: str, default: str = '') -> str:
    '''Read a nested runtime state value from .podarcis/state.yaml (falling back to config.yaml).'''
    st = load_yaml(root_dir / '.podarcis' / 'state.yaml')
    val = st
    for k in keys:
        if isinstance(val, dict) and k in val:
            val = val[k]
        else:
            val = None
            break
    if val is not None and isinstance(val, str):
        return val

    # Fallback to config.yaml
    cfg = load_yaml(root_dir / '.podarcis' / 'config.yaml')
    val = cfg
    for k in keys:
        if isinstance(val, dict):
            val = val.get(k, {})
        else:
            return default
    return val if isinstance(val, str) else default


def set_state_value(root_dir: Path, value: str, *keys: str) -> None:
    '''Write a nested runtime state value into .podarcis/state.yaml.'''
    st_path = root_dir / '.podarcis' / 'state.yaml'
    data = load_yaml(st_path)
    target = data
    for k in keys[:-1]:
        target = target.setdefault(k, {})
    target[keys[-1]] = value
    save_yaml(st_path, data)


def get_config_value(root_dir: Path, *keys: str, default: str = '') -> str:
    '''Read a nested value checking state.yaml first for state keys, then config.yaml.'''
    if keys and keys[0] in STATE_KEYS:
        return get_state_value(root_dir, *keys, default=default)
    cfg = load_yaml(root_dir / '.podarcis' / 'config.yaml')
    val = cfg
    for k in keys:
        if isinstance(val, dict):
            val = val.get(k, {})
        else:
            val = None
            break
    if isinstance(val, str) and val:
        return val
    return val if isinstance(val, str) else default


def set_config_value(root_dir: Path, value: str, *keys: str) -> None:
    '''Write a value to state.yaml if it's state-related, or config.yaml otherwise.'''
    if keys and keys[0] in STATE_KEYS:
        set_state_value(root_dir, value, *keys)
        return
    cfg_path = root_dir / '.podarcis' / 'config.yaml'
    data = load_yaml(cfg_path)
    target = data
    for k in keys[:-1]:
        target = target.setdefault(k, {})
    target[keys[-1]] = value
    save_yaml(cfg_path, data)


def set_engine_status(root_dir: Path, engine_name: str, enabled: bool) -> None:
    '''Update engine status in .podarcis/state.yaml.'''
    st_path = root_dir / '.podarcis' / 'state.yaml'
    data = load_yaml(st_path)
    engines = data.setdefault('engines', {})
    engines[engine_name] = enabled
    save_yaml(st_path, data)


def load_version_info(root_dir: Path) -> tuple[str, str]:
    '''Read release version from pyproject.toml or VERSION.'''
    version, date = '1.1.0', '2026-07-25'
    pyproject = root_dir / 'pyproject.toml'
    if pyproject.exists():
        try:
            for line in pyproject.read_text(encoding='utf-8').splitlines():
                if line.strip().startswith('version ='):
                    version = line.split('=', 1)[1].strip().strip('"\'')
                    break
        except Exception: pass
    elif (f := root_dir / 'VERSION').exists():
        for line in f.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line.startswith('version='):
                version = line.split('=', 1)[1].strip()
            elif line.startswith('date='):
                date = line.split('=', 1)[1].strip()
    return version, date


def load_one_liners(root_dir: Path) -> list[str]:
    '''Load punchy splash lines from .podarcis/config.yaml.'''
    cfg = load_yaml(root_dir/'.podarcis'/'config.yaml')
    lines = cfg.get('oneliners', [])
    if isinstance(lines, list) and lines:
        return [str(l).strip() for l in lines if str(l).strip()]
    return ['Welcome to the Podarcis TUI!']
