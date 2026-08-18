#!/usr/bin/env python3
'''Automated bootstrap, MCP/skill config, and credential setup.'''

import os
import shutil
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
podarcis_dir = Path(__file__).resolve().parent
if str(root) not in sys.path:
    sys.path.insert(0, str(root))
if str(podarcis_dir) not in sys.path:
    sys.path.insert(0, str(podarcis_dir))

def _bootstrap_venv() -> None:
    venv_dir = root / '.venv'
    exe_suffix = 'Scripts' if sys.platform == 'win32' else 'bin'
    python = venv_dir / exe_suffix / 'python'
    pip = venv_dir / exe_suffix / ('pip.exe' if sys.platform == 'win32' else 'pip')

    if sys.executable == str(python):
        return  # already inside venv

    if not venv_dir.exists() or not python.exists():
        print('[✓] Creating Python virtual environment (.venv)...')
        subprocess.run([sys.executable, '-m', 'venv', '--clear', str(venv_dir)], check=True)

    deps_ok = (
        python.exists()
        and subprocess.run(
            [str(python), '-c', 'import rich, questionary'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0
    )

    if not deps_ok:
        req_file = podarcis_dir / 'requirements.txt'
        if not req_file.exists():
            req_file = root / 'requirements.txt'

        print(f'[✓] Installing dependencies from {req_file.name}...')

        if not pip.exists():
            print('[✓] Bootstrapping pip via ensurepip...')
            subprocess.run([str(python), '-m', 'ensurepip', '--upgrade'])

        pip_cmd: list[str] = [str(pip)] if pip.exists() else [str(python), '-m', 'pip']

        subprocess.run(pip_cmd + ['install', '--upgrade', 'pip'])
        if req_file.exists():
            subprocess.run(pip_cmd + ['install', '-r', str(req_file)])
        subprocess.run(pip_cmd + ['install', '-e', str(root)])

        if (egg_info := root / 'podarcis.egg-info').exists():
            shutil.rmtree(egg_info, ignore_errors=True)

    if (root / 'podarcis').exists():
        os.chmod(root / 'podarcis', 0o755)

    if '--bootstrap-only' in sys.argv:
        sys.exit(0)

    if python.exists():
        argv = [a for a in sys.argv if a != '--bootstrap-only']
        os.execv(str(python), [str(python)] + argv)

_bootstrap_venv()

from banner import display_install_banner
from common import load_yaml, run_command, save_yaml
from console import console, QSTYLE

import questionary

def _say(*args: str) -> None:
    console.print(''.join(args))

def _hr() -> None:
    _say('[dim]' + '─' * 72 + '[/dim]')

def _select(prompt: str, choices: list[str], default: str | None = None) -> str:
    ans = questionary.select(
        prompt, choices=choices, default=default, qmark='?',
        style=questionary.Style(QSTYLE),
    ).ask()
    if ans is None:
        console.print('\n[yellow]Cancelled by user. Exiting.[/yellow]')
        raise SystemExit(1)
    return ans


# ── .podarcis/config.yaml ──────────────────────────────────────────────────

def _create_podarcis_yaml() -> None: 
    cfg_file = root / '.podarcis' / 'config.yaml'
    st_file = root / '.podarcis' / 'state.yaml'

    if not cfg_file.exists():
        save_yaml(cfg_file, {
            'apis': {'semantic_scholar_api_key': 'your_api_key_here'},
            'remote_mcp': {
                'drive': {
                    'url': 'https://drivemcp.googleapis.com/mcp/v1',
                    'transport': 'streamable-http',
                    'oauth': {
                        'client_id_env': 'GDRIVE_OAUTH_CLIENT_ID',
                        'client_secret_env': 'GDRIVE_OAUTH_CLIENT_SECRET',
                        'scope': 'https://www.googleapis.com/auth/drive.readonly',
                    },
                }
            },
            'oneliners': [
                'Agile enough to catch a dangling reference.',
                'Quick on its feet. Quicker on the audit.',
                'Wall-crawling through the taxonomy tree, link by link.',
                'Filesystem traversal at gecko speed.',
                'Podarcis: endemic to knowledge graphs everywhere.',
            ],
            'harness': 'opencode',
            'frontend': 'none',
        })

    if not st_file.exists():
        save_yaml(st_file, {
            'engines': {'qmd': False},
            'mcp_servers': {'finance-mcp': False, 'menumaker-mcp': False},
            'gdrive_sync': {'last_sync': ''},
        })

# ── global command ────────────────────────────────────────────────────────

def _configure_global_command() -> None:
    _say('Optionally installs the "podarcis" CLI executable into ~/.local/bin '
         'allowing you to run status, config, test, and lint commands from any shell prompt.')
    if _select('Install command globally in ~/.local/bin?', ['yes', 'no'], default='yes') == 'no':
        return _say('[dim]Skipped global command installation. You can run `./podarcis` from project root.[/dim]\n')
    name = questionary.text(
        'Command name (Enter for "podarcis"):', default='podarcis',
        style=questionary.Style(QSTYLE),
    ).ask() or 'podarcis'
    local_bin = Path.home() / '.local' / 'bin'
    local_bin.mkdir(parents=True, exist_ok=True)
    target = local_bin / name
    source = root / 'podarcis'
    try:
        target.unlink(missing_ok=True)
        target.symlink_to(source)
        _say(f'[bold green]✓ Symlinked {target} → {source}[/bold green]')
        if str(local_bin) not in os.environ.get('PATH', '').split(os.pathsep):
            _say(f'[bold yellow]⚠️ Note: {local_bin} is not currently in your PATH.[/bold yellow]')
            _say('[dim]Add `export PATH="$HOME/.local/bin:$PATH"` to your ~/.bashrc or ~/.zshrc.[/dim]')
    except Exception as err:
        _say(f'[bold red]Failed to create symlink: {err}[/bold red]')
    _say()


# ── main ───────────────────────────────────────────────────────────────────

def main() -> None:
    _bootstrap_venv()

    console.clear()
    display_install_banner(root)

    _create_podarcis_yaml()
    _hr()

    from config_wizard import (
        configure_mcp_servers, configure_skills, configure_agents, configure_jobs,
        configure_harness, configure_frontend, configure_repositories,
    )

    configure_mcp_servers(root, title='MCP Servers Configuration',
        description='Model Context Protocol (MCP) servers equip Podarcis subagents with external tool capabilities and data source integrations.')
    _hr()
    configure_skills(root, title='Agent Skills Configuration',
        description='Skills extend subagents with specialized workflows, Python code execution, context compaction, and runtime harnesses.')
    _hr()
    configure_agents(root, title='Subagent Personas Configuration',
        description='Personas configure autonomous subagents under .agents/agents/ for multi-agent literature discovery, protocol architecture, and OKF concept verification.')
    _hr()
    configure_harness(root, title='Agent Harness',
        description='The harness is the AI coding tool Podarcis subagents run inside. Choosing a harness writes MCP server configuration to its native config file.')
    _hr()
    configure_frontend(root, title='Frontend Tool',
        description='The frontend is the editor or knowledge-base viewer for wiki and workspace files.\nObsidian: markdown vault viewer. VSCode: full IDE.')
    _hr()
    configure_repositories(root, title='Workspace Repositories',
        description='Podarcis manages knowledge across Open Knowledge Format (OKF v0.2) repositories:\n  • wiki: Objective knowledge base (anonymized concepts & references)\n  • workspace: Actionable deliverables (user profiles, protocols, reviews)')
    _hr()
    configure_jobs(root, title='Scheduled Jobs',
        description='Jobs automate periodic tasks like GDrive sync and wiki audits. Enable them to schedule automatic background execution via cron.')
    _hr()
    _configure_global_command()
    _hr()

    if _select('Sync workspace repos now?', ['no', 'yes'], default='yes') == 'yes':
        from repos import sync_repos
        _say('[#29b8db]Syncing workspace repos...[/#29b8db]')
        sync_repos(root, clone_missing=True, update_remotes=True)
        _say()

    from rich.panel import Panel
    console.print(Panel(
            '[bold green]✓ Bootstrap & Setup Complete![/bold green]\n\n'
            '- Virtual environment configured, deps in .venv/\n'
            '- Configuration (.podarcis/config.yaml) prepared.\n\n'
            '[bold #29b8db]Quick Commands:[/bold #29b8db]\n'
            '  • Status:       [bold]podarcis status[/bold] (or [bold]podarcis status --json[/bold])\n'
            '  • CLI Config:   [bold]podarcis config enable/disable/repo[/bold]\n'
            '  • Interactive:  [bold]podarcis config interactive[/bold]\n'
            '  • Test:         [bold]podarcis test[/bold]\n'
            '  • Lint:         [bold]podarcis lint[/bold]',
            border_style='green', width=72, expand=False,
        ))

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
