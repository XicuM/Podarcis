'''Shared system, process, state, and configuration file utilities for Podarcis.'''

import json, subprocess, sys
from pathlib import Path
from podarcis.console import console, err_console


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
    '''Persist dict to formatted JSON file.

    Failures are reported, not swallowed: this writes configuration, and a
    silently dropped write is a setting the user believes they changed.
    '''
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
            f.write('\n')
    except OSError as e:
        console.print(f'[bold red]Failed to write {path}:[/bold red] {e}')


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
    '''Persist dict to YAML file.

    Failures are reported, not swallowed: see save_json.
    '''
    import yaml
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
    except (OSError, yaml.YAMLError) as e:
        console.print(f'[bold red]Failed to write {path}:[/bold red] {e}')


def config_path(root_dir: Path) -> Path:
    '''The one configuration file. There is no second one.

    Runtime state used to live in a parallel .podarcis/state.yaml, with a
    hardcoded STATE_KEYS set routing each key to one file or the other. Both
    files were git-ignored, so the split bought nothing and cost correctness:
    the MCP servers read `engines.qmd` and `sources_backend` straight out of
    config.yaml while the TUI wrote them to state.yaml, so configuring either
    through `podarcis config` never reached the server that reads it.
    '''
    return root_dir / '.podarcis' / 'config.yaml'


_migrated: set[Path] = set()


def _migrate_legacy_state(root_dir: Path) -> None:
    '''Fold a pre-8.0 .podarcis/state.yaml into config.yaml, once per process.

    state.yaml took read priority over config.yaml, so its values win the merge.
    '''
    if root_dir in _migrated:
        return
    _migrated.add(root_dir)

    st_path = root_dir / '.podarcis' / 'state.yaml'
    if not st_path.exists():
        return

    def merge(base: dict, over: dict) -> dict:
        for k, v in over.items():
            if isinstance(v, dict) and isinstance(base.get(k), dict):
                merge(base[k], v)
            else:
                base[k] = v
        return base

    state = load_yaml(st_path)
    if state:
        save_yaml(config_path(root_dir), merge(load_yaml(config_path(root_dir)), state))
    st_path.unlink()
    err_console.print('[dim]Merged legacy .podarcis/state.yaml into config.yaml.[/dim]')


def load_config(root_dir: Path) -> dict:
    '''Read .podarcis/config.yaml, migrating any legacy state.yaml first.'''
    _migrate_legacy_state(root_dir)
    return load_yaml(config_path(root_dir))


def get_config_value(root_dir: Path, *keys: str, default: str = '') -> str:
    '''Read a nested value from .podarcis/config.yaml.'''
    val = load_config(root_dir)
    for k in keys:
        if not isinstance(val, dict):
            return default
        val = val.get(k)
    return val if isinstance(val, str) and val else default


def set_config_value(root_dir: Path, value: object, *keys: str) -> None:
    '''Write a nested value into .podarcis/config.yaml.'''
    data = load_config(root_dir)
    target = data
    for k in keys[:-1]:
        target = target.setdefault(k, {})
    target[keys[-1]] = value
    save_yaml(config_path(root_dir), data)


def load_version_info(root_dir: Path) -> tuple[str, str]:
    '''Release version from pyproject.toml, dated by the latest commit.

    pyproject is the only source: AGENTS.md requires a bump on every platform
    commit precisely so drift is visible, and an editable install caches its
    metadata version at install time.
    '''
    version, date = 'unknown', ''
    pyproject = root_dir / 'pyproject.toml'
    if pyproject.exists():
        for line in pyproject.read_text(encoding='utf-8').splitlines():
            if line.strip().startswith('version ='):
                version = line.split('=', 1)[1].strip().strip('"\'')
                break

    result = subprocess.run(
        ['git', '-C', str(root_dir), 'log', '-1', '--format=%cs'],
        capture_output=True, text=True, timeout=5, check=False,
    )
    if result.returncode == 0:
        date = result.stdout.strip()
    return version, date


def load_one_liners(root_dir: Path) -> list[str]:
    '''Load punchy splash lines from .podarcis/config.yaml.'''
    lines = load_config(root_dir).get('oneliners', [])
    if isinstance(lines, list) and lines:
        return [str(l).strip() for l in lines if str(l).strip()]
    return ['Welcome to the Podarcis TUI!']
