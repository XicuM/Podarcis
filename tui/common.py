'''Shared system, process, and file utilities for TUI operations.'''

import json
import subprocess
import sys
from pathlib import Path
from tui.console import console


def get_root_dir() -> Path:
    '''Resolve project root directory.'''
    return Path(__file__).resolve().parent.parent


def get_venv_pip(root: Path) -> list[str]:
    '''Find virtualenv pip executable or fall back to system python module.'''
    venv_pip = root / '.venv' / ('Scripts/pip.exe' if sys.platform == 'win32' else 'bin/pip')
    return [str(venv_pip)] if venv_pip.exists() else [sys.executable, '-m', 'pip']


def run_command(cmd: list[str], check: bool = True, cwd: Path | None = None) -> None:
    '''Execute shell command with status logging.'''
    console.print(f'[dim]Running command: {" ".join(cmd)}[/dim]')
    try:
        subprocess.run(cmd, check=check, cwd=cwd)
    except subprocess.CalledProcessError as e:
        console.print(f'[bold red]Error running command: {e}[/bold red]')
        if check:
            sys.exit(1)


def load_json(path: Path) -> dict:
    '''Safely read JSON file contents.'''
    if not path.exists():
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_json(path: Path, data: dict) -> None:
    '''Persist dict to formatted JSON file.'''
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
        f.write('\n')


def load_yaml(path: Path) -> dict:
    '''Safely read YAML file contents.'''
    if not path.exists():
        return {}
    try:
        import yaml
        with open(path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_yaml(path: Path, data: dict) -> None:
    '''Persist dict to YAML file.'''
    try:
        import yaml
        with open(path, 'w', encoding='utf-8') as f:
            yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
    except Exception:
        pass


def load_podarcis_config(root_dir: Path) -> dict:
    '''Load podarcis.yaml configuration with fallback to podarcis.example.yaml.'''
    yaml_path = root_dir / 'podarcis.yaml'
    if yaml_path.exists():
        return load_yaml(yaml_path)
    example_path = root_dir / 'podarcis.example.yaml'
    if example_path.exists():
        return load_yaml(example_path)
    return {}


def set_engine_status(root_dir: Path, engine_name: str, enabled: bool) -> None:
    '''Update engine status in podarcis.yaml.'''
    yaml_path = root_dir / 'podarcis.yaml'
    if not yaml_path.exists():
        example_path = root_dir / 'podarcis.example.yaml'
        if example_path.exists():
            import shutil
            shutil.copy(example_path, yaml_path)
    data = load_podarcis_config(root_dir)
    engines = data.setdefault('engines', {})
    engines[engine_name] = enabled
    save_yaml(yaml_path, data)



def get_git_info(root_dir: Path) -> tuple[str, str]:
    '''Extract current git branch and short commit hash.'''
    try:
        branch = subprocess.check_output(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], cwd=root_dir, text=True).strip()
        commit = subprocess.check_output(['git', 'rev-parse', '--short', 'HEAD'], cwd=root_dir, text=True).strip()
        return branch, commit
    except Exception:
        return 'main', 'unknown'


def load_version_info(root_dir: Path) -> tuple[str, str]:
    '''Read release version and date metadata.'''
    version_file = root_dir / 'VERSION'
    version, date = '0.1.0', '2026-07-24'
    if version_file.exists():
        for line in version_file.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line.startswith('version='):
                version = line.split('=', 1)[1].strip()
            elif line.startswith('date='):
                date = line.split('=', 1)[1].strip()
    return version, date


def load_one_liners(root_dir: Path) -> list[str]:
    '''Load punchy splash lines specifically from wiki/.podarcis/oneliners.txt.'''
    file_path = root_dir / 'wiki' / '.podarcis' / 'oneliners.txt'
    if not file_path.exists():
        file_path = root_dir / 'wiki' / '.pordacis' / 'oneliners.txt'
    if file_path.exists():
        try:
            lines = [l.strip() for l in file_path.read_text(encoding='utf-8').splitlines() if l.strip()]
            if lines:
                return lines
        except Exception:
            pass
    return ['HPDSA: High-Performance Domain-Specific Architecture']
