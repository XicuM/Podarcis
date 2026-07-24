'''Questionary-based interactive TUI configuration runner.'''

from pathlib import Path
from tui.banner import display_project_banner
from tui.components import (
    SERVER_NAME_MAP, discover_components, get_enabled_mcp_servers,
    install_deps, set_mcp_server_status, set_skill_status
)
from tui.console import console
from tui.repos import (
    get_repo_url, load_repos_config, set_repo_protocol, set_repo_url, sync_repos
)

try:
    import questionary
    HAS_QUESTIONARY = True
except ImportError:
    HAS_QUESTIONARY = False


def interactive_config(root: Path) -> None:
    '''Run main interactive menu for configuring MCP servers, skills, and repositories.'''
    console.clear()
    display_project_banner(root)

    if not HAS_QUESTIONARY:
        install_deps(root, 'questionary', False, 'Installing questionary for interactive menus...')
        try:
            global questionary
            import questionary
            globals()['HAS_QUESTIONARY'] = True
        except ImportError:
            console.print('[red]Could not load questionary.[/red]')
            return

    custom_style = questionary.Style([
        ('qmark', 'fg:#29b8db bold'),
        ('question', 'bold white'),
        ('answer', 'fg:#29b8db bold'),
        ('pointer', 'fg:#29b8db bold'),
        ('highlighted', 'noinherit fg:white'),
        ('selected', 'noinherit fg:white'),
        ('separator', 'fg:yellow bold'),
        ('instruction', 'fg:#888888 italic'),
        ('choice-title', 'fg:white'),
        ('checkbox-checked', 'fg:#29b8db bold'),
        ('checkbox-unchecked', 'fg:#888888'),
        ('checkbox-selected', 'fg:#29b8db bold'),
    ])

    while True:
        mcp_servers, skills = discover_components(root)
        enabled_servers = get_enabled_mcp_servers(root)

        action = questionary.select(
            'What would you like to configure?',
            choices=[
                'Configure MCP Servers',
                'Configure Skills',
                'Configure Repositories',
                'Configure Optional Tool Engines',
                'Exit'
            ],
            style=custom_style
        ).ask()

        if not action or action == 'Exit':
            console.print('[bold #29b8db]Exiting configuration.[/bold #29b8db]')
            break

        if action == 'Configure MCP Servers':
            choices = [
                questionary.Choice(
                    title=k,
                    checked=(k in enabled_servers or SERVER_NAME_MAP.get(k) in enabled_servers)
                )
                for k in sorted(mcp_servers.keys())
            ]
            selected = questionary.checkbox('Select MCP Servers:', choices=choices, style=custom_style).ask()

            if selected is not None:
                selected_set = set(selected)
                for k, info in mcp_servers.items():
                    set_mcp_server_status(root, k, k in selected_set, info)
                console.print('[bold green]✓ MCP Server configurations updated successfully![/bold green]\n')

        elif action == 'Configure Skills':
            choices = [
                questionary.Choice(title=k, checked=skills[k]['enabled'])
                for k in sorted(skills.keys())
            ]
            selected = questionary.checkbox('Select Skills to enable:', choices=choices, style=custom_style).ask()

            if selected is not None:
                selected_set = set(selected)
                for k, info in skills.items():
                    set_skill_status(root, k, k in selected_set, info)
                console.print('[bold green]✓ Skill configurations updated successfully![/bold green]\n')

        elif action == 'Configure Optional Tool Engines':
            from tui.common import load_podarcis_config, set_engine_status
            import shutil

            pod_cfg = load_podarcis_config(root)
            qmd_enabled = bool(pod_cfg.get('engines', {}).get('qmd', False))
            qmd_bin = shutil.which('qmd')

            status_str = "Enabled" if qmd_enabled else "Disabled"
            bin_str = f"Available ({qmd_bin})" if qmd_bin else "Missing binary"

            engine_choice = questionary.confirm(
                f'Enable QMD Vector DB Engine? (Currently: {status_str}, Binary: {bin_str})',
                default=qmd_enabled,
                style=custom_style
            ).ask()

            if engine_choice is not None:
                set_engine_status(root, 'qmd', engine_choice)
                new_state = "Enabled" if engine_choice else "Disabled"
                console.print(f'[bold green]✓ QMD Vector DB Engine set to: {new_state} in podarcis.yaml[/bold green]\n')

        elif action == 'Configure Repositories':
            while True:
                repos_cfg = load_repos_config(root)
                proto = repos_cfg.get('protocol', 'ssh').upper()

                repo_action = questionary.select(
                    'Workspace Repository Configuration:',
                    choices=[
                        f'Switch Git Protocol (Current: {proto})',
                        'Modify Repository URLs',
                        'Sync / Clone Workspace Repositories',
                        'Back to Main Menu'
                    ],
                    style=custom_style
                ).ask()

                if not repo_action or repo_action == 'Back to Main Menu':
                    break

                if repo_action.startswith('Switch Git Protocol'):
                    new_proto = questionary.select(
                        'Select preferred Git clone protocol:',
                        choices=['SSH (git@...)', 'HTTPS (https://...)'],
                        style=custom_style
                    ).ask()
                    if new_proto:
                        p_val = 'ssh' if 'SSH' in new_proto else 'https'
                        set_repo_protocol(root, p_val, update_existing_remotes=True)
                        console.print(f'[bold green]✓ Preferred Git protocol updated to {p_val.upper()}![/bold green]\n')

                elif repo_action == 'Modify Repository URLs':
                    repos_dict = repos_cfg.get('repositories', {})
                    for r_name in sorted(repos_dict.keys()):
                        current_url = repos_dict[r_name]
                        new_url = questionary.text(
                            f'URL for workspace repo "{r_name}":',
                            default=current_url,
                            style=custom_style
                        ).ask()
                        if new_url and new_url != current_url:
                            set_repo_url(root, r_name, new_url)
                            console.print(f'[bold green]✓ Updated {r_name} URL to {new_url}[/bold green]')

                elif repo_action == 'Sync / Clone Workspace Repositories':
                    console.print('[#29b8db]Synchronizing workspace repositories...[/#29b8db]')
                    sync_repos(root, clone_missing=True, update_remotes=True)
                    console.print('[bold green]✓ Repositories synchronized successfully![/bold green]\n')
