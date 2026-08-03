'''Shared configuration wizard — called by both install (sequential) and interactive (menu loop).'''

from pathlib import Path

import questionary
from rich.panel import Panel

from common import get_config_value, set_config_value
from components import (
    discover_components, get_enabled_mcp_servers,
    build_component_choices, run_mcp_setup,
    set_agent_status, set_mcp_server_status, set_skill_status,
)
from console import console, QSTYLE

_BACKEND_CHOICES = ['opencode', 'codex', 'agy', 'claude', 'openclaw', 'hermes', 'none']
_FRONTEND_CHOICES = ['vscode', 'code-server', 'obsidian', 'none']


def _style(style):
    return style or questionary.Style(QSTYLE)


def _header(title: str | None, description: str | None) -> None:
    if title and description:
        console.print(Panel(
            description.strip(),
            title=f'[bold #29b8db]{title}[/bold #29b8db]',
            border_style='#29b8db', width=72, expand=False,
        ))


def configure_mcp_servers(root: Path, style=None, title=None, description=None) -> None:
    _header(title, description)
    mcp_servers, _, _ = discover_components(root)
    if not mcp_servers:
        return console.print('[dim]No MCP servers discovered.[/dim]')
    st = _style(style)
    while True:
        enabled = get_enabled_mcp_servers(root)
        choices = [(f'{"●" if k in enabled else "○"} {k}') for k in sorted(mcp_servers)] + ['Done']
        choice = questionary.select('Select MCP Server to configure:', choices=choices, style=st).ask()
        if choice in (None, 'Done'):
            break
        key = choice.split(' ', 1)[1].strip()
        info = mcp_servers[key]
        is_on = key in enabled
        has_cfg = (root / '.agents' / 'mcp' / info['dir_name'] / 'setup.py').exists()

        sub = (['Disable'] + (['Configure'] if has_cfg else []) if is_on else ['Enable']) + ['Cancel']
        act = questionary.select('Select Action:', choices=sub, qmark=f'{key} /', style=st).ask()
        if act == 'Enable':
            ok = not has_cfg or questionary.select(
                'Do you want to configure before activating?',
                choices=['yes', 'no'], default='yes', qmark=f'{key} /', style=st,
            ).ask() != 'yes' or run_mcp_setup(root, key)
            set_mcp_server_status(root, key, ok, info)
            console.print(f'[{"bold green" if ok else "yellow"}]✓ {"Enabled" if ok else "disabled"} {key}.[/]\n')
        elif act == 'Disable':
            set_mcp_server_status(root, key, False, info)
            console.print(f'[yellow]Disabled {key}.[/yellow]\n')
        elif act == 'Configure':
            run_mcp_setup(root, key)


def configure_skills(root: Path, style=None, title=None, description=None) -> None:
    _header(title, description)
    _, skills, _ = discover_components(root)
    if not skills:
        return console.print('[dim]No skills discovered.[/dim]')
    selected = questionary.checkbox(
        'Select Skills to enable:',
        choices=build_component_choices(root, 'skill', skills), style=_style(style),
    ).ask()
    if selected is not None:
        s = set(selected)
        for k, info in skills.items():
            set_skill_status(root, k, k in s, info)
        console.print('[bold green]✓ Skill configs updated.[/bold green]')


def configure_agents(root: Path, style=None, title=None, description=None) -> None:
    _header(title, description)
    _, _, agents = discover_components(root)
    if not agents:
        return console.print('[dim]No agents discovered.[/dim]')
    selected = questionary.checkbox(
        'Select Agents to enable:',
        choices=build_component_choices(root, 'agent', agents), style=_style(style),
    ).ask()
    if selected is not None:
        s = set(selected)
        for k, info in agents.items():
            set_agent_status(root, k, k in s, info)
        console.print('[bold green]✓ Agent configs updated.[/bold green]')


def configure_jobs(root: Path, style=None, title=None, description=None) -> None:
    _header(title, description)
    from jobs import discover_jobs, set_job_status
    jobs = discover_jobs(root)
    if not jobs:
        return console.print('[dim]No jobs discovered.[/dim]')
    selected = questionary.checkbox(
        'Select Jobs to enable (automatically syncs crontab):',
        choices=build_component_choices(root, 'job', jobs), style=_style(style),
    ).ask()
    if selected is not None:
        s = set(selected)
        for k in jobs:
            set_job_status(root, k, k in s)
        console.print('[bold green]✓ Job configs and crontab updated.[/bold green]')


def configure_repositories(root: Path, style=None, title=None, description=None) -> None:
    _header(title, description)
    st = _style(style)
    while True:
        choices = [f'{rn} ({get_repo_url(root, rn) or "local"})' for rn in get_active_repo_names(root)] + ['Done']
        sub = questionary.select('Workspace Repository Configuration:', choices=choices, style=st).ask()
        if sub in (None, 'Done'):
            break
        prompt_configure_repo(root, sub.split(' (', 1)[0].strip(), style=st)


def configure_backend(root: Path, style=None, title=None, description=None) -> None:
    _header(title, description)
    current = get_config_value(root, 'backend', default='none')
    backend = questionary.select(
        'Select Agent Backend:', choices=_BACKEND_CHOICES,
        default=current if current in _BACKEND_CHOICES else 'none', style=_style(style),
    ).ask()
    if not backend or backend == current:
        return
    set_config_value(root, backend, 'backend')
    if backend == 'none':
        return console.print('[bold yellow]✓ Backend set to none.[/bold yellow] MCP configuration will be skipped.')
    from backends import generate_for_backend
    from cli import _sync_claudian_plugin
    _sync_claudian_plugin(backend)
    generate_for_backend(root, backend)
    console.print(f'[bold green]✓ Backend set to {backend}.[/bold green]')


def configure_frontend(root: Path, style=None, title=None, description=None) -> None:
    _header(title, description)
    current = get_config_value(root, 'frontend', default='none')
    frontend = questionary.select(
        'Select Frontend Tool:', choices=_FRONTEND_CHOICES,
        default=current if current in _FRONTEND_CHOICES else 'none', style=_style(style),
    ).ask()
    if not frontend:
        return
    set_config_value(root, frontend, 'frontend')
    if frontend in ('vscode', 'code-server'):
        from cli import _ensure_vscode_config, _ensure_docker_image
        _ensure_vscode_config(root)
        if frontend == 'code-server':
            _ensure_docker_image()
        console.print(f'[bold green]✓ Frontend set to {frontend} (VSCode config seeded).[/bold green]')
    elif frontend == 'obsidian':
        backend = get_config_value(root, 'backend', default='none')
        if questionary.confirm(
            f'Configure Obsidian plugins for agentic knowledge base (Claudian plugin for backend "{backend}")?',
            default=True, style=_style(style),
        ).ask():
            from cli import _sync_claudian_plugin
            _sync_claudian_plugin(backend)
            console.print('[bold green]✓ Claudian plugin configured for Obsidian.[/bold green]')
        else:
            console.print(f'[bold green]✓ Frontend set to obsidian.[/bold green] [dim]Skipped plugin configuration.[/dim]')
    elif frontend == 'none':
        console.print('[bold yellow]✓ Frontend set to none.[/bold yellow] Opening a frontend will be skipped.')
    else:
        console.print(f'[bold green]✓ Frontend set to {frontend}.[/bold green]')
