'''Questionary-based interactive TUI configuration runner.'''

from pathlib import Path

import questionary

from banner import display_project_banner
from common import get_config_value
from console import console, QSTYLE
from config_wizard import (
    configure_mcp_servers, configure_skills, configure_agents, configure_jobs,
    configure_repositories, configure_backend, configure_frontend,
)

_MENU = ['MCP Servers', 'Skills', 'Agents', 'Jobs', 'Repositories', 'Backend', 'Frontend', 'Exit']


def interactive_config(root: Path) -> None:
    style = questionary.Style(QSTYLE)
    yaml_path = root / '.podarcis' / 'config.yaml'
    initial = yaml_path.read_text(encoding='utf-8') if yaml_path.exists() else ''

    while True:
        console.clear()
        display_project_banner(root)

        action = questionary.select(
            'What would you like to configure?',
            choices=_MENU, style=style,
        ).ask()

        if action in ('Exit', None):
            current = yaml_path.read_text(encoding='utf-8') if yaml_path.exists() else ''
            if initial != current:
                console.print('[bold green]✓ .podarcis/config.yaml modified.[/bold green]')
            console.print('[bold #29b8db]Exiting config.[/bold #29b8db]')
            break

        match action:
            case 'MCP Servers':    configure_mcp_servers(root, style=style)
            case 'Skills':         configure_skills(root, style=style)
            case 'Agents':         configure_agents(root, style=style)
            case 'Jobs':           configure_jobs(root, style=style)
            case 'Repositories':   configure_repositories(root, style=style)
            case 'Backend':        configure_backend(root, style=style)
            case 'Frontend':       configure_frontend(root, style=style)
