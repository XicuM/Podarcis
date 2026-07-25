'''Shared system, process, and file utilities for TUI operations.'''

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
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
        f.write('\n')


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
        with open(path, 'w', encoding='utf-8') as f:
            yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
    except Exception: pass


def get_config_value(root_dir: Path, *keys: str, default: str = '') -> str:
    '''Read a nested config value from .podarcis/config.yaml.'''
    cfg = load_yaml(root_dir / '.podarcis' / 'config.yaml')
    val = cfg
    for k in keys:
        if isinstance(val, dict):
            val = val.get(k, {})
        else:
            return default
    return val if isinstance(val, str) else default


def set_config_value(root_dir: Path, value: str, *keys: str) -> None:
    '''Write a nested config value into .podarcis/config.yaml.'''
    cfg_path = root_dir / '.podarcis' / 'config.yaml'
    data = load_yaml(cfg_path)
    target = data
    for k in keys[:-1]:
        target = target.setdefault(k, {})
    target[keys[-1]] = value
    save_yaml(cfg_path, data)


def set_engine_status(root_dir: Path, engine_name: str, enabled: bool) -> None:
    '''Update engine status in .podarcis/config.yaml.'''
    cfg_path = root_dir/'.podarcis'/'config.yaml'
    data = load_yaml(cfg_path)
    engines = data.setdefault('engines', {})
    engines[engine_name] = enabled
    save_yaml(cfg_path, data)


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
