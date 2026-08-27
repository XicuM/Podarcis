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

HARNESSES = {'opencode': 'opencode', 'codex': 'codex', 'agy': 'agy', 'claude': 'claude', 'openclaw': 'openclaw', 'hermes': 'hermes', 'none': None}
BACKENDS = HARNESSES
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
    sync_all_harnesses,
    sync_all_backends,
)

from repos import (
    get_repo_names,
    get_repo_url,
    set_repo_url,
    get_repo_status,
    sync_repos_full,
    push_repos,
)


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


def _cmd_config_set_status(args: argparse.Namespace, enable: bool) -> int:
    '''Enable or disable a component (mcp, skill, agent).'''
    harness = get_config_value(root_dir, 'harness', default=get_config_value(root_dir, 'backend', default='none'))
    if harness == 'none':
        console.print(
            '[bold yellow]Warning:[/bold yellow] No harness selected — MCP config changes will have no effect.\n'
            'Change it with: [bold]podarcis config harness <name>[/bold] or [bold]podarcis config interactive[/bold]'
        )
    ctype = args.type.lower()
    name = args.name
    mcp_servers, skills, agents = discover_components(root_dir)

    if ctype == 'mcp':
        if name not in mcp_servers:
            console.print(f'[bold red]Error:[/bold red] MCP server "{name}" not found. Available: {", ".join(mcp_servers.keys())}')
            return 1
        set_mcp_server_status(root_dir, name, enable, mcp_servers[name])
        msg = f'[bold green]✓ Enabled[/bold green]' if enable else '[yellow]Disabled[/yellow]'
        console.print(f'{msg} MCP server "{name}".')

    elif ctype == 'skill':
        if name not in skills:
            console.print(f'[bold red]Error:[/bold red] Skill "{name}" not found. Available: {", ".join(skills.keys())}')
            return 1
        set_skill_status(root_dir, name, enable, skills[name])
        msg = f'[bold green]✓ Enabled[/bold green]' if enable else '[yellow]Disabled[/yellow]'
        console.print(f'{msg} skill "{name}".')

    elif ctype == 'agent':
        if name not in agents:
            console.print(f'[bold red]Error:[/bold red] Agent "{name}" not found. Available: {", ".join(agents.keys())}')
            return 1
        set_agent_status(root_dir, name, enable, agents[name])
        msg = f'[bold green]✓ Enabled[/bold green]' if enable else '[yellow]Disabled[/yellow]'
        console.print(f'{msg} agent "{name}".')

    else:
        console.print(f'[bold red]Error:[/bold red] Unknown component type "{ctype}". Must be one of: mcp, skill, agent')
        return 1

    return 0


def cmd_config_enable(args: argparse.Namespace) -> int:
    '''Enable a component (mcp, skill, agent).'''
    return _cmd_config_set_status(args, True)


def cmd_config_disable(args: argparse.Namespace) -> int:
    '''Disable a component (mcp, skill, agent).'''
    return _cmd_config_set_status(args, False)


def cmd_config_repo(args: argparse.Namespace) -> int:
    '''Update repository remote or local path configuration.'''
    from repos import ensure_local_git_repo
    repo_name = getattr(args, 'repo_name', None)
    known_repos = get_repo_names(root_dir)

    if not repo_name:
        console.print('[bold #29b8db]Configured Podarcis Repositories:[/bold #29b8db]\n')
        for r_name in known_repos:
            url = get_repo_url(root_dir, r_name)
            url_str = url if url else 'local-only'
            console.print(f'  • [bold white]{r_name:<15}[/bold white] {url_str}')
        return 0

    target_val = (getattr(args, 'url', None) or getattr(args, 'path', None) or '')
    if target_val is not None:
        target_val = target_val.strip()

    if getattr(args, 'local', False):
        set_repo_url(root_dir, repo_name, '')
        ensure_local_git_repo(root_dir, repo_name)
        console.print(f'[bold green]✓ Set {repo_name} to local-only.[/bold green]')
    elif getattr(args, 'url', None) is not None or getattr(args, 'path', None) is not None:
        set_repo_url(root_dir, repo_name, target_val)
        ensure_local_git_repo(root_dir, repo_name)
        if target_val:
            console.print(f'[bold green]✓ Set remote/path for {repo_name} to {target_val}[/bold green]')
        else:
            console.print(f'[bold green]✓ Set {repo_name} to local-only.[/bold green]')
    else:
        current_url = get_repo_url(root_dir, repo_name)
        remote_label = current_url if current_url else 'local-only'
        console.print(f'Repository "{repo_name}": {remote_label}')

    return 0



# Claudian Obsidian plugin ID
_CLAUDIAN_PLUGIN_ID = 'realclaudian'

# Maps Podarcis harness name to the Claudian settingsProvider value.
# Harnesses absent from this map are unsupported: the plugin is disabled.
_HARNESS_TO_CLAUDIAN: dict[str, str] = {
    'claude': 'claude',
    'opencode': 'opencode',
    'codex': 'codex',
    # 'openclaw' and 'hermes' have no Claudian providerId → plugin is disabled
}
_BACKEND_TO_CLAUDIAN = _HARNESS_TO_CLAUDIAN


def _sync_claudian_plugin(harness: str) -> None:
    '''Sync the Claudian Obsidian plugin state to match the active Podarcis harness.

    Supported harnesses (claude / opencode / codex): enable the plugin and
    write the matching ``settingsProvider`` into its data.json.
    Unsupported harnesses (agy / openclaw / hermes): disable the plugin entirely.
    '''
    obsidian_dir = root_dir / '.obsidian'
    community_plugins_path = obsidian_dir / 'community-plugins.json'
    plugin_data_path = obsidian_dir / 'plugins' / _CLAUDIAN_PLUGIN_ID / 'data.json'

    provider = _HARNESS_TO_CLAUDIAN.get(harness)
    supported = provider is not None

    if supported:
        obsidian_dir.mkdir(parents=True, exist_ok=True)

    # --- community-plugins.json: add or remove the plugin entry ---
    plugins: list[str] = []
    if community_plugins_path.exists():
        try:
            plugins = json.loads(community_plugins_path.read_text(encoding='utf-8'))
            if not isinstance(plugins, list):
                plugins = []
        except Exception:
            plugins = []

    if supported and _CLAUDIAN_PLUGIN_ID not in plugins:
        plugins.append(_CLAUDIAN_PLUGIN_ID)
        community_plugins_path.write_text(
            json.dumps(plugins, indent=2) + '\n', encoding='utf-8'
        )
        console.print(f'[dim]Claudian: enabled in community-plugins.json[/dim]')
    elif not supported and _CLAUDIAN_PLUGIN_ID in plugins:
        plugins.remove(_CLAUDIAN_PLUGIN_ID)
        community_plugins_path.write_text(
            json.dumps(plugins, indent=2) + '\n', encoding='utf-8'
        )
        console.print(f'[dim]Claudian: disabled in community-plugins.json (harness "{harness}" not supported)[/dim]')

    # --- data.json: set settingsProvider when supported ---
    if not supported:
        return

    plugin_data_path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if plugin_data_path.exists():
        try:
            data = json.loads(plugin_data_path.read_text(encoding='utf-8'))
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}

    data['settingsProvider'] = provider
    plugin_data_path.write_text(
        json.dumps(data, indent=2) + '\n', encoding='utf-8'
    )
    console.print(f'[dim]Claudian: settingsProvider set to "{provider}"[/dim]')


def cmd_config_harness(args: argparse.Namespace) -> int:
    '''Set or show the harness name (opencode, codex, agy, claude, openclaw, hermes, none).'''
    harnesses = {'opencode', 'codex', 'agy', 'claude', 'openclaw', 'hermes', 'none'}
    harness_arg = getattr(args, 'harness_name', None) or getattr(args, 'backend_name', None)
    name = harness_arg.lower() if harness_arg else ''
    if name not in harnesses:
        console.print(f'[bold red]Error:[/bold red] Unknown harness "{name}". Choose from: {", ".join(sorted(harnesses))}')
        return 1
    set_config_value(root_dir, name, 'harness')
    if name == 'none':
        console.print('[bold yellow]✓ Harness set to none.[/bold yellow] MCP configuration will be skipped.')
        return 0
    _sync_claudian_plugin(name)
    # Regenerate MCP config for the newly active harness
    from harnesses import generate_for_harness
    paths = generate_for_harness(root_dir, name)
    if paths:
        path_list = paths if isinstance(paths, list) else [paths]
        console.print(f'[dim]Regenerated MCP config for {name}: {[str(p) for p in path_list]}[/dim]')
    console.print(f'[bold green]✓ Harness set to {name}.[/bold green]')
    return 0


cmd_config_backend = cmd_config_harness


def cmd_config_sync(args: argparse.Namespace) -> int:
    '''Regenerate MCP config for harnesses and sync workspace repositories.'''
    results = sync_all_harnesses(root_dir)
    console.print('[bold #29b8db]Synced MCP config to all harnesses:[/bold #29b8db]\n')
    for harness, paths in results.items():
        if paths:
            console.print(f'  [green]✓[/green] {harness:<12} → {[str(p.name) for p in paths]}')
        else:
            console.print(f'  [dim]—[/dim] {harness:<12} (no files written)')

    console.print('\n[bold #29b8db]Synchronizing workspace repositories...[/bold #29b8db]\n')
    repo_results = sync_repos_full(root_dir)
    for rname, rinfo in repo_results.items():
        st = rinfo.get('status')
        msg = rinfo.get('message')
        if st == 'ok':
            console.print(f'  [green]✓[/green] [bold]{rname:<12}[/bold] {msg}')
        elif st == 'warning':
            console.print(f'  [yellow]⚠️[/yellow] [bold]{rname:<12}[/bold] {msg}')
        else:
            console.print(f'  [red]✗[/red] [bold]{rname:<12}[/bold] {msg}')
    return 0


def cmd_repo(args: argparse.Namespace) -> int:
    '''Manage and synchronize workspace repositories (workspace, wiki, sources).'''
    from rich.table import Table

    action = getattr(args, 'repo_action', 'status') or 'status'

    if action == 'status':
        statuses = get_repo_status(root_dir)
        if getattr(args, 'json', False):
            print(json.dumps(statuses, indent=2))
            return 0

        table = Table(title="Workspace Repositories Status", border_style="cyan")
        table.add_column("Repo", style="bold white", width=12)
        table.add_column("Type", style="cyan", width=8)
        table.add_column("Branch", style="magenta", width=12)
        table.add_column("Status", style="yellow", width=16)
        table.add_column("Changes", justify="right", width=8)
        table.add_column("Ahead/Behind", justify="right", width=12)
        table.add_column("Remote / Target", style="dim")

        for s in statuses:
            st = s['status']
            st_str = f"[green]✓ {st}[/green]" if st in ('synced', 'ready', 'gdrive_managed') else f"[yellow]{st}[/yellow]"
            ab = f"+{s['ahead']} / -{s['behind']}" if (s['ahead'] or s['behind']) else "—"
            table.add_row(
                s['repo'],
                s['type'],
                s['branch'] or '—',
                st_str,
                str(s['changes']) if s['changes'] else "0",
                ab,
                s['url'] or 'local'
            )
        console.print(table)
        return 0

    elif action in ('sync', 'pull'):
        console.print('[bold #29b8db]Synchronizing all workspace repositories & backends...[/bold #29b8db]\n')
        res = sync_repos_full(root_dir)
        for rname, rinfo in res.items():
            st = rinfo.get('status')
            msg = rinfo.get('message')
            if st == 'ok':
                console.print(f'  [green]✓[/green] [bold]{rname:<12}[/bold] {msg}')
            elif st == 'warning':
                console.print(f'  [yellow]⚠️[/yellow] [bold]{rname:<12}[/bold] {msg}')
            else:
                console.print(f'  [red]✗[/red] [bold]{rname:<12}[/bold] {msg}')
        return 0

    elif action == 'push':
        console.print('[bold #29b8db]Pushing local workspace changes to remotes...[/bold #29b8db]\n')
        auto_commit = getattr(args, 'commit', False)
        msg = getattr(args, 'message', 'chore: sync workspace changes') or 'chore: sync workspace changes'
        res = push_repos(root_dir, auto_commit=auto_commit, message=msg)
        for rname, rinfo in res.items():
            st = rinfo.get('status')
            r_msg = rinfo.get('message')
            if st == 'ok':
                console.print(f'  [green]✓[/green] [bold]{rname:<12}[/bold] {r_msg}')
            elif st == 'skipped':
                console.print(f'  [dim]—[/dim] [bold]{rname:<12}[/bold] {r_msg}')
            else:
                console.print(f'  [red]✗[/red] [bold]{rname:<12}[/bold] {r_msg}')
        return 0

    elif action == 'config':
        return cmd_config_repo(args)

    return 0


def _ensure_vscode_config(root: Path) -> None:
    '''Ensure .vscode user configuration directories are initialized from templates if missing.'''
    template_dir = root / '.podarcis' / 'templates' / 'vscode'
    target_dir = root / '.vscode'
    if template_dir.exists():
        target_dir.mkdir(parents=True, exist_ok=True)
        for item in template_dir.iterdir():
            target_file = target_dir / item.name
            if not target_file.exists():
                shutil.copy2(item, target_file)
                console.print(f'[dim]Initialized .vscode/{item.name} from template[/dim]')


def cmd_config_frontend(args: argparse.Namespace) -> int:
    '''Set or show the frontend name.'''
    name = args.frontend_name.lower()
    if name not in FRONTENDS:
        console.print(f'[bold red]Error:[/bold red] Unknown frontend "{name}". Choose from: {", ".join(sorted(FRONTENDS))}')
        return 1
    set_config_value(root_dir, name, 'frontend')
    if name == 'vscode':
        _ensure_vscode_config(root_dir)
    elif name == 'obsidian':
        harness = get_config_value(root_dir, 'harness', default=get_config_value(root_dir, 'backend', default='none'))
        cfg_plugins = getattr(args, 'configure_plugins', None)
        if cfg_plugins is None and sys.stdin.isatty():
            try:
                import questionary
                cfg_plugins = questionary.confirm(
                    f'Configure Obsidian plugins for agentic knowledge base (Claudian for harness "{harness}")?',
                    default=True,
                ).ask()
            except Exception:
                cfg_plugins = False

        if cfg_plugins:
            _sync_claudian_plugin(harness)
            console.print(f'[bold green]✓ Frontend set to obsidian with Claudian plugin configured.[/bold green]')
        else:
            console.print(f'[bold green]✓ Frontend set to obsidian.[/bold green] [dim]Skipped Obsidian plugin configuration.[/dim]')
    if name == 'none':
        console.print('[bold yellow]✓ Frontend set to none.[/bold yellow] Opening a frontend will be skipped.')
    else:
        console.print(f'[bold green]✓ Frontend set to {name}.[/bold green]')
    return 0


def cmd_harness(args: argparse.Namespace) -> int:
    '''Open the configured harness.'''
    harness = get_config_value(root_dir, 'harness', default=get_config_value(root_dir, 'backend', default='none'))
    if harness == 'none':
        console.print(
            '[bold yellow]Warning:[/bold yellow] No harness selected.\n'
            'Change it with: [bold]podarcis config harness <name>[/bold] or [bold]podarcis config interactive[/bold]'
        )
        return 1
    return cmd_open_tool('harness')


cmd_backend = cmd_harness


def cmd_frontend(args: argparse.Namespace) -> int:
    '''Open the configured frontend.'''
    from banner import display_project_banner
    display_project_banner(root_dir)
    frontend = get_config_value(root_dir, 'frontend', default='none')
    if frontend == 'none':
        return 0
    return cmd_open_tool('frontend')


def cmd_interactive(args: argparse.Namespace) -> int:
    '''Launch TUI interactive menu.'''
    from interactive import interactive_config
    interactive_config(root_dir)
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    '''Run bootstrap installer.'''
    install_script = root_dir / '.podarcis' / 'install.py'
    py_bin = sys.executable
    return subprocess.run([py_bin, str(install_script)] + args.remaining_args).returncode


def cmd_clean(args: argparse.Namespace) -> int:
    '''Clean Python build artifacts and cache files.'''
    import shutil
    count = 0
    for p in root_dir.rglob('__pycache__'):
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
            count += 1
    for p in root_dir.rglob('*.pyc'):
        if p.is_file():
            p.unlink(missing_ok=True)
            count += 1
    for p in root_dir.glob('.pytest_cache'):
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
            count += 1
    for p in root_dir.rglob('*.egg-info'):
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
            count += 1
    console.print(f'[bold green]✓ Cleaned {count} build artifacts and cache directories.[/bold green]')
    return 0


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

    resolve_id = getattr(args, 'resolve', None)
    if resolve_id:
        if hasattr(diag_mod, 'resolve_issue_by_id'):
            ok = diag_mod.resolve_issue_by_id(resolve_id, base_dir=root_dir)
            if ok:
                console.print(f'[bold green]✓ Marked pain point [{resolve_id}] as resolved.[/bold green]')
                return 0
            else:
                console.print(f'[bold red]Error:[/bold red] Pain point ID [{resolve_id}] not found or already resolved.')
                return 1

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


def cmd_research(args: argparse.Namespace) -> int:
    '''Search academic literature or ingest papers into sources/ literature.'''
    if 'research_mcp_server' in sys.modules:
        research_server = sys.modules['research_mcp_server']
    else:
        res_script = root_dir / '.agents' / 'mcp' / 'research' / 'server.py'
        if not res_script.exists():
            console.print('[bold red]Error:[/bold red] research server module not found.')
            return 1
        import importlib.util
        spec = importlib.util.spec_from_file_location('research_mcp_server', res_script)
        if spec is None or spec.loader is None:
            console.print('[bold red]Error:[/bold red] Could not load research server module.')
            return 1
        research_server = importlib.util.module_from_spec(spec)
        sys.modules['research_mcp_server'] = research_server
        spec.loader.exec_module(research_server)




    action = getattr(args, 'research_action', None)
    if action == 'search':
        import asyncio
        query = args.query
        limit = getattr(args, 'limit', 5)
        provider = getattr(args, 'provider', 'all')
        results = asyncio.run(research_server.search_literature(query=query, limit=limit, provider=provider))
        if getattr(args, 'json', False):
            print(json.dumps(results, indent=2))
            return 0
        if not results:
            console.print(f'[yellow]No research papers found for query: "{query}"[/yellow]')
            return 0
        console.print(f'[bold #29b8db]Literature Search Results ({len(results)} found):[/bold #29b8db]\n')
        for i, item in enumerate(results, 1):
            title = item.get('title') or 'Unknown Title'
            year = item.get('year') or 'Unknown'
            pid = item.get('paperId')
            ext = item.get('externalIds') or {}
            doi = ext.get('DOI')
            arxiv = ext.get('ArXiv')
            pmid = ext.get('PubMed')
            id_str = pid if pid else (f"DOI:{doi}" if doi else (f"arXiv:{arxiv}" if arxiv else (f"pmid:{pmid}" if pmid else "N/A")))
            abstract = (item.get('abstract') or '').replace('\n', ' ').strip()
            if len(abstract) > 180:
                abstract = abstract[:180] + '...'
            console.print(f'[bold green]{i}. {title}[/bold green] ({year})')
            console.print(f'   [dim]ID:[/dim] {id_str}')
            if abstract:
                console.print(f'   [dim]{abstract}[/dim]')
            console.print('')
        return 0

    elif action == 'ingest':
        import asyncio
        import re
        from unittest.mock import AsyncMock
        paper_id = args.paper_id
        domain = args.domain
        name = getattr(args, 'name', None)
        ctx = AsyncMock()

        async def _run_ingest():
            meta = await research_server._resolve_metadata(paper_id)
            clean_title = re.sub(r'[^a-z0-9_]+', '_', meta.title.lower()).strip('_')
            filename_base = name or clean_title[:45]
            res = await research_server._ingest_paper(ctx, paper_id, filename_base, domain, meta)
            return res

        try:
            res = asyncio.run(_run_ingest())
            if getattr(args, 'json', False):
                print(json.dumps(res, indent=2))
            else:
                console.print(f'[bold green]✓ Successfully ingested paper![/bold green]')
                console.print(f'  • Path: [cyan]{res.get("paper_dir")}[/cyan]')
                console.print(f'  • Files: {", ".join(res.get("files", []))}')
            return 0
        except Exception as exc:
            console.print(f'[bold red]Ingestion failed:[/bold red] {exc}')
            return 1

    else:
        console.print('[yellow]Usage: podarcis research [search|ingest] ...[/yellow]')
        return 1








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
    '''Open the configured tool (harness or frontend) at the current directory.'''
    is_harness = tool_type in ('harness', 'backend')
    valid = HARNESSES if is_harness else FRONTENDS
    if is_harness:
        name = get_config_value(root_dir, 'harness', default=get_config_value(root_dir, 'backend', default='opencode'))
    else:
        name = get_config_value(root_dir, 'frontend', default='vscode')
    cwd = str(root_dir)

    if not is_harness and name.lower() == 'vscode':
        _ensure_vscode_config(root_dir)

    command = valid.get(name.lower(), name)
    if not command:
        return 0

    try:
        if is_harness:
            os.execvp(command, [command, cwd])
        else:
            if name.lower() == 'obsidian':
                import urllib.parse
                uri = f"obsidian://open?path={urllib.parse.quote(cwd)}"
                subprocess.Popen([command, uri], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
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

    # job
    j_parser = subparsers.add_parser('job', help='Manage and execute modular scheduled jobs (.agents/jobs/*.yaml)')
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
    cfg_repo = config_sub.add_parser('repo', help='Configure repository Git remotes or local paths')
    cfg_repo.add_argument('repo_name', nargs='?', help='Repository name (wiki, workspace, user, sources, etc.)')
    cfg_repo.add_argument('--url', help='Remote Git URL or repository path')
    cfg_repo.add_argument('--path', help='Local directory path or target path')
    cfg_repo.add_argument('--local', action='store_true', help='Set repository to local-only (no remote)')


    # config harness
    cfg_harness = config_sub.add_parser('harness', help='Set the agent harness (opencode, codex, agy, claude, none, …)')
    cfg_harness.add_argument('harness_name', choices=list(HARNESSES), help='Harness name')

    # config backend (alias)
    cfg_backend = config_sub.add_parser('backend', help='Set the agent harness (alias for config harness)')
    cfg_backend.add_argument('backend_name', choices=list(HARNESSES), help='Harness name')

    # config frontend
    cfg_frontend = config_sub.add_parser('frontend', help='Set the frontend tool (vscode, obsidian, none)')
    cfg_frontend.add_argument('frontend_name', choices=list(FRONTENDS), metavar='{vscode,obsidian,none}', help='Frontend name')
    cfg_frontend.add_argument('--configure-plugins', action='store_true', dest='configure_plugins', default=None, help='Configure Obsidian plugins (Claudian)')
    cfg_frontend.add_argument('--no-plugins', action='store_false', dest='configure_plugins', help='Skip Obsidian plugin configuration')

    # config interactive
    config_sub.add_parser('interactive', help='Launch interactive TUI menu')

    # repo / repos
    repo_parser = subparsers.add_parser('repo', aliases=['repos'], help='Manage and synchronize workspace repositories')
    repo_sub = repo_parser.add_subparsers(dest='repo_action', help='Repository action')
    repo_st = repo_sub.add_parser('status', help='Display Git and sync status across all workspace repositories')
    repo_st.add_argument('--json', action='store_true', help='Output repository status in JSON format')

    repo_sync = repo_sub.add_parser('sync', aliases=['pull'], help='Synchronize workspace repositories (pull git remotes & ingest gdrive deltas)')

    repo_push = repo_sub.add_parser('push', help='Push local commits to remotes for workspace repositories')
    repo_push.add_argument('--commit', '-c', action='store_true', help='Commit uncommitted local changes before pushing')
    repo_push.add_argument('--message', '-m', default='chore: sync workspace changes', help='Commit message')

    repo_cfg = repo_sub.add_parser('config', help='Configure repository Git remotes or local paths')
    repo_cfg.add_argument('repo_name', nargs='?', help='Repository name (wiki, workspace, user, sources, etc.)')
    repo_cfg.add_argument('--url', help='Remote Git URL or repository path')
    repo_cfg.add_argument('--path', help='Local directory path or target path')
    repo_cfg.add_argument('--local', action='store_true', help='Set repository to local-only (no remote)')

    # sync (top-level)
    subparsers.add_parser('sync', help='Synchronize workspace repos, Google Drive deltas, and MCP harness configs')

    # harness & backend
    subparsers.add_parser('harness', help='Open the configured harness tool')
    subparsers.add_parser('backend', help='Open the configured harness tool (alias for harness)')

    # frontend
    subparsers.add_parser('frontend', help='Open the configured frontend tool')



    # install & reinstall
    install_parser = subparsers.add_parser('install', help='Run bootstrap installer')
    install_parser.add_argument('remaining_args', nargs=argparse.REMAINDER)

    # clean
    clean_parser = subparsers.add_parser('clean', help='Clean Python build artifacts and cache files')

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
    diag_parser.add_argument('--resolve', type=str, help='Mark a specific pain point ID as resolved')
    diag_parser.add_argument('--log-session', type=str, metavar='PATH', help='Parse and log pain points for a transcript file')

    # research
    research_parser = subparsers.add_parser('research', help='Search peer-reviewed literature and ingest papers into sources/')
    research_sub = research_parser.add_subparsers(dest='research_action', help='Research action')

    r_search = research_sub.add_parser('search', help='Search literature across PubMed, OpenAlex, arXiv, and Semantic Scholar')
    r_search.add_argument('query', help='Search query or topic')
    r_search.add_argument('--limit', type=int, default=5, help='Maximum results (default 5)')
    r_search.add_argument('--provider', default='all', choices=['all', 'pubmed', 'openalex', 'arxiv', 'semanticscholar'], help='Provider filter')
    r_search.add_argument('--json', action='store_true', help='Output search results in JSON format')

    r_ingest = research_sub.add_parser('ingest', help='Fetch PDF, extract text, and ingest paper into sources/literature/')
    r_ingest.add_argument('paper_id', help='Paper ID (DOI:xxx, openalex:xxx, pmid:xxx, arXiv:xxx, or raw title/hash)')
    r_ingest.add_argument('--domain', required=True, help='Target domain directory under sources/literature/')
    r_ingest.add_argument('--name', help='Custom snake_case slug directory name')
    r_ingest.add_argument('--json', action='store_true', help='Output ingestion result in JSON format')

    # ingest
    ingest_parser = subparsers.add_parser('ingest', help='Run automated source ingestion (GDrive API delta check)')
    ingest_parser.add_argument('--gdrive', action='store_true', help='Run Google Drive API delta ingestion')
    ingest_parser.add_argument('--dry-run', action='store_true', help='Scan deltas without modifying files')

    args = parser.parse_args()

    if args.interactive:
        sys.exit(cmd_interactive(args))

    if args.subcommand in ('repo', 'repos'):
        sys.exit(cmd_repo(args))
    elif args.subcommand == 'sync':
        sys.exit(cmd_config_sync(args))
    elif args.subcommand in ('job',):
        sys.exit(cmd_job(args))
    elif args.subcommand == 'research':
        sys.exit(cmd_research(args))
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
        elif args.config_action in ('harness', 'backend'):
            sys.exit(cmd_config_harness(args))
        elif args.config_action == 'frontend':
            sys.exit(cmd_config_frontend(args))
        elif args.config_action == 'interactive':
            sys.exit(cmd_interactive(args))
        elif args.config_action == 'sync':
            sys.exit(cmd_config_sync(args))
        else:
            sys.exit(cmd_interactive(args))

    elif args.subcommand in ('harness', 'backend'):
        sys.exit(cmd_harness(args))
    elif args.subcommand == 'frontend':
        sys.exit(cmd_frontend(args))
    elif args.subcommand == 'install':
        sys.exit(cmd_install(args))
    elif args.subcommand == 'clean':
        sys.exit(cmd_clean(args))
    elif args.subcommand == 'uninstall':
        sys.exit(cmd_uninstall(args))
    elif args.subcommand == 'test':
        sys.exit(cmd_test(args))
    elif args.subcommand == 'lint':
        sys.exit(cmd_lint(args))
    elif args.subcommand == 'diagnose':
        sys.exit(cmd_diagnose(args))
    else:
        sys.exit(cmd_frontend(args))




if __name__ == '__main__':
    main()
