'''Questionary-based interactive TUI configuration runner.'''

from pathlib import Path
from banner import display_project_banner
from components import (
    discover_components, get_enabled_mcp_servers,
    build_component_choices, run_mcp_setup,
    set_agent_status, set_mcp_server_status, set_skill_status,
)
from common import get_config_value, set_config_value
from console import console, QSTYLE
from repos import get_repo_names, get_repo_url, prompt_configure_repo

import questionary

def interactive_config(root: Path) -> None:
    '''Run main interactive menu for configuring MCP servers, skills, and
    repositories.'''
    custom_style = questionary.Style(QSTYLE)

    while True:
        console.clear()
        display_project_banner(root)

        mcp_servers, skills, agents = discover_components(root)
        enabled_servers = get_enabled_mcp_servers(root)

        action = questionary.select(
            'What would you like to configure?',
            choices=['MCP Servers', 'Skills', 'Agents', 'Repositories', 'Backend', 'Frontend', 'Exit'],
            style=custom_style,
        ).ask()

        match action:
            case 'Exit' | None:
                console.print('[bold #29b8db]Exiting config.[/bold #29b8db]')
                break

            case 'MCP Servers':
                while True:
                    enabled_servers = get_enabled_mcp_servers(root)
                    choices = []
                    for k in sorted(mcp_servers):
                        is_on = k in enabled_servers
                        bullet = '●' if is_on else '○'
                        choices.append(f'{bullet} {k}')
                    choices.append('Back to Main Menu')

                    server_choice = questionary.select(
                        'Select MCP Server to configure:',
                        choices=choices, style=custom_style,
                    ).ask()

                    if server_choice is None or server_choice == 'Back to Main Menu':
                        break

                    chosen_key = server_choice.split(' ', 1)[1].strip()
                    info = mcp_servers[chosen_key]
                    is_currently_enabled = chosen_key in enabled_servers
                    dir_name = info['dir_name']
                    setup_script = root / '.agents' / 'mcp' / dir_name / 'setup.py'
                    has_config = setup_script.exists()

                    sub_choices = []
                    if is_currently_enabled:
                        sub_choices.append('Disable')
                        if has_config:
                            sub_choices.append('Configure')
                    else:
                        sub_choices.append('Enable')

                    sub_choices.append('Cancel')

                    act = questionary.select(
                        'Select Action:',
                        choices=sub_choices, qmark=f'{chosen_key} /', style=custom_style,
                    ).ask()

                    if act == 'Disable':
                        set_mcp_server_status(root, chosen_key, False, info)
                        console.print(f'[yellow]Disabled {chosen_key}.[/yellow]\n')

                    elif act == 'Enable':
                        should_enable = True
                        if has_config:
                            want_cfg = questionary.select(
                                'Do you want to configure before activating?',
                                choices=['yes', 'no'], default='yes', qmark=f'{chosen_key} /', style=custom_style,
                            ).ask()
                            if want_cfg == 'yes':
                                should_enable = run_mcp_setup(root, chosen_key)

                        if should_enable:
                            set_mcp_server_status(root, chosen_key, True, info)
                            console.print(f'[bold green]✓ Enabled {chosen_key}.[/bold green]\n')
                        else:
                            set_mcp_server_status(root, chosen_key, False, info)
                            console.print(f'[yellow]{chosen_key} remains disabled.[/yellow]\n')

                    elif act == 'Configure':
                        run_mcp_setup(root, chosen_key)

            case 'Skills':
                choices = build_component_choices(root, 'skill', skills)
                selected = questionary.checkbox(
                    'Select Skills to enable:',
                    choices=choices, style=custom_style,
                ).ask()
                if selected is not None:
                    s = set(selected)
                    for k, info in skills.items():
                        set_skill_status(root, k, k in s, info)
                    console.print('[bold green]✓ Skill configs updated.[/bold green]\n')

            case 'Agents':
                choices = build_component_choices(root, 'agent', agents)
                selected = questionary.checkbox(
                    'Select Agents to enable:',
                    choices=choices, style=custom_style,
                ).ask()
                if selected is not None:
                    s = set(selected)
                    for k, info in agents.items():
                        set_agent_status(root, k, k in s, info)
                    console.print('[bold green]✓ Agent configs updated.[/bold green]\n')

            case 'Repositories':
                while True:
                    repo_choices = []
                    for r_name in get_repo_names(root):
                        url = get_repo_url(root, r_name)
                        remote_label = url if url else 'local'
                        repo_choices.append(f'{r_name} ({remote_label})')
                    repo_choices.append('Back to Main Menu')

                    sub_action = questionary.select(
                        'Workspace Repository Configuration:',
                        choices=repo_choices,
                        style=custom_style,
                    ).ask()

                    if sub_action is None or sub_action == 'Back to Main Menu':
                        break

                    selected_repo = sub_action.split(' (', 1)[0].strip()
                    prompt_configure_repo(root, selected_repo, style=custom_style)

            case 'Backend':
                current_backend = get_config_value(root, 'backend', default='opencode')
                backend = questionary.select(
                    'Select Agent Backend:',
                    choices=['opencode', 'codex', 'agy', 'claude'],
                    default=current_backend,
                    style=custom_style,
                ).ask()
                if backend:
                    set_config_value(root, backend, 'backend')
                    console.print(f'[bold green]✓ Backend set to {backend}.[/bold green]\n')

            case 'Frontend':
                current_frontend = get_config_value(root, 'frontend', default='vscode')
                frontend = questionary.select(
                    'Select Frontend Tool:',
                    choices=['vscode', 'obsidian'],
                    default=current_frontend,
                    style=custom_style,
                ).ask()
                if frontend:
                    set_config_value(root, frontend, 'frontend')
                    console.print(f'[bold green]✓ Frontend set to {frontend}.[/bold green]\n')
