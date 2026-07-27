#!/usr/bin/env python3
'''Podarcis CLI tool for agentic configuration management, testing, and lifecycle.'''

import argparse
import json
import os
import subprocess
import sys
from importlib.metadata import version, PackageNotFoundError
from pathlib import Path

BACKENDS = {'opencode': 'opencode', 'codex': 'codex', 'agy': 'agy', 'claude': 'claude', 'openclaw': 'openclaw', 'hermes': 'hermes', 'none': None}
FRONTENDS = {'vscode': 'code', 'obsidian': 'obsidian', 'none': None}

# Ensure root and .podarcis directories are in sys.path
root_dir = Path(__file__).resolve().parent.parent
podarcis_dir = Path(__file__).resolve().parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))
if str(podarcis_dir) not in sys.path:
    sys.path.insert(0, str(podarcis_dir))

from common import get_config_value, set_config_value
from console import console
from components import (
    discover_components,
    get_enabled_mcp_servers,
    set_mcp_server_status,
    set_skill_status,
    set_agent_status,
    sync_all_backends,
)

from repos import get_repo_names, get_repo_url, set_repo_url


def _get_python_bin() -> str:
    '''Get path to python binary inside virtualenv if available.'''
    venv_py = root_dir / '.venv' / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
    if venv_py.exists():
        return str(venv_py)
    return sys.executable


def _get_pytest_bin() -> str:
    '''Get path to pytest binary inside virtualenv if available.'''
    venv_pytest = root_dir / '.venv' / ('Scripts/pytest.exe' if sys.platform == 'win32' else 'bin/pytest')
    if venv_pytest.exists():
        return str(venv_pytest)
    return 'pytest'


def cmd_status(args: argparse.Namespace) -> int:
    '''List component and repository status.'''
    mcp_servers, skills, agents = discover_components(root_dir)
    enabled_mcp = get_enabled_mcp_servers(root_dir)

    status_data = {
        'mcp_servers': {},
        'skills': {},
        'agents': {},
        'repositories': {},
    }

    for key, info in sorted(mcp_servers.items()):
        is_on = key in enabled_mcp
        status_data['mcp_servers'][key] = {
            'enabled': is_on,
            'tokens': info.get('tokens', 0),
            'dir_name': info.get('dir_name', key),
        }

    for key, info in sorted(skills.items()):
        status_data['skills'][key] = {
            'enabled': info.get('enabled', False),
            'tokens': info.get('tokens', 0),
        }

    for key, info in sorted(agents.items()):
        status_data['agents'][key] = {
            'enabled': info.get('enabled', False),
            'tokens': info.get('tokens', 0),
        }

    for r_name in get_repo_names(root_dir):
        url = get_repo_url(root_dir, r_name)
        status_data['repositories'][r_name] = {
            'remote_url': url,
            'is_local_only': not bool(url),
        }

    if getattr(args, 'json', False):
        print(json.dumps(status_data, indent=2))
        return 0

    console.print('[bold #29b8db]Podarcis Configuration Status[/bold #29b8db]\n')

    console.print('[bold white]MCP Servers:[/bold white]')
    for k, v in status_data['mcp_servers'].items():
        st = '[green]enabled[/green]' if v['enabled'] else '[dim red]disabled[/dim red]'
        console.print(f'  • {k:<20} [{st}] ({v["tokens"]} tokens)')

    console.print('\n[bold white]Skills:[/bold white]')
    for k, v in status_data['skills'].items():
        st = '[green]enabled[/green]' if v['enabled'] else '[dim red]disabled[/dim red]'
        console.print(f'  • {k:<20} [{st}] ({v["tokens"]} tokens)')

    console.print('\n[bold white]Agents:[/bold white]')
    for k, v in status_data['agents'].items():
        st = '[green]enabled[/green]' if v['enabled'] else '[dim red]disabled[/dim red]'
        console.print(f'  • {k:<20} [{st}] ({v["tokens"]} tokens)')

    console.print('\n[bold white]Repositories:[/bold white]')
    for k, v in status_data['repositories'].items():
        url_str = v['remote_url'] if v['remote_url'] else 'local-only'
        console.print(f'  • {k:<20} {url_str}')

    return 0


def cmd_config_enable(args: argparse.Namespace) -> int:
    '''Enable a component (mcp, skill, agent).'''
    backend = get_config_value(root_dir, 'backend', default='none')
    if backend == 'none':
        console.print(
            '[bold yellow]Warning:[/bold yellow] No backend selected — MCP config changes will have no effect.\n'
            'Change it with: [bold]podarcis config backend <name>[/bold] or [bold]podarcis config interactive[/bold]'
        )
    ctype = args.type.lower()
    name = args.name
    mcp_servers, skills, agents = discover_components(root_dir)

    if ctype == 'mcp':
        if name not in mcp_servers:
            console.print(f'[bold red]Error:[/bold red] MCP server "{name}" not found. Available: {", ".join(mcp_servers.keys())}')
            return 1
        set_mcp_server_status(root_dir, name, True, mcp_servers[name])
        console.print(f'[bold green]✓ Enabled MCP server "{name}".[/bold green]')

    elif ctype == 'skill':
        if name not in skills:
            console.print(f'[bold red]Error:[/bold red] Skill "{name}" not found. Available: {", ".join(skills.keys())}')
            return 1
        set_skill_status(root_dir, name, True, skills[name])
        console.print(f'[bold green]✓ Enabled skill "{name}".[/bold green]')

    elif ctype == 'agent':
        if name not in agents:
            console.print(f'[bold red]Error:[/bold red] Agent "{name}" not found. Available: {", ".join(agents.keys())}')
            return 1
        set_agent_status(root_dir, name, True, agents[name])
        console.print(f'[bold green]✓ Enabled agent "{name}".[/bold green]')

    else:
        console.print(f'[bold red]Error:[/bold red] Unknown component type "{ctype}". Must be one of: mcp, skill, agent')
        return 1

    return 0


def cmd_config_disable(args: argparse.Namespace) -> int:
    '''Disable a component (mcp, skill, agent).'''
    backend = get_config_value(root_dir, 'backend', default='none')
    if backend == 'none':
        console.print(
            '[bold yellow]Warning:[/bold yellow] No backend selected — MCP config changes will have no effect.\n'
            'Change it with: [bold]podarcis config backend <name>[/bold] or [bold]podarcis config interactive[/bold]'
        )
    ctype = args.type.lower()
    name = args.name
    mcp_servers, skills, agents = discover_components(root_dir)

    if ctype == 'mcp':
        if name not in mcp_servers:
            console.print(f'[bold red]Error:[/bold red] MCP server "{name}" not found. Available: {", ".join(mcp_servers.keys())}')
            return 1
        set_mcp_server_status(root_dir, name, False, mcp_servers[name])
        console.print(f'[yellow]Disabled MCP server "{name}".[/yellow]')

    elif ctype == 'skill':
        if name not in skills:
            console.print(f'[bold red]Error:[/bold red] Skill "{name}" not found. Available: {", ".join(skills.keys())}')
            return 1
        set_skill_status(root_dir, name, False, skills[name])
        console.print(f'[yellow]Disabled skill "{name}".[/yellow]')

    elif ctype == 'agent':
        if name not in agents:
            console.print(f'[bold red]Error:[/bold red] Agent "{name}" not found. Available: {", ".join(agents.keys())}')
            return 1
        set_agent_status(root_dir, name, False, agents[name])
        console.print(f'[yellow]Disabled agent "{name}".[/yellow]')

    else:
        console.print(f'[bold red]Error:[/bold red] Unknown component type "{ctype}". Must be one of: mcp, skill, agent')
        return 1

    return 0


def cmd_config_repo(args: argparse.Namespace) -> int:
    '''Update repository remote configuration.'''
    repo_name = args.repo_name
    known_repos = get_repo_names(root_dir)
    if repo_name not in known_repos:
        console.print(f'[bold red]Error:[/bold red] Repository "{repo_name}" not found. Known repositories: {", ".join(known_repos)}')
        return 1

    if args.local:
        set_repo_url(root_dir, repo_name, '')
        console.print(f'[bold green]✓ Set {repo_name} to local-only.[/bold green]')
    elif args.url is not None:
        url_val = args.url.strip()
        set_repo_url(root_dir, repo_name, url_val)
        if url_val:
            console.print(f'[bold green]✓ Set remote for {repo_name} to {url_val}[/bold green]')
        else:
            console.print(f'[bold green]✓ Set {repo_name} to local-only.[/bold green]')
    else:
        current_url = get_repo_url(root_dir, repo_name)
        remote_label = current_url if current_url else 'local-only'
        console.print(f'Repository "{repo_name}": {remote_label}')

    return 0


# Claudian Obsidian plugin ID
_CLAUDIAN_PLUGIN_ID = 'realclaudian'

# Maps Podarcis backend name to the Claudian settingsProvider value.
# Backends absent from this map are unsupported: the plugin is disabled.
_BACKEND_TO_CLAUDIAN: dict[str, str] = {
    'claude': 'claude',
    'opencode': 'opencode',
    'codex': 'codex',
    # 'openclaw' and 'hermes' have no Claudian providerId → plugin is disabled
}


def _sync_claudian_plugin(backend: str) -> None:
    '''Sync the Claudian Obsidian plugin state to match the active Podarcis backend.

    Supported backends (claude / opencode / codex): enable the plugin and
    write the matching ``settingsProvider`` into its data.json.
    Unsupported backends (agy / openclaw / hermes): disable the plugin entirely.
    '''
    obsidian_dir = root_dir / '.obsidian'
    community_plugins_path = obsidian_dir / 'community-plugins.json'
    plugin_data_path = obsidian_dir / 'plugins' / _CLAUDIAN_PLUGIN_ID / 'data.json'

    provider = _BACKEND_TO_CLAUDIAN.get(backend)
    supported = provider is not None

    # --- community-plugins.json: add or remove the plugin entry ---
    plugins: list[str] = []
    if community_plugins_path.exists():
        try:
            import json as _json
            plugins = _json.loads(community_plugins_path.read_text(encoding='utf-8'))
            if not isinstance(plugins, list):
                plugins = []
        except Exception:
            plugins = []

    if supported and _CLAUDIAN_PLUGIN_ID not in plugins:
        plugins.append(_CLAUDIAN_PLUGIN_ID)
        community_plugins_path.write_text(
            __import__('json').dumps(plugins, indent=2) + '\n', encoding='utf-8'
        )
        console.print(f'[dim]Claudian: enabled in community-plugins.json[/dim]')
    elif not supported and _CLAUDIAN_PLUGIN_ID in plugins:
        plugins.remove(_CLAUDIAN_PLUGIN_ID)
        community_plugins_path.write_text(
            __import__('json').dumps(plugins, indent=2) + '\n', encoding='utf-8'
        )
        console.print(f'[dim]Claudian: disabled in community-plugins.json (backend "{backend}" not supported)[/dim]')

    # --- data.json: set settingsProvider when supported ---
    if not supported:
        return

    plugin_data_path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if plugin_data_path.exists():
        try:
            import json as _json
            data = _json.loads(plugin_data_path.read_text(encoding='utf-8'))
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}

    data['settingsProvider'] = provider
    plugin_data_path.write_text(
        __import__('json').dumps(data, indent=2) + '\n', encoding='utf-8'
    )
    console.print(f'[dim]Claudian: settingsProvider set to "{provider}"[/dim]')


def cmd_config_backend(args: argparse.Namespace) -> int:
    '''Set or show the backend name (opencode, codex, agy, claude, openclaw, hermes, none).'''
    backends = {'opencode', 'codex', 'agy', 'claude', 'openclaw', 'hermes', 'none'}
    name = args.backend_name.lower()
    if name not in backends:
        console.print(f'[bold red]Error:[/bold red] Unknown backend "{name}". Choose from: {", ".join(sorted(backends))}')
        return 1
    set_config_value(root_dir, name, 'backend')
    if name == 'none':
        console.print('[bold yellow]✓ Backend set to none.[/bold yellow] MCP configuration will be skipped.')
        return 0
    _sync_claudian_plugin(name)
    # Regenerate MCP config for the newly active backend
    from backends import generate_for_backend
    paths = generate_for_backend(root_dir, name)
    if paths:
        console.print(f'[dim]Regenerated MCP config for {name}: {[str(p) for p in paths]}[/dim]')
    console.print(f'[bold green]✓ Backend set to {name}.[/bold green]')
    return 0


def cmd_config_sync(args: argparse.Namespace) -> int:
    '''Regenerate MCP config for all supported backends from canonical mcp_config.json.'''
    results = sync_all_backends(root_dir)
    console.print('[bold #29b8db]Synced MCP config to all backends:[/bold #29b8db]\n')
    for backend, paths in results.items():
        if paths:
            console.print(f'  [green]✓[/green] {backend:<12} → {[str(p.name) for p in paths]}')
        else:
            console.print(f'  [dim]—[/dim] {backend:<12} (no files written)')
    return 0


def cmd_config_frontend(args: argparse.Namespace) -> int:
    '''Set or show the frontend name.'''
    name = args.frontend_name.lower()
    if name not in FRONTENDS:
        console.print(f'[bold red]Error:[/bold red] Unknown frontend "{name}". Choose from: {", ".join(sorted(FRONTENDS))}')
        return 1
    set_config_value(root_dir, name, 'frontend')
    if name == 'none':
        console.print('[bold yellow]✓ Frontend set to none.[/bold yellow] Opening a frontend will be skipped.')
    else:
        console.print(f'[bold green]✓ Frontend set to {name}.[/bold green]')
    return 0


def cmd_backend(args: argparse.Namespace) -> int:
    '''Open the configured backend.'''
    backend = get_config_value(root_dir, 'backend', default='none')
    if backend == 'none':
        console.print(
            '[bold yellow]Warning:[/bold yellow] No backend selected.\n'
            'Change it with: [bold]podarcis config backend <name>[/bold] or [bold]podarcis config interactive[/bold]'
        )
        return 1
    return cmd_open_tool('backend')


def cmd_frontend(args: argparse.Namespace) -> int:
    '''Open the configured frontend.'''
    frontend = get_config_value(root_dir, 'frontend', default='none')
    if frontend == 'none':
        console.print(
            '[bold yellow]Warning:[/bold yellow] No frontend selected.\n'
            'Change it with: [bold]podarcis config frontend <name>[/bold] or [bold]podarcis config interactive[/bold]'
        )
        return 1
    from banner import display_project_banner
    display_project_banner(root_dir)
    return cmd_open_tool('frontend')


def cmd_interactive(args: argparse.Namespace) -> int:
    '''Launch TUI interactive menu.'''
    from interactive import interactive_config
    interactive_config(root_dir)
    return 0


def cmd_reinstall(args: argparse.Namespace) -> int:
    '''Re-run bootstrap installer.'''
    install_script = root_dir / '.podarcis' / 'install.py'
    py_bin = sys.executable
    return subprocess.run([py_bin, str(install_script)] + args.remaining_args).returncode


def cmd_uninstall(args: argparse.Namespace) -> int:
    '''Remove global symlink, virtualenv, and build artefacts.'''
    uninstall_script = root_dir / '.podarcis' / 'uninstall.py'
    py_bin = sys.executable
    extra: list[str] = []
    if getattr(args, 'yes', False):
        extra += ['--yes']
    if getattr(args, 'dry_run', False):
        extra += ['--dry-run']
    if getattr(args, 'purge', False):
        extra += ['--purge']
    return subprocess.run([py_bin, str(uninstall_script)] + extra).returncode


def cmd_test(args: argparse.Namespace) -> int:
    '''Run pytest test suite.'''
    pytest_bin = _get_pytest_bin()
    cmd = [pytest_bin] + args.remaining_args
    return subprocess.run(cmd).returncode


def cmd_lint(args: argparse.Namespace) -> int:
    '''Run markdown link checker.'''
    check_links = root_dir / '.agents' / 'mcp' / 'wiki' / 'check_links.py'
    py_bin = _get_python_bin()
    targets = args.remaining_args if args.remaining_args else [str(root_dir)]
    return subprocess.run([py_bin, str(check_links)] + targets).returncode


def cmd_open_tool(tool_type: str) -> int:
    '''Open the configured tool (backend or frontend) at the current directory.'''
    valid = BACKENDS if tool_type == 'backend' else FRONTENDS
    name = get_config_value(root_dir, tool_type, default='vscode' if tool_type == 'frontend' else 'opencode')
    command = valid.get(name.lower(), name)
    cwd = str(root_dir)
    try:
        if tool_type == 'backend':
            # CLI tools: exec replacing current process to take over terminal
            os.execvp(command, [command, cwd])
        else:
            # GUI tools: detach in background
            subprocess.Popen([command, cwd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            console.print(f'[bold green]✓ Opened {name} at {cwd}[/bold green]')
    except FileNotFoundError:
        console.print(f'[bold red]Error:[/bold red] "{command}" not found on PATH.')
        return 1
    return 0


def main() -> None:
    '''Main entry point for podarcis CLI.'''
    parser = argparse.ArgumentParser(
        prog='podarcis',
        description='Podarcis OKF v0.2 Research Agent engine & CLI configuration tool',
    )
    try:
        _version = version('podarcis')
    except PackageNotFoundError:
        # Fallback for dev/editable installs: read directly from pyproject.toml
        _pyproject = Path(__file__).resolve().parent.parent / 'pyproject.toml'
        try:
            import tomllib  # Python 3.11+
        except ImportError:
            import tomli as tomllib  # type: ignore[no-redef]
        with open(_pyproject, 'rb') as _f:
            _version = tomllib.load(_f)['project']['version']
    parser.add_argument('-v', '--version', action='version', version=f'podarcis {_version}')
    parser.add_argument('-i', '--interactive', action='store_true', help='Launch interactive TUI menu')

    subparsers = parser.add_subparsers(dest='subcommand', title='Subcommands', help='Action to perform')

    # status
    status_parser = subparsers.add_parser('status', help='Display status of MCP servers, skills, agents, and repos')
    status_parser.add_argument('--json', action='store_true', help='Output status in JSON format')

    # config
    config_parser = subparsers.add_parser('config', help='Configure components and repositories')
    config_sub = config_parser.add_subparsers(dest='config_action', help='Config action')

    # config list
    cfg_list = config_sub.add_parser('list', help='List status of components and repositories')
    cfg_list.add_argument('--json', action='store_true', help='Output status in JSON format')

    # config enable
    cfg_enable = config_sub.add_parser('enable', help='Enable a component (mcp|skill|agent)')
    cfg_enable.add_argument('type', choices=['mcp', 'skill', 'agent'], help='Component type')
    cfg_enable.add_argument('name', help='Component name')

    # config disable
    cfg_disable = config_sub.add_parser('disable', help='Disable a component (mcp|skill|agent)')
    cfg_disable.add_argument('type', choices=['mcp', 'skill', 'agent'], help='Component type')
    cfg_disable.add_argument('name', help='Component name')

    # config repo
    cfg_repo = config_sub.add_parser('repo', help='Configure repository Git remotes')
    cfg_repo.add_argument('repo_name', help='Repository name (wiki or workspace)')
    cfg_repo.add_argument('--url', help='Remote Git URL')
    cfg_repo.add_argument('--local', action='store_true', help='Set repository to local-only (no remote)')

    # config backend
    cfg_backend = config_sub.add_parser('backend', help='Set the agent backend (opencode, codex, agy, claude, none, …)')
    cfg_backend.add_argument('backend_name', choices=list(BACKENDS), help='Backend name')

    # config frontend
    cfg_frontend = config_sub.add_parser('frontend', help='Set the frontend tool (vscode, obsidian, none)')
    cfg_frontend.add_argument('frontend_name', choices=list(FRONTENDS), metavar='{vscode,obsidian,none}', help='Frontend name')

    # config interactive
    config_sub.add_parser('interactive', help='Launch interactive TUI menu')

    # config sync
    config_sub.add_parser('sync', help='Regenerate MCP config for all backends')

    # backend
    subparsers.add_parser('backend', help='Open the configured backend tool')

    # frontend
    subparsers.add_parser('frontend', help='Open the configured frontend tool')

    # reinstall
    reinstall_parser = subparsers.add_parser('reinstall', help='Re-run bootstrap installer')
    reinstall_parser.add_argument('remaining_args', nargs=argparse.REMAINDER)

    # uninstall
    uninstall_parser = subparsers.add_parser(
        'uninstall',
        help='Remove global symlink, virtualenv, and build artefacts',
    )
    uninstall_parser.add_argument('-y', '--yes', action='store_true', help='Skip all confirmations')
    uninstall_parser.add_argument('--dry-run', action='store_true', dest='dry_run', help='Preview without removing anything')
    uninstall_parser.add_argument('--purge', action='store_true', help='Also remove .podarcis/config.yaml')

    # test
    test_parser = subparsers.add_parser('test', help='Run pytest suite')
    test_parser.add_argument('remaining_args', nargs=argparse.REMAINDER)

    # lint
    lint_parser = subparsers.add_parser('lint', help='Run link integrity check')
    lint_parser.add_argument('remaining_args', nargs=argparse.REMAINDER)

    args = parser.parse_args()

    if args.interactive:
        sys.exit(cmd_interactive(args))

    if args.subcommand == 'status':
        sys.exit(cmd_status(args))
    elif args.subcommand == 'config':
        if args.config_action == 'list':
            sys.exit(cmd_status(args))
        elif args.config_action == 'enable':
            sys.exit(cmd_config_enable(args))
        elif args.config_action == 'disable':
            sys.exit(cmd_config_disable(args))
        elif args.config_action == 'repo':
            sys.exit(cmd_config_repo(args))
        elif args.config_action == 'backend':
            sys.exit(cmd_config_backend(args))
        elif args.config_action == 'frontend':
            sys.exit(cmd_config_frontend(args))
        elif args.config_action == 'interactive':
            sys.exit(cmd_interactive(args))
        elif args.config_action == 'sync':
            sys.exit(cmd_config_sync(args))
        else:
            if sys.stdin.isatty():
                sys.exit(cmd_interactive(args))
            else:
                config_parser.print_help()
                sys.exit(1)

    elif args.subcommand == 'backend':
        sys.exit(cmd_backend(args))
    elif args.subcommand == 'frontend':
        sys.exit(cmd_frontend(args))
    elif args.subcommand == 'reinstall':
        sys.exit(cmd_reinstall(args))
    elif args.subcommand == 'uninstall':
        sys.exit(cmd_uninstall(args))
    elif args.subcommand == 'test':
        sys.exit(cmd_test(args))
    elif args.subcommand == 'lint':
        sys.exit(cmd_lint(args))
    else:
        sys.exit(cmd_frontend(args))


if __name__ == '__main__':
    main()
