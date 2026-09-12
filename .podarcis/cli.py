#!/usr/bin/env python3
'''Podarcis CLI tool for agentic configuration management, testing, and lifecycle.'''

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

FRONTENDS = {'vscode': 'code', 'obsidian': 'obsidian', 'none': None}

from podarcis import PODARCIS_DIR, ROOT_DIR
from podarcis.common import get_config_value, load_version_info, set_config_value
from podarcis.console import console
from podarcis.components import (
    discover_components,
    external_skills,
    get_enabled_mcp_servers,
    set_mcp_server_status,
)

from podarcis.repos import (
    get_repo_names,
    get_repo_url,
    set_repo_url,
    get_repo_status,
    sync_repos_full,
    push_repos,
)


def _get_python_bin() -> str:
    '''Get path to python binary inside virtualenv if available.'''
    venv_py = ROOT_DIR / '.venv' / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
    if venv_py.exists():
        return str(venv_py)
    return sys.executable


def _get_pytest_bin() -> str:
    '''Get path to pytest binary inside virtualenv if available.'''
    venv_pytest = ROOT_DIR / '.venv' / ('Scripts/pytest.exe' if sys.platform == 'win32' else 'bin/pytest')
    if venv_pytest.exists():
        return str(venv_pytest)
    return 'pytest'


def cmd_status(args: argparse.Namespace) -> int:
    '''List component, job, and repository status.'''
    mcp_servers, skills, agents = discover_components(ROOT_DIR)
    enabled_mcp = get_enabled_mcp_servers(ROOT_DIR)
    from podarcis.jobs import discover_jobs
    jobs = discover_jobs(ROOT_DIR)
    external = external_skills(ROOT_DIR)

    status_data = {
        'mcp_servers': {},
        'skills': {},
        'agents': {},
        'external_skills': {},
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

    for key, info in sorted(external.items()):
        status_data['external_skills'][key] = {
            'executables': info['executables'],
            'ok': all(info['executables'].values()),
        }

    for key, info in sorted(jobs.items()):
        status_data['jobs'][key] = {
            'enabled': info.get('enabled', False),
            'schedule': info.get('schedule', ''),
            'description': info.get('description', ''),
            'last_run': info.get('last_run', ''),
        }

    for r_name in get_repo_names(ROOT_DIR):
        url = get_repo_url(ROOT_DIR, r_name)
        status_data['repositories'][r_name] = {
            'remote_url': url,
            'is_local_only': not bool(url),
        }

    if getattr(args, 'json', False):
        print(json.dumps(status_data, indent=2))
        return 0

    console.print('[bold #29b8db]Podarcis Configuration Status[/bold #29b8db]\n')

    # Tool modules are the only context cost paid up front, so they get the budget.
    live_tk = sum(v['tokens'] for v in status_data['mcp_servers'].values() if v['enabled'])
    console.print(f'[bold white]MCP tool modules:[/bold white] [dim]{live_tk:,} tokens per session[/dim]')
    for k, v in sorted(status_data['mcp_servers'].items(), key=lambda i: -i[1]['tokens']):
        st = '[green]enabled[/green]' if v['enabled'] else '[dim red]disabled[/dim red]'
        console.print(f'  • {k:<20} [{st}] ({v["tokens"]} tokens)')

    # Skills and personas load on invocation, so there is no per-session budget
    # and nothing to toggle — list them, don't imply a switch.
    for label, section in (('Personas', 'agents'), ('Skills', 'skills')):
        console.print(f'\n[bold white]{label}:[/bold white] [dim]loaded on demand[/dim]')
        for k, v in status_data[section].items():
            console.print(f'  • {k:<20} [dim]({v["tokens"]} tokens when invoked)[/dim]')

    # APM deploys a skill's files but installs no runtime, and a failing
    # lifecycle script does not fail `apm install` — so a deployed skill whose
    # CLI never got installed looks fine everywhere except here.
    if status_data['external_skills']:
        console.print('\n[bold white]External skills:[/bold white] [dim]installed via `apm install`[/dim]')
        for k, v in status_data['external_skills'].items():
            if not v['executables']:
                detail = '[dim]no CLI declared[/dim]'
            else:
                detail = ', '.join(
                    f'[green]{name}[/green]' if path else f'[bold red]{name} MISSING[/bold red]'
                    for name, path in v['executables'].items())
            console.print(f'  • {k:<20} {detail}')
        if any(not v['ok'] for v in status_data['external_skills'].values()):
            console.print('  [yellow]Run `apm lifecycle trust` then `apm install` to install missing CLIs.[/yellow]')

    console.print('\n[bold white]Jobs:[/bold white]')
    for k, v in status_data['jobs'].items():
        st = '[green]enabled[/green]' if v['enabled'] else '[dim red]disabled[/dim red]'
        sched = f'({v["schedule"]})' if v["schedule"] else ''
        console.print(f'  • {k:<20} [{st}] {sched}')

    console.print('\n[bold white]Repositories:[/bold white]')
    for k, v in status_data['repositories'].items():
        url_str = v['remote_url'] if v['remote_url'] else 'local-only'
        console.print(f'  • [bold #29b8db]{k:<20}[/bold #29b8db] {url_str}')

    return 0


def _cmd_config_set_status(args: argparse.Namespace, enable: bool) -> int:
    '''Enable or disable an MCP tool module.

    Tool modules are the only toggleable component: their schemas load into
    every session up front. The harness loads only a skill's or persona's
    one-line description until something invokes it, so gating those saves
    ~60 tokens and is not worth a config surface.
    '''
    name = args.name
    mcp_servers, _, _ = discover_components(ROOT_DIR)

    if name not in mcp_servers:
        skills, agents = discover_components(ROOT_DIR)[1:]
        if name in skills or name in agents:
            console.print(
                f'[bold red]Error:[/bold red] "{name}" is a '
                f'{"skill" if name in skills else "persona"}, not a tool module, '
                f'and is not toggleable. To retire it, set '
                f'[bold]disabled: true[/bold] in its own frontmatter.'
            )
            return 1
        console.print(
            f'[bold red]Error:[/bold red] Tool module "{name}" not found. '
            f'Available: {", ".join(sorted(mcp_servers))}'
        )
        return 1

    set_mcp_server_status(ROOT_DIR, name, enable, mcp_servers[name])
    msg = '[bold green]✓ Enabled[/bold green]' if enable else '[yellow]Disabled[/yellow]'
    console.print(f'{msg} tool module "{name}".')
    return 0


def cmd_config_enable(args: argparse.Namespace) -> int:
    '''Enable an MCP tool module.'''
    return _cmd_config_set_status(args, True)


def cmd_config_disable(args: argparse.Namespace) -> int:
    '''Disable an MCP tool module.'''
    return _cmd_config_set_status(args, False)


def cmd_config_repo(args: argparse.Namespace) -> int:
    '''Update repository remote or local path configuration.'''
    from podarcis.repos import ensure_local_git_repo
    repo_name = getattr(args, 'repo_name', None)
    known_repos = get_repo_names(ROOT_DIR)

    if not repo_name:
        console.print('[bold #29b8db]Configured Podarcis Repositories:[/bold #29b8db]\n')
        for r_name in known_repos:
            url = get_repo_url(ROOT_DIR, r_name)
            url_str = url if url else 'local-only'
            console.print(f'  • [bold white]{r_name:<15}[/bold white] {url_str}')
        return 0

    target_val = (getattr(args, 'url', None) or getattr(args, 'path', None) or '')
    if target_val is not None:
        target_val = target_val.strip()

    if getattr(args, 'local', False):
        set_repo_url(ROOT_DIR, repo_name, '')
        ensure_local_git_repo(ROOT_DIR, repo_name)
        console.print(f'[bold green]✓ Set {repo_name} to local-only.[/bold green]')
    elif getattr(args, 'url', None) is not None or getattr(args, 'path', None) is not None:
        set_repo_url(ROOT_DIR, repo_name, target_val)
        ensure_local_git_repo(ROOT_DIR, repo_name)
        if target_val:
            console.print(f'[bold green]✓ Set remote/path for {repo_name} to {target_val}[/bold green]')
        else:
            console.print(f'[bold green]✓ Set {repo_name} to local-only.[/bold green]')
    else:
        current_url = get_repo_url(ROOT_DIR, repo_name)
        remote_label = current_url if current_url else 'local-only'
        console.print(f'Repository "{repo_name}": {remote_label}')

    return 0



def cmd_repo_status(args: argparse.Namespace) -> int:
    '''Show git and sync status across all workspace repositories.'''
    from rich.table import Table

    statuses = get_repo_status(ROOT_DIR)
    if getattr(args, 'json', False):
        print(json.dumps(statuses, indent=2))
        return 0

    table = Table(title="Workspace Repositories Status", border_style="cyan")
    for col, style, width in (
        ("Repo", "bold white", 12), ("Type", "cyan", 8), ("Branch", "magenta", 12),
        ("Status", "yellow", 16),
    ):
        table.add_column(col, style=style, width=width)
    table.add_column("Changes", justify="right", width=8)
    table.add_column("Ahead/Behind", justify="right", width=12)
    table.add_column("Remote / Target", style="dim")

    for s in statuses:
        st = s['status']
        st_str = f"[green]✓ {st}[/green]" if st in ('synced', 'ready', 'gdrive_managed') else f"[yellow]{st}[/yellow]"
        ab = f"+{s['ahead']} / -{s['behind']}" if (s['ahead'] or s['behind']) else "—"
        table.add_row(
            s['repo'], s['type'], s['branch'] or '—', st_str,
            str(s['changes']) if s['changes'] else "0", ab, s['url'] or 'local',
        )
    console.print(table)
    return 0


def _print_repo_results(res: dict, symbols: dict[str, str]) -> None:
    for rname, rinfo in res.items():
        sym = symbols.get(rinfo.get('status'), '[red]✗[/red]')
        console.print(f'  {sym} [bold]{rname:<12}[/bold] {rinfo.get("message")}')


def cmd_repo_sync(args: argparse.Namespace) -> int:
    '''Pull git remotes and ingest gdrive deltas for every workspace repository.'''
    console.print('[bold #29b8db]Synchronizing all workspace repositories...[/bold #29b8db]\n')
    _print_repo_results(
        sync_repos_full(ROOT_DIR),
        {'ok': '[green]✓[/green]', 'warning': '[yellow]⚠️[/yellow]'},
    )
    return 0


def cmd_repo_push(args: argparse.Namespace) -> int:
    '''Push local commits to the configured remotes.'''
    console.print('[bold #29b8db]Pushing local workspace changes to remotes...[/bold #29b8db]\n')
    _print_repo_results(
        push_repos(ROOT_DIR, auto_commit=args.commit, message=args.message),
        {'ok': '[green]✓[/green]', 'skipped': '[dim]—[/dim]'},
    )
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
    '''Set the frontend tool.'''
    name = args.frontend_name.lower()
    set_config_value(ROOT_DIR, name, 'frontend')
    if name == 'vscode':
        _ensure_vscode_config(ROOT_DIR)
    if name == 'none':
        console.print('[bold yellow]✓ Frontend set to none.[/bold yellow] Opening a frontend will be skipped.')
    else:
        console.print(f'[bold green]✓ Frontend set to {name}.[/bold green]')
    return 0


def cmd_frontend(args: argparse.Namespace) -> int:
    '''Open the configured frontend.'''
    from podarcis.banner import display_project_banner
    display_project_banner(ROOT_DIR)
    frontend = get_config_value(ROOT_DIR, 'frontend', default='none')
    if frontend == 'none':
        return 0
    return cmd_open_tool()


def cmd_interactive(args: argparse.Namespace) -> int:
    '''Launch TUI interactive menu.'''
    from podarcis.interactive import interactive_config
    interactive_config(ROOT_DIR)
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    '''Run bootstrap installer.'''
    install_script = ROOT_DIR / '.podarcis' / 'install.py'
    py_bin = sys.executable
    return subprocess.run([py_bin, str(install_script)] + args.remaining_args).returncode


def cmd_clean(args: argparse.Namespace) -> int:
    '''Clean Python build artifacts and cache files.'''
    import shutil
    count = 0
    for p in ROOT_DIR.rglob('__pycache__'):
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
            count += 1
    for p in ROOT_DIR.rglob('*.pyc'):
        if p.is_file():
            p.unlink(missing_ok=True)
            count += 1
    for p in ROOT_DIR.glob('.pytest_cache'):
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
            count += 1
    for p in ROOT_DIR.rglob('*.egg-info'):
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
            count += 1
    console.print(f'[bold green]✓ Cleaned {count} build artifacts and cache directories.[/bold green]')
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    '''Remove global symlink, virtualenv, and build artefacts.'''
    uninstall_script = ROOT_DIR / '.podarcis' / 'uninstall.py'
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
    check_links = ROOT_DIR / '.agents' / 'mcp' / 'wiki' / 'check_links.py'
    py_bin = _get_python_bin()
    targets = args.remaining_args if args.remaining_args else [str(ROOT_DIR)]
    return subprocess.run([py_bin, str(check_links)] + targets).returncode


def cmd_diagnose(args: argparse.Namespace) -> int:
    '''Display platform pain points and current logged issues.'''
    diag_script = ROOT_DIR / '.apm' / 'skills' / 'self-improvement' / 'scripts' / 'diagnose_session.py'
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

    resolve_id, resolve_cat = getattr(args, 'resolve', None), getattr(args, 'resolve_category', None)
    if resolve_id or resolve_cat or getattr(args, 'clear', False):
        resolved = diag_mod.resolve_issues(
            base_dir=ROOT_DIR, ids=[resolve_id] if resolve_id else (),
            category=resolve_cat or '', sweep=getattr(args, 'clear', False),
        )
        if not resolved:
            console.print('[bold yellow]No unresolved pain points matched.[/bold yellow]')
            return 1
        console.print(f"[bold green]✓ Resolved {len(resolved)} pain point(s): {', '.join(resolved)}[/bold green]")
        return 0

    log_sess = getattr(args, 'log_session', None)
    if log_sess:
        p = Path(log_sess)
        points = diag_mod.parse_transcript(p)
        diag_mod.log_pain_points(points, base_dir=ROOT_DIR)
        console.print(f'[bold green]Parsed {p.name} and logged {len(points)} pain point(s).[/bold green]')

    issues = diag_mod.get_active_issues(base_dir=ROOT_DIR)
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
        res_script = ROOT_DIR / '.agents' / 'mcp' / 'research' / 'server.py'
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
        results = asyncio.run(research_server.literature_search(query=query, limit=limit, provider=provider))
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








def cmd_job_list(args: argparse.Namespace) -> int:
    '''List discovered jobs, schedules, and status.'''
    from podarcis.jobs import discover_jobs, scheduler

    discovered = discover_jobs(ROOT_DIR)
    if getattr(args, 'json', False):
        print(json.dumps(discovered, indent=2, default=str))
        return 0

    console.print('[bold #29b8db]Registered Jobs (.agents/jobs/*.yaml):[/bold #29b8db]\n')
    if not discovered:
        console.print('[dim]No jobs found in .agents/jobs/[/dim]')
        return 0
    for k, v in discovered.items():
        st = '[green]enabled[/green]' if v['enabled'] else '[dim red]disabled[/dim red]'
        nxt = scheduler.next_elapse(v['schedule']) if v['enabled'] else ''
        when = f' [dim]→ next {nxt}[/dim]' if nxt else ''
        last = f' [dim](last run: {v["last_run"]})[/dim]' if v['last_run'] else ''
        console.print(
            f'  • {k:<20} [{st}] [magenta]{v["type"]}[/magenta] '
            f'[cyan]({v["schedule"]})[/cyan]{when}{last}\n    [dim]{v["description"]}[/dim]\n'
        )
    return 0


def _cmd_job_set_status(args: argparse.Namespace, enable: bool) -> int:
    from podarcis.jobs import set_job_status

    ok, msg = set_job_status(ROOT_DIR, args.name, enable)
    if ok:
        console.print(f'[{"bold green" if enable else "yellow"}]✓ {msg}[/]')
        return 0
    console.print(f'[bold red]Error:[/bold red] {msg}')
    return 1


def cmd_job_enable(args: argparse.Namespace) -> int:
    '''Enable a job and install its systemd timer.'''
    return _cmd_job_set_status(args, True)


def cmd_job_disable(args: argparse.Namespace) -> int:
    '''Disable a job and remove its systemd timer.'''
    return _cmd_job_set_status(args, False)


def cmd_job_run(args: argparse.Namespace) -> int:
    '''Execute a job immediately by name.'''
    from podarcis.jobs import run_job

    res = run_job(ROOT_DIR, args.name, dry_run=args.dry_run)
    if res.get('status') == 'error':
        console.print(f'[bold red]Error:[/bold red] {res.get("message", "")}')
        return 1
    return 0


def cmd_job_logs(args: argparse.Namespace) -> int:
    '''Show journald output for a job.'''
    from podarcis.jobs import scheduler

    unit = f'{scheduler.unit_name(ROOT_DIR, args.name)}.service'
    return subprocess.run(
        ['journalctl', '--user', '-u', unit, '-n', str(args.lines), '--no-pager'],
    ).returncode


def cmd_open_tool() -> int:
    '''Open the configured frontend tool at the current directory.'''
    name = get_config_value(ROOT_DIR, 'frontend', default='vscode')
    cwd = str(ROOT_DIR)

    if name.lower() == 'vscode':
        _ensure_vscode_config(ROOT_DIR)

    command = FRONTENDS.get(name.lower(), name)
    if not command:
        return 0

    try:
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


def cmd_ingest(args: argparse.Namespace) -> int:
    '''Run the Google Drive delta ingestion job.'''
    from podarcis.jobs import run_job

    res = run_job(ROOT_DIR, 'gdrive_sync', dry_run=args.dry_run)
    return 0 if res.get('status') != 'error' else 1


def main() -> None:
    '''Parse arguments and dispatch to the handler each subparser declares.'''
    parser = argparse.ArgumentParser(
        prog='podarcis',
        description='Podarcis OKF v0.2 Research Agent engine & CLI configuration tool',
    )
    # pyproject.toml wins over installed dist metadata. AGENTS.md requires every
    # platform commit to bump it precisely so drift between instances is visible
    # here — but an editable install caches the version at install time, so
    # importlib.metadata kept reporting whatever was current when `pip install -e`
    # last ran, defeating the check it exists to serve.
    parser.add_argument(
        '-v', '--version', action='version',
        version=f'podarcis {load_version_info(ROOT_DIR)[0]}',
    )
    parser.add_argument('-i', '--interactive', action='store_true', help='Launch interactive TUI menu')
    parser.set_defaults(func=cmd_frontend)

    sub = parser.add_subparsers(dest='subcommand', title='Subcommands', help='Action to perform')

    def add(name, help_text, handler, *, parent=sub, **kwargs):
        '''Register a subparser bound to its handler.'''
        p = parent.add_parser(name, help=help_text, **kwargs)
        p.set_defaults(func=handler)
        return p

    # ── status ────────────────────────────────────────────────────────────
    add('status', 'Display status of MCP tool modules, skills, agents, jobs, and repos',
        cmd_status).add_argument('--json', action='store_true', help='Output status in JSON format')

    # ── job ───────────────────────────────────────────────────────────────
    job_p = add('job', 'Manage and execute scheduled jobs (.agents/jobs/*.yaml)', cmd_job_list)
    job_sub = job_p.add_subparsers(dest='job_action', help='Job action')
    add('list', 'List discovered jobs, schedules, and status', cmd_job_list, parent=job_sub) \
        .add_argument('--json', action='store_true', help='Output status in JSON format')
    jr = add('run', 'Execute job immediately by name', cmd_job_run, parent=job_sub)
    jr.add_argument('name', help='Job name (e.g. gdrive_sync, audit_wiki)')
    jr.add_argument('--dry-run', action='store_true', help='Preview execution without side effects')
    add('enable', 'Enable job and install its systemd timer', cmd_job_enable, parent=job_sub) \
        .add_argument('name', help='Job name')
    add('disable', 'Disable job and remove its systemd timer', cmd_job_disable, parent=job_sub) \
        .add_argument('name', help='Job name')
    jlog = add('logs', 'Show journald output for a job', cmd_job_logs, parent=job_sub)
    jlog.add_argument('name', help='Job name')
    jlog.add_argument('-n', '--lines', type=int, default=50, help='Lines to show')

    # ── repo ──────────────────────────────────────────────────────────────
    def add_repo_config(parent):
        '''The repo-configuration flags, shared by `repo config` and `config repo`.'''
        rc = add('repo' if parent is config_sub else 'config',
                 'Configure repository Git remotes or local paths',
                 cmd_config_repo, parent=parent)
        rc.add_argument('repo_name', nargs='?', help='Repository name (wiki, workspace, sources, …)')
        rc.add_argument('--url', help='Remote Git URL or repository path')
        rc.add_argument('--path', help='Local directory path or target path')
        rc.add_argument('--local', action='store_true', help='Set repository to local-only (no remote)')

    repo_p = add('repo', 'Manage and synchronize workspace repositories', cmd_repo_status,
                 aliases=['repos'])
    repo_sub = repo_p.add_subparsers(dest='repo_action', help='Repository action')
    add('status', 'Display Git and sync status across all workspace repositories',
        cmd_repo_status, parent=repo_sub) \
        .add_argument('--json', action='store_true', help='Output repository status in JSON format')
    add('sync', 'Synchronize workspace repositories (pull git remotes & ingest gdrive deltas)',
        cmd_repo_sync, parent=repo_sub, aliases=['pull'])
    rp = add('push', 'Push local commits to remotes for workspace repositories',
             cmd_repo_push, parent=repo_sub)
    rp.add_argument('--commit', '-c', action='store_true', help='Commit uncommitted local changes before pushing')
    rp.add_argument('--message', '-m', default='chore: sync workspace changes', help='Commit message')

    # ── config ────────────────────────────────────────────────────────────
    config_p = add('config', 'Configure components and repositories', cmd_interactive)
    config_sub = config_p.add_subparsers(dest='config_action', help='Config action')
    add('list', 'List status of components and repositories', cmd_status, parent=config_sub) \
        .add_argument('--json', action='store_true', help='Output status in JSON format')
    for verb, handler in (('enable', cmd_config_enable), ('disable', cmd_config_disable)):
        add(verb, f'{verb.capitalize()} an MCP tool module', handler, parent=config_sub) \
            .add_argument('name', help='Tool module name (skills and personas are not toggleable)')
    add_repo_config(config_sub)
    add_repo_config(repo_sub)
    add('frontend', 'Set the frontend tool (vscode, obsidian, none)',
        cmd_config_frontend, parent=config_sub) \
        .add_argument('frontend_name', choices=list(FRONTENDS),
                      metavar='{vscode,obsidian,none}', help='Frontend name')
    add('interactive', 'Launch interactive TUI menu', cmd_interactive, parent=config_sub)

    # ── lifecycle ─────────────────────────────────────────────────────────
    add('frontend', 'Open the configured frontend tool', cmd_frontend)
    add('install', 'Run bootstrap installer', cmd_install) \
        .add_argument('remaining_args', nargs=argparse.REMAINDER)
    add('clean', 'Clean Python build artifacts and cache files', cmd_clean)
    un = add('uninstall', 'Remove global symlink, virtualenv, and build artefacts', cmd_uninstall)
    un.add_argument('-y', '--yes', action='store_true', help='Skip all confirmations')
    un.add_argument('--dry-run', action='store_true', dest='dry_run', help='Preview without removing anything')
    un.add_argument('--purge', action='store_true', help='Also remove .podarcis/config.yaml')
    add('test', 'Run pytest suite', cmd_test).add_argument('remaining_args', nargs=argparse.REMAINDER)
    add('lint', 'Run link integrity check', cmd_lint).add_argument('remaining_args', nargs=argparse.REMAINDER)

    # ── diagnose ──────────────────────────────────────────────────────────
    diag = add('diagnose', 'Display current platform pain points and logged issues', cmd_diagnose)
    diag.add_argument('--json', action='store_true', help='Output issues in JSON format')
    diag.add_argument('--clear', action='store_true', help='Resolve every unresolved pain point')
    diag.add_argument('--resolve', type=str, metavar='ID', help='Mark a specific pain point ID as resolved')
    diag.add_argument('--resolve-category', type=str, metavar='CAT', help='Resolve every unresolved pain point in a category')
    diag.add_argument('--log-session', type=str, metavar='PATH', help='Parse and log pain points for a transcript file')

    # ── research ──────────────────────────────────────────────────────────
    research_p = add('research', 'Search peer-reviewed literature and ingest papers into sources/',
                     cmd_research)
    research_sub = research_p.add_subparsers(dest='research_action', help='Research action')
    r_search = add('search', 'Search literature across PubMed, OpenAlex, arXiv, and Semantic Scholar',
                   cmd_research, parent=research_sub)
    r_search.add_argument('query', help='Search query or topic')
    r_search.add_argument('--limit', type=int, default=5, help='Maximum results (default 5)')
    r_search.add_argument('--provider', default='all',
                          choices=['all', 'pubmed', 'openalex', 'arxiv', 'semanticscholar'],
                          help='Provider filter')
    r_search.add_argument('--json', action='store_true', help='Output search results in JSON format')
    r_ingest = add('ingest', 'Fetch PDF, extract text, and ingest paper into sources/literature/',
                   cmd_research, parent=research_sub)
    r_ingest.add_argument('paper_id', help='Paper ID (DOI:xxx, openalex:xxx, pmid:xxx, arXiv:xxx, or raw title/hash)')
    r_ingest.add_argument('--domain', required=True, help='Target domain directory under sources/literature/')
    r_ingest.add_argument('--name', help='Custom snake_case slug directory name')
    r_ingest.add_argument('--json', action='store_true', help='Output ingestion result in JSON format')

    # ── ingest ────────────────────────────────────────────────────────────
    ing = add('ingest', 'Run automated source ingestion (GDrive API delta check)', cmd_ingest)
    ing.add_argument('--dry-run', action='store_true', help='Scan deltas without modifying files')

    args = parser.parse_args()
    sys.exit(cmd_interactive(args) if args.interactive else args.func(args))


if __name__ == '__main__':
    main()
