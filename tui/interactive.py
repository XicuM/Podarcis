'''Questionary-based interactive TUI configuration runner.'''

from pathlib import Path
from tui.banner import display_project_banner
from tui.components import (
    SERVER_NAME_MAP, discover_components, get_enabled_mcp_servers,
    install_deps, set_mcp_server_status, set_skill_status,
)
from tui.console import console
from tui.repos import load_repos_config, set_repo_url, sync_repos

try:
    import questionary
    HAS_QUESTIONARY = True
except ImportError:
    HAS_QUESTIONARY = False


def interactive_config(root: Path) -> None:
    '''Run main interactive menu for configuring MCP servers, skills, and
    repositories.'''
    console.clear()
    display_project_banner(root)

    if not HAS_QUESTIONARY:
        install_deps(
            root, 'questionary', False,
            'Installing questionary for interactive menus...')
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
                'MCP Servers', 'Skills', 'Repositories', 'Optional Tool Engines', 'Exit',
            ],
            style=custom_style,
        ).ask()

        match action:
            case 'Exit' | None:
                console.print('[bold #29b8db]Exiting config.[/bold #29b8db]')
                break

            case 'MCP Servers':
                choices = [
                    questionary.Choice(
                        title=k,
                        checked=(
                            k in enabled_servers
                            or SERVER_NAME_MAP.get(k) in enabled_servers
                        ),
                    )
                    for k in sorted(mcp_servers)
                ]
                selected = questionary.checkbox(
                    'Select MCP Servers:',
                    choices=choices, style=custom_style,
                ).ask()
                if selected is not None:
                    s = set(selected)
                    for k, info in mcp_servers.items():
                        set_mcp_server_status(root, k, k in s, info)
                    console.print(
                        '[bold green]✓ MCP server configs updated.[/bold green]\n')

            case 'Skills':
                choices = [
                    questionary.Choice(title=k, checked=skills[k]['enabled'])
                    for k in sorted(skills)
                ]
                selected = questionary.checkbox(
                    'Select Skills to enable:',
                    choices=choices, style=custom_style,
                ).ask()
                if selected is not None:
                    s = set(selected)
                    for k, info in skills.items():
                        set_skill_status(root, k, k in s, info)
                    console.print(
                        '[bold green]✓ Skill configs updated.[/bold green]\n')

            case 'Optional Tool Engines':
                from tui.common import load_yaml, set_engine_status
                import shutil

                pod_cfg = load_yaml(root/'podarcis.yaml')
                qmd_enabled = bool(pod_cfg.get('engines', {}).get('qmd', False))
                qmd_bin = shutil.which('qmd')

                bin_str = f' ({qmd_bin})' if qmd_bin else ''
                choice = questionary.select(
                    f'Enable QMD Vector DB Engine? '
                    f'(Currently: {"Enabled" if qmd_enabled else "Disabled"}, '
                    f'Binary: {"Available" if qmd_bin else "Missing"}{bin_str})',
                    choices=['no', 'yes'],
                    default='yes' if qmd_enabled else 'no',
                    style=custom_style,
                ).ask()

                if choice is not None:
                    enable = choice == 'yes'
                    set_engine_status(root, 'qmd', enable)
                    console.print(
                        f'[bold green]✓ QMD set to: '
                        f'{"Enabled" if enable else "Disabled"} in '
                        f'podarcis.yaml[/bold green]\n')

            case 'Repositories':
                while True:
                    repos_cfg = load_repos_config(root)

                    sub_action = questionary.select(
                        'Workspace Repository Configuration:',
                        choices=[
                            'Modify Repository URLs',
                            'Sync / Clone Workspace Repositories',
                            'Back to Main Menu',
                        ],
                        style=custom_style,
                    ).ask()

                    match sub_action:
                        case 'Back to Main Menu' | None:
                            break

                        case 'Modify Repository URLs':
                            repos_dict = repos_cfg.get('repositories', {})
                            for r_name in sorted(repos_dict):
                                current_url = repos_dict[r_name]
                                new_url = questionary.text(
                                    f'URL for workspace repo "{r_name}":',
                                    default=current_url,
                                    style=custom_style,
                                ).ask()
                                if new_url and new_url != current_url:
                                    set_repo_url(root, r_name, new_url)
                                    console.print(
                                        f'[bold green]✓ Updated {r_name} URL '
                                        f'to {new_url}[/bold green]')

                        case 'Sync / Clone Workspace Repositories':
                            console.print(
                                '[#29b8db]Syncing workspace repos...'
                                '[/#29b8db]')
                            sync_repos(
                                root, clone_missing=True, update_remotes=True)
                            console.print(
                                '[bold green]✓ Repos synced.[/bold green]\n')
