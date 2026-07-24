#!/usr/bin/env python3
'''Automated bootstrap, MCP/skill config, and credential setup.'''

import os
import shutil
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

from tui.banner import display_project_banner
from tui.common import load_yaml, run_command, save_yaml
from tui.console import console

def _say(*args: str) -> None:
    console.print(''.join(args))

def _hr() -> None:
    _say('[dim]──────────────────────────────────────────────[/dim]')

def _select(prompt: str, choices: list[str], default: str | None = None) -> str:
    '''Arrow-key select prompt.'''
    import questionary
    return questionary.select(
        prompt, choices=choices, default=default,
        style=questionary.Style(_QSTYLE),
    ).ask()

# ── venv bootstrap ─────────────────────────────────────────────────────────

def _bootstrap_venv() -> None:
    venv_dir = root / '.venv'
    exe_suffix = 'Scripts' if sys.platform == 'win32' else 'bin'
    python = venv_dir / exe_suffix / 'python'
    pip = venv_dir / exe_suffix / ('pip.exe' if sys.platform == 'win32' else 'pip')

    if sys.executable == str(python):
        return  # already inside venv

    if not venv_dir.exists():
        _say('[green]✓ Creating Python virtual environment (.venv)...[/green]')
        run_command([sys.executable, '-m', 'venv', str(venv_dir)])

    rich_ok = (
        python.exists()
        and subprocess.run(
            [str(python), '-c', 'import rich'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0
    )

    if not rich_ok:
        _say('[green]✓ Installing dependencies from requirements.txt...[/green]')
        pip_bin = (
            str(pip) if pip.exists()
            else [sys.executable, '-m', 'pip'])
        run_command(
            [pip_bin, 'install', '--upgrade', 'pip']
            if isinstance(pip_bin, str)
            else pip_bin + ['install', '--upgrade', 'pip'])
        run_command(
            [pip_bin, 'install', '-r', 'requirements.txt']
            if isinstance(pip_bin, str)
            else pip_bin + ['install', '-r', 'requirements.txt'])

    if python.exists():
        _say('[#29b8db]Re-launching setup inside virtual environment...[/#29b8db]')
        os.execv(str(python), [str(python)] + sys.argv)

# ── podarcis.yaml ──────────────────────────────────────────────────────────

def _create_podarcis_yaml() -> None: 
    if (yaml := root/'podarcis.yaml').exists(): return
    save_yaml(yaml, {'apis': {}, 'repositories': {}, 'engines': {'qmd': False}})

def _configure_repos() -> None:
    import questionary
    from tui.repos import REPO_NAMES, get_repo_url, set_repo_url

    _say('[bold #29b8db]Workspace Repositories[/bold #29b8db]')
    style = questionary.Style(_QSTYLE)

    for name in REPO_NAMES:
        current = get_repo_url(root, name)
        _say()
        url = questionary.text(
            f'Git URL for "{name}" repo (leave empty to skip):\n  >>',
            default=current,
            style=style,
        ).ask()
        if url is None:
            raise SystemExit(1)
        url = url.strip()
        if url and url != current:
            set_repo_url(root, name, url, update_remote=False)
            _say(f'[green]✓ {name} → {url}[/green]')
        elif url: _say(f'[dim]{name}: {url}[/dim]')
        else: _say(f'[dim]{name}: skipped[/dim]')
    _say()

# ── QMD engine ─────────────────────────────────────────────────────────────

def _configure_qmd(enabled_servers: set[str]) -> None:
    if not (enabled_servers & {'wiki-mcp', 'wiki'}):
        return

    from tui.common import set_engine_status

    pod_cfg = load_yaml(root/'podarcis.yaml')
    current = bool(pod_cfg.get('engines', {}).get('qmd', False))

    qmd_bin = shutil.which('qmd')
    _say(f'[bold #29b8db]QMD Vector DB Search Engine[/bold #29b8db]')
    _say(f'   Binary: {"Available" if qmd_bin else "Missing"}'
         f'{" (" + qmd_bin + ")" if qmd_bin else ""}\n')

    choice = _select(
        'Enable QMD?', ['no', 'yes'],
        default='yes' if current else 'no')
    enable = choice == 'yes'
    set_engine_status(root, 'qmd', enable)

    if not enable:
        _say('[yellow]✓ QMD disabled — native keyword search.[/yellow]\n')
        return

    if qmd_bin:
        _say('[green]✓ QMD CLI found and enabled.[/green]\n')
        return

    _say('[bold yellow]⚠️ QMD CLI not found in PATH.[/bold yellow]')
    if not shutil.which('npm'):
        _say('[bold red]⚠️ npm missing. Install Node.js + @tobilu/qmd manually.[/bold red]\n')
        return

    if _select('Install @tobilu/qmd globally via npm?', ['no', 'yes'], default='yes') == 'yes':
        _say('[#29b8db]Installing @tobilu/qmd globally...[/#29b8db]')
        run_command(['npm', 'install', '-g', '@tobilu/qmd'], check=False)
        if shutil.which('qmd'):
            _say('[green]✓ QMD CLI installed.[/green]')
        else:
            _say('[bold yellow]⚠️ QMD install failed. wiki-mcp falls back to native search.[/bold yellow]')
    _say()

# ── questionary helpers ───────────────────────────────────────────────────

def _ensure_questionary() -> bool:
    try:
        import questionary  # noqa: F401
        return True
    except ImportError:
        pass

    from tui.components import install_deps
    _say('[#29b8db]Installing questionary for interactive menus...[/#29b8db]')
    install_deps(root, 'questionary', False, 'Installing questionary')
    try:
        import questionary  # noqa: F401
        return True
    except ImportError:
        _say('[red]Could not install questionary. Skipping interactive setup.[/red]')
        return False

_QSTYLE = [
    ('qmark',           'fg:#29b8db bold'),
    ('question',        'bold white'),
    ('answer',          'fg:#29b8db bold'),
    ('pointer',         'fg:#29b8db bold'),
    ('highlighted',     'noinherit fg:white'),
    ('selected',        'noinherit fg:white'),
    ('checkbox-checked',   'fg:#29b8db bold'),
    ('checkbox-unchecked', 'fg:#888888'),
    ('checkbox-selected',  'fg:#29b8db bold'),
]

# ── MCP servers ────────────────────────────────────────────────────────────

def _prompt_research_credentials() -> bool:
    '''Prompt for Semantic Scholar API key. Returns False if skipped.'''
    import questionary

    _say('[bold #29b8db]Credentials for research-mcp[/bold #29b8db]')
    key = questionary.text(
        'Semantic Scholar API key (leave empty to skip)',
        style=questionary.Style(_QSTYLE),
    ).ask()
    if key is None:
        raise SystemExit(1)
    if not key.strip():
        _say('[yellow]⚠️ No API key — research-mcp deactivated.[/yellow]\n')
        return False

    yaml_path = root / 'podarcis.yaml'
    data = load_yaml(yaml_path) if yaml_path.exists() else {}
    data.setdefault('apis', {})['semantic_scholar_api_key'] = key.strip()
    save_yaml(yaml_path, data)
    _say('[green]✓ API key saved.[/green]\n')
    return True


def _prompt_gdrive_credentials() -> bool:
    '''Prompt for Google Drive OAuth. Returns False if skipped or failed.'''
    import questionary

    _say('[bold #29b8db]Credentials for google-drive-mcp[/bold #29b8db]')
    choice = questionary.select(
        'Set up Google Drive access?',
        choices=['yes', 'skip'], default='yes',
        style=questionary.Style(_QSTYLE),
    ).ask()
    if choice is None:
        raise SystemExit(1)
    if choice == 'skip':
        _say('[yellow]⚠️ Google Drive not configured — gdrive-mcp deactivated.[/yellow]\n')
        return False

    from tui.gdrive import setup_google_drive
    if setup_google_drive(root):
        return True
    _say('[yellow]⚠️ Google Drive setup failed — gdrive-mcp deactivated.[/yellow]\n')
    return False


def _configure_mcp_servers() -> set[str]:
    import questionary
    from tui.components import (
        discover_components,
        get_enabled_mcp_servers, set_mcp_server_status,
    )

    servers, _ = discover_components(root)
    if not servers:
        _say('[dim]No MCP servers discovered.[/dim]')
        return set()

    enabled = get_enabled_mcp_servers(root)
    style = questionary.Style(_QSTYLE)
    choices = [
        questionary.Choice(
            title=k,
            checked=(k in enabled),
        )
        for k in sorted(servers)
    ]
    selected = (
        questionary.checkbox(
            'Select MCP servers to activate:',
            choices=choices, style=style)
        .ask()
    )
    if selected is None:
        raise SystemExit(1)

    s = set(selected)

    if s & {'research-mcp', 'research'} and not _prompt_research_credentials():
        s -= {'research-mcp', 'research'}
    if s & {'google-drive-mcp', 'gdrive'} and not _prompt_gdrive_credentials():
        s -= {'google-drive-mcp', 'gdrive'}

    for k, info in servers.items():
        set_mcp_server_status(root, k, k in s, info)
    _say('[bold green]✓ MCP server configurations updated.[/bold green]\n')
    return s

# ── skills ─────────────────────────────────────────────────────────────────

def _configure_skills() -> None:
    import questionary
    from tui.components import discover_components, set_skill_status

    _, skills = discover_components(root)
    if not skills:
        _say('[dim]No skills discovered.[/dim]')
        return

    style = questionary.Style(_QSTYLE)
    choices = [
        questionary.Choice(title=k, checked=skills[k]['enabled'])
        for k in sorted(skills)
    ]
    selected = (
        questionary.checkbox(
            'Select skills to enable:', choices=choices, style=style)
        .ask()
    )
    if selected is None:
        raise SystemExit(1)

    s = set(selected)
    for k, info in skills.items():
        set_skill_status(root, k, k in s, info)
    _say('[bold green]✓ Skill configurations updated.[/bold green]\n')

# ── main ───────────────────────────────────────────────────────────────────

def main() -> None:
    _bootstrap_venv()

    console.clear()
    display_project_banner(root)

    _create_podarcis_yaml()
    _hr()

    if not _ensure_questionary():
        _say('[yellow]Skipping interactive setup. Run [bold]make config[/bold] later.[/yellow]\n')
    else:
        enabled = _configure_mcp_servers()
        _hr()
        _configure_qmd(enabled)
        _hr()
        _configure_skills()
        _hr()
        _configure_repos()
        _hr()

    from tui.repos import sync_repos
    _say('[#29b8db]Syncing workspace repos...[/#29b8db]')
    sync_repos(root, clone_missing=True, update_remotes=True)
    _say()

    from rich.panel import Panel
    console.print(Panel(
            '[bold green]✓ Bootstrap & Setup Complete![/bold green]\n\n'
            '- Virtual environment configured, deps in .venv/\n'
            '- Configuration (podarcis.yaml) prepared.\n\n'
            '[bold #29b8db]Quick Commands:[/bold #29b8db]\n'
            '  • Reconﬁgure: [bold]make config[/bold]\n'
            '  • Re-sync:    [bold]make sync[/bold]\n'
            '  • Test:       [bold]make test[/bold]\n'
            '  • Lint:       [bold]make lint[/bold]',
            border_style='green', expand=False,
        ))

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
