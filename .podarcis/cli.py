#!/usr/bin/env python3
'''Podarcis CLI tool for agentic configuration management, testing, and lifecycle.'''

import argparse
import json
import os
import shutil
import subprocess
import sys
from importlib.metadata import version, PackageNotFoundError
from pathlib import Path

BACKENDS = {'opencode': 'opencode', 'codex': 'codex', 'agy': 'agy', 'claude': 'claude', 'openclaw': 'openclaw', 'hermes': 'hermes', 'none': None}
FRONTENDS = {'vscode': 'code', 'code-server': 'code-server', 'vscode-web': 'code-server', 'obsidian': 'obsidian', 'none': None}

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
    '''List component, job, and repository status.'''
    mcp_servers, skills, agents = discover_components(root_dir)
    enabled_mcp = get_enabled_mcp_servers(root_dir)
    from jobs import discover_jobs
    jobs = discover_jobs(root_dir)

    status_data = {
        'mcp_servers': {},
        'skills': {},
        'agents': {},
        'jobs': {},
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

    for key, info in sorted(jobs.items()):
        status_data['jobs'][key] = {
            'enabled': info.get('enabled', False),
            'schedule': info.get('schedule', ''),
            'description': info.get('description', ''),
            'last_run': info.get('last_run', ''),
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

    console.print('\n[bold white]Jobs:[/bold white]')
    for k, v in status_data['jobs'].items():
        st = '[green]enabled[/green]' if v['enabled'] else '[dim red]disabled[/dim red]'
        sched = f'[{v["schedule"]}]' if v["schedule"] else ''
        console.print(f'  • {k:<20} [{st}] {sched}')

    console.print('\n[bold white]Repositories:[/bold white]')
    for k, v in status_data['repositories'].items():
        url_str = v['remote_url'] if v['remote_url'] else 'local-only'
        console.print(f'  • [bold #29b8db]{k:<20}[/bold #29b8db] {url_str}')

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

    if supported:
        obsidian_dir.mkdir(parents=True, exist_ok=True)

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
        path_list = paths if isinstance(paths, list) else [paths]
        console.print(f'[dim]Regenerated MCP config for {name}: {[str(p) for p in path_list]}[/dim]')
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


def _ensure_vscode_config(root: Path) -> None:
    '''Ensure .vscode and code-server user configuration directories are initialized from templates if missing.'''
    template_dir = root / '.podarcis' / 'templates' / 'vscode'
    target_dir = root / '.vscode'
    if template_dir.exists():
        target_dir.mkdir(parents=True, exist_ok=True)
        for item in template_dir.iterdir():
            target_file = target_dir / item.name
            if not target_file.exists():
                shutil.copy2(item, target_file)
                console.print(f'[dim]Initialized .vscode/{item.name} from template[/dim]')

    # Also seed user settings for code-server / VSCode server if absent
    code_user_dir = root / 'config' / 'code-config' / 'Code' / 'User'
    code_user_dir.mkdir(parents=True, exist_ok=True)
    code_user_settings = code_user_dir / 'settings.json'
    template_settings = template_dir / 'settings.json'
    if template_settings.exists() and not code_user_settings.exists():
        shutil.copy2(template_settings, code_user_settings)
        console.print('[dim]Initialized config/code-config/Code/User/settings.json from template[/dim]')


def cmd_config_frontend(args: argparse.Namespace) -> int:
    '''Set or show the frontend name.'''
    name = args.frontend_name.lower()
    if name not in FRONTENDS:
        console.print(f'[bold red]Error:[/bold red] Unknown frontend "{name}". Choose from: {", ".join(sorted(FRONTENDS))}')
        return 1
    set_config_value(root_dir, name, 'frontend')
    if name in ('vscode', 'code-server'):
        _ensure_vscode_config(root_dir)
    elif name == 'obsidian':
        backend = get_config_value(root_dir, 'backend', default='none')
        cfg_plugins = getattr(args, 'configure_plugins', None)
        if cfg_plugins is None and sys.stdin.isatty():
            try:
                import questionary
                cfg_plugins = questionary.confirm(
                    f'Configure Obsidian plugins for agentic knowledge base (Claudian for backend "{backend}")?',
                    default=True,
                ).ask()
            except Exception:
                cfg_plugins = False

        if cfg_plugins:
            _sync_claudian_plugin(backend)
            console.print(f'[bold green]✓ Frontend set to obsidian with Claudian plugin configured.[/bold green]')
        else:
            console.print(f'[bold green]✓ Frontend set to obsidian.[/bold green] [dim]Skipped Obsidian plugin configuration.[/dim]')
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


def cmd_diagnose(args: argparse.Namespace) -> int:
    '''Display platform pain points and current logged issues.'''
    diag_script = root_dir / '.agents' / 'skills' / 'self-improvement' / 'scripts' / 'diagnose_session.py'
    if not diag_script.exists():
        console.print('[bold red]Error:[/bold red] diagnose_session.py script not found.')
        return 1

    import importlib.util
    spec = importlib.util.spec_from_file_location('diagnose_session', diag_script)
    if spec is None or spec.loader is None:
        console.print('[bold red]Error:[/bold red] Could not load diagnose_session module.')
        return 1
    diag_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(diag_mod)

    if getattr(args, 'clear', False):
        cleared = diag_mod.clear_issues(base_dir=root_dir)
        console.print(f'[bold green]Cleared {cleared} logged platform issue(s).[/bold green]')
        return 0

    log_sess = getattr(args, 'log_session', None)
    if log_sess:
        p = Path(log_sess)
        points = diag_mod.parse_transcript(p)
        diag_mod.log_pain_points(points, base_dir=root_dir)
        console.print(f'[bold green]Parsed {p.name} and logged {len(points)} pain point(s).[/bold green]')

    issues = diag_mod.get_active_issues(base_dir=root_dir)
    if getattr(args, 'json', False):
        print(json.dumps(issues, indent=2))
        return 0

    if not issues:
        console.print('[bold green]No active platform pain points logged in .podarcis/diagnostics/[/bold green]')
    else:
        console.print(f'[bold #29b8db]Current Platform Pain Points ({len(issues)} active):[/bold #29b8db]\n')
        for idx, issue in enumerate(issues, 1):
            sev = issue.get('severity', 'medium')
            cat = issue.get('category', 'issue')
            summ = issue.get('summary', '')
            ts = issue.get('timestamp', '')
            color = 'red' if sev == 'high' else 'yellow'
            console.print(f'{idx}. [{color}][{sev.upper()}][/{color}] [bold white][{cat}][/bold white] {summ} [dim]({ts})[/dim]')
        console.print()
    return 0



def _get_server_pid_file() -> Path:
    return root_dir / '.podarcis' / 'podarcis-server.pid'


def _get_server_log_file() -> Path:
    log_dir = root_dir / '.podarcis' / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / 'server.log'


def _is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def cmd_server(args: argparse.Namespace) -> int:
    '''Launch or manage Podarcis Multi-User dynamic router and admin server.'''
    action = getattr(args, 'server_action', None)
    port = getattr(args, 'port', 8080)
    pid_file = _get_server_pid_file()
    log_file = _get_server_log_file()

    if action == 'stop' or getattr(args, 'stop', False):
        action_name = 'stop'
    elif action == 'status' or getattr(args, 'status', False):
        action_name = 'status'
    elif action == 'install' or getattr(args, 'install', False):
        action_name = 'install'
    elif action == 'uninstall' or getattr(args, 'uninstall', False):
        action_name = 'uninstall'
    elif action == 'start' or getattr(args, 'daemon', False):
        action_name = 'start'
    else:
        action_name = 'run'

    if action_name == 'start':
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                if _is_pid_running(pid):
                    console.print(f'[yellow]Podarcis Server daemon is already running (PID {pid}) at http://localhost:{port}[/yellow]')
                    return 0
            except ValueError:
                pass

        py_bin = _get_python_bin()
        podarcis_cli = root_dir / '.podarcis' / 'cli.py'
        log_f = open(log_file, 'a', encoding='utf-8')
        proc = subprocess.Popen(
            [py_bin, str(podarcis_cli), 'server', '--port', str(port)],
            stdout=log_f,
            stderr=log_f,
            cwd=str(root_dir),
            start_new_session=True,
        )
        pid_file.write_text(str(proc.pid), encoding='utf-8')
        console.print(f'[bold green]✓ Started Podarcis Server daemon (PID {proc.pid})[/bold green]')
        console.print(f'[bold green]✓ Portal: http://localhost:{port}/login[/bold green]')
        console.print(f'[bold green]✓ Logs: {log_file}[/bold green]')
        return 0

    elif action_name == 'stop':
        stopped = False
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                if _is_pid_running(pid):
                    import signal
                    os.kill(pid, signal.SIGTERM)
                    stopped = True
            except Exception:
                pass
            try:
                pid_file.unlink()
            except OSError:
                pass

        try:
            res = subprocess.run(['systemctl', '--user', 'is-active', 'podarcis-server'], capture_output=True, text=True)
            if res.stdout.strip() == 'active':
                subprocess.run(['systemctl', '--user', 'stop', 'podarcis-server'], capture_output=True)
                stopped = True
        except Exception:
            pass

        if stopped:
            console.print('[yellow]✓ Stopped Podarcis Server.[/yellow]')
        else:
            console.print('[dim]Podarcis Server is not running.[/dim]')
        return 0

    elif action_name == 'status':
        is_running = False
        pid_info = None
        systemd_info = None

        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                if _is_pid_running(pid):
                    is_running = True
                    pid_info = pid
            except ValueError:
                pass

        try:
            res = subprocess.run(['systemctl', '--user', 'is-active', 'podarcis-server'], capture_output=True, text=True)
            systemd_info = res.stdout.strip()
            if systemd_info == 'active':
                is_running = True
        except Exception:
            systemd_info = 'n/a'

        if getattr(args, 'json', False):
            print(json.dumps({
                'running': is_running,
                'pid': pid_info,
                'systemd_status': systemd_info,
                'port': port,
                'url': f'http://localhost:{port}/login',
            }, indent=2))
            return 0

        console.print('[bold #29b8db]Podarcis Server Status:[/bold #29b8db]')
        console.print(f'  • Active: {"[bold green]Yes[/bold green]" if is_running else "[yellow]No[/yellow]"}')
        if pid_info:
            console.print(f'  • Process PID: {pid_info}')
        if systemd_info and systemd_info != 'n/a':
            console.print(f'  • Systemd Service: {systemd_info}')
        console.print(f'  • Configured Port: {port}')
        console.print(f'  • Access URL: http://localhost:{port}/login')
        return 0

    elif action_name == 'install':
        py_bin = _get_python_bin()
        cli_path = root_dir / '.podarcis' / 'cli.py'
        user_service_dir = Path.home() / '.config' / 'systemd' / 'user'
        user_service_dir.mkdir(parents=True, exist_ok=True)
        service_file = user_service_dir / 'podarcis-server.service'

        service_content = f'''[Unit]
Description=Podarcis Multi-User Research Engine & Server
After=network.target

[Service]
Type=simple
WorkingDirectory={root_dir}
ExecStart={py_bin} {cli_path} server --port {port}
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
'''
        service_file.write_text(service_content, encoding='utf-8')

        try:
            subprocess.run(['systemctl', '--user', 'daemon-reload'], check=False, capture_output=True)
            res = subprocess.run(['systemctl', '--user', 'enable', '--now', 'podarcis-server.service'], check=False, capture_output=True, text=True)
            if res.returncode == 0:
                console.print(f'[bold green]✓ Installed & enabled systemd boot service at {service_file}[/bold green]')
            else:
                console.print(f'[bold green]✓ Created systemd user unit at {service_file}[/bold green]')
                if res.stderr.strip():
                    console.print(f'[dim]Note: {res.stderr.strip()}[/dim]')

            subprocess.run(['loginctl', 'enable-linger'], check=False, capture_output=True)
        except Exception as e:
            console.print(f'[bold green]✓ Created systemd unit at {service_file}[/bold green] ({e})')

        return 0

    elif action_name == 'uninstall':
        service_file = Path.home() / '.config' / 'systemd' / 'user' / 'podarcis-server.service'
        try:
            subprocess.run(['systemctl', '--user', 'disable', '--now', 'podarcis-server.service'], check=False, capture_output=True)
        except Exception:
            pass

        if service_file.exists():
            try:
                service_file.unlink()
                console.print(f'[yellow]✓ Removed systemd user unit {service_file}[/yellow]')
            except OSError as e:
                console.print(f'[bold red]Error removing service file:[/bold red] {e}')
                return 1
        else:
            console.print('[dim]Systemd unit podarcis-server.service was not installed.[/dim]')

        try:
            subprocess.run(['systemctl', '--user', 'daemon-reload'], check=False, capture_output=True)
        except Exception:
            pass

        return 0

    else:
        # Default foreground execution
        console.print(f'[bold #29b8db]Starting Podarcis Multi-User Server on port {port}...[/bold #29b8db]')
        console.print(f'[bold green]✓ Login Portal live at http://localhost:{port}/login[/bold green]')
        console.print(f'[bold green]✓ Admin Dashboard live at http://localhost:{port}/admin[/bold green]')
        from podarcis.server.app import run_server
        run_server(port=port)
        return 0



def cmd_user(args: argparse.Namespace) -> int:
    '''Manage per-user containers and workspaces.'''
    from podarcis.server.user_manager import UserManager
    mgr = UserManager(root_dir)
    action = getattr(args, 'user_action', 'list')

    if action == 'list':
        users = mgr.get_users_registry()
        console.print('[bold #29b8db]Podarcis Multi-User Registry:[/bold #29b8db]\n')
        for username, info in users.items():
            c_info = mgr.get_container_for_user(username)
            st = c_info.get('status') if c_info else 'Stopped'
            port = c_info.get('port') if c_info else 'Auto'
            console.print(f'  • {username:<15} [{info.get("role", "user")}] — Status: {st} (Port: {port})')
        return 0

    elif action == 'create':
        username = args.username.lower()
        role = getattr(args, 'role', 'user')
        password = getattr(args, 'password', None)
        try:
            mgr.create_user(username, role, password=password)
            mgr.start_user_container(username)
            console.print(f'[bold green]✓ Provisioned user "{username}" container.[/bold green]')
        except Exception as e:
            console.print(f'[bold red]Error:[/bold red] {e}')
            return 1
        return 0

    elif action == 'password':
        username = args.username.lower()
        password = args.password
        try:
            mgr.set_user_password(username, password)
            console.print(f'[bold green]✓ Updated password for user "{username}".[/bold green]')
        except Exception as e:
            console.print(f'[bold red]Error:[/bold red] {e}')
            return 1
        return 0

    elif action == 'start':
        username = args.username.lower()
        info = mgr.start_user_container(username)
        console.print(f'[bold green]✓ Started container for user "{username}" (Port: {info.get("port")}).[/bold green]')
        return 0

    elif action == 'stop':
        username = args.username.lower()
        mgr.stop_user_container(username)
        console.print(f'[yellow]Stopped container for user "{username}".[/yellow]')
        return 0

    elif action == 'delete':
        username = args.username.lower()
        try:
            mgr.delete_user(username)
            console.print(f'[bold green]✓ Deleted user "{username}".[/bold green]')
        except Exception as e:
            console.print(f'[bold red]Error:[/bold red] {e}')
            return 1
        return 0

    return 0



def cmd_job(args: argparse.Namespace) -> int:
    '''Manage and execute modular Podarcis jobs (.agents/jobs/*.yaml).'''
    from jobs import discover_jobs, set_job_status, run_job
    action = getattr(args, 'job_action', None) or 'list'
    name = getattr(args, 'name', None)

    if action == 'list':
        discovered = discover_jobs(root_dir)
        if getattr(args, 'json', False):
            print(json.dumps(discovered, indent=2, default=str))
            return 0
        console.print('[bold #29b8db]Registered Jobs (.agents/jobs/*.yaml):[/bold #29b8db]\n')
        if not discovered:
            console.print('[dim]No jobs found in .agents/jobs/[/dim]')
            return 0
        for k, v in discovered.items():
            st = '[green]enabled[/green]' if v['enabled'] else '[dim red]disabled[/dim red]'
            sched = v['schedule']
            last = f' [dim](last run: {v["last_run"]})[/dim]' if v['last_run'] else ''
            console.print(f'  • {k:<20} [{st}] [{sched}]{last}\n    [dim]{v["description"]}[/dim]\n')
        return 0

    if action == 'enable':
        if not name:
            console.print('[bold red]Error:[/bold red] Specify job name to enable.')
            return 1
        ok, msg = set_job_status(root_dir, name, True)
        if ok:
            console.print(f'[bold green]✓ {msg}[/bold green]')
            return 0
        console.print(f'[bold red]Error:[/bold red] {msg}')
        return 1

    if action == 'disable':
        if not name:
            console.print('[bold red]Error:[/bold red] Specify job name to disable.')
            return 1
        ok, msg = set_job_status(root_dir, name, False)
        if ok:
            console.print(f'[yellow]✓ {msg}[/yellow]')
            return 0
        console.print(f'[bold red]Error:[/bold red] {msg}')
        return 1

    if action == 'run':
        if not name:
            console.print('[bold red]Error:[/bold red] Specify job name to run.')
            return 1
        res = run_job(root_dir, name, dry_run=getattr(args, 'dry_run', False))
        return 0 if res.get('status') != 'error' else 1

    return 0


def cmd_open_tool(tool_type: str) -> int:
    '''Open the configured tool (backend or frontend) at the current directory.'''
    valid = BACKENDS if tool_type == 'backend' else FRONTENDS
    name = get_config_value(root_dir, tool_type, default='vscode' if tool_type == 'frontend' else 'opencode')
    command = valid.get(name.lower(), name)
    cwd = str(root_dir)

    if tool_type == 'frontend' and name.lower() in ('vscode', 'vscode-web'):
        _ensure_vscode_config(root_dir)

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
    status_parser = subparsers.add_parser('status', help='Display status of MCP servers, skills, agents, jobs, and repos')
    status_parser.add_argument('--json', action='store_true', help='Output status in JSON format')

    # job / cron
    for job_cmd in ('job', 'cron'):
        j_parser = subparsers.add_parser(job_cmd, help='Manage and execute modular scheduled jobs (.agents/jobs/*.yaml)')
        j_sub = j_parser.add_subparsers(dest='job_action', help='Job action')

        jl = j_sub.add_parser('list', help='List discovered jobs, schedules, and status')
        jl.add_argument('--json', action='store_true', help='Output status in JSON format')

        jr = j_sub.add_parser('run', help='Execute job immediately by name')
        jr.add_argument('name', nargs='?', help='Job name (e.g. gdrive_sync, audit_wiki)')
        jr.add_argument('--dry-run', action='store_true', help='Preview execution without side effects')

        je = j_sub.add_parser('enable', help='Enable job and install its schedule in crontab')
        je.add_argument('name', help='Job name')

        jd = j_sub.add_parser('disable', help='Disable job and remove its schedule from crontab')
        jd.add_argument('name', help='Job name')

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
    cfg_frontend.add_argument('frontend_name', choices=list(FRONTENDS), metavar='{vscode,obsidian,code-server,none}', help='Frontend name')
    cfg_frontend.add_argument('--configure-plugins', action='store_true', dest='configure_plugins', default=None, help='Configure Obsidian plugins (Claudian)')
    cfg_frontend.add_argument('--no-plugins', action='store_false', dest='configure_plugins', help='Skip Obsidian plugin configuration')

    # config interactive
    config_sub.add_parser('interactive', help='Launch interactive TUI menu')

    # config sync
    config_sub.add_parser('sync', help='Regenerate MCP config for all backends')

    # backend
    subparsers.add_parser('backend', help='Open the configured backend tool')

    # frontend
    subparsers.add_parser('frontend', help='Open the configured frontend tool')

    # server
    server_parser = subparsers.add_parser('server', help='Start or manage Podarcis multi-user dynamic router server')
    server_parser.add_argument('server_action', nargs='?', choices=['start', 'stop', 'status', 'install', 'uninstall'], help='Server action (start|stop|status|install|uninstall)')
    server_parser.add_argument('--port', type=int, default=8080, help='Port to bind (default: 8080)')
    server_parser.add_argument('--daemon', '-d', action='store_true', help='Run server as background daemon process')
    server_parser.add_argument('--stop', action='store_true', help='Stop running server daemon')
    server_parser.add_argument('--status', action='store_true', help='Check running server status')
    server_parser.add_argument('--install', action='store_true', help='Install and enable systemd boot service')
    server_parser.add_argument('--uninstall', action='store_true', help='Remove systemd boot service')
    server_parser.add_argument('--json', action='store_true', help='Output status in JSON format')

    # user
    user_parser = subparsers.add_parser('user', help='Manage user containers and workspaces')
    user_sub = user_parser.add_subparsers(dest='user_action', help='User action')
    user_sub.add_parser('list', help='List user registry and container statuses')

    user_create = user_sub.add_parser('create', help='Create user workspace & container')
    user_create.add_argument('username', help='Username')
    user_create.add_argument('--password', help='Initial user password')
    user_create.add_argument('--role', choices=['user', 'admin'], default='user', help='User role')

    user_pwd = user_sub.add_parser('password', help='Set or update a user password')
    user_pwd.add_argument('username', help='Username')
    user_pwd.add_argument('password', help='New password')

    user_start = user_sub.add_parser('start', help='Start user container')
    user_start.add_argument('username', help='Username')

    user_stop = user_sub.add_parser('stop', help='Stop user container')
    user_stop.add_argument('username', help='Username')

    user_del = user_sub.add_parser('delete', help='Delete user workspace and container')
    user_del.add_argument('username', help='Username')

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

    # diagnose
    diag_parser = subparsers.add_parser('diagnose', help='Display current platform pain points and logged issues')
    diag_parser.add_argument('--json', action='store_true', help='Output issues in JSON format')
    diag_parser.add_argument('--clear', action='store_true', help='Clear or resolve current logged issues')
    diag_parser.add_argument('--log-session', type=str, metavar='PATH', help='Parse and log pain points for a transcript file')

    # ingest
    ingest_parser = subparsers.add_parser('ingest', help='Run automated source ingestion (GDrive API delta check)')
    ingest_parser.add_argument('--gdrive', action='store_true', help='Run Google Drive API delta ingestion')
    ingest_parser.add_argument('--dry-run', action='store_true', help='Scan deltas without modifying files')

    args = parser.parse_args()

    if args.interactive:
        sys.exit(cmd_interactive(args))

    if args.subcommand in ('job', 'cron'):
        sys.exit(cmd_job(args))
    elif args.subcommand == 'server':
        sys.exit(cmd_server(args))
    elif args.subcommand == 'user':
        sys.exit(cmd_user(args))
    elif args.subcommand == 'status':
        sys.exit(cmd_status(args))
    elif args.subcommand == 'ingest':
        from jobs import run_job
        res = run_job(root_dir, 'gdrive_sync', dry_run=getattr(args, 'dry_run', False))
        sys.exit(0 if res.get('status') != 'error' else 1)
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
            sys.exit(cmd_interactive(args))

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
    elif args.subcommand == 'diagnose':
        sys.exit(cmd_diagnose(args))
    else:
        sys.exit(cmd_interactive(args))



if __name__ == '__main__':
    main()
