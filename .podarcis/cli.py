#!/usr/bin/env python3
'''Python surface of the Podarcis CLI.

The Rust `podarcis` binary owns the command tree; the core subcommands
(status, config, repo, lint, diagnose, test, build, tui, clean) run
natively on the shared library. What remains here is the engine-bound work
with no Rust home yet — install/uninstall lifecycle, research/ingest HTTP and
pdf service, systemd job timers, and the interactive menu — reachable through
`python -m podarcis.cli <…>` dispatched by the Rust CLI.
'''

import argparse
import json
import subprocess
import sys
from pathlib import Path

from podarcis import ROOT_DIR
from podarcis.common import load_version_info
from podarcis.console import console
from podarcis.repos import push_repos


def _print_repo_results(res: dict, symbols: dict[str, str]) -> None:
    '''Print the `{repo: status}` mapping from repos.py with per-status symbols.'''
    for repo, status in res.items():
        print(f"  {symbols.get(status, '•')} {repo:<15} {status}")
    console.print()


def cmd_repo_push(args: argparse.Namespace) -> int:
    '''Push local commits to the configured remotes.'''
    console.print('[bold #29b8db]Pushing local workspace changes to remotes...[/bold #29b8db]\n')
    _print_repo_results(
        push_repos(
            ROOT_DIR,
            auto_commit=args.commit,
            message=args.message,
            audit=getattr(args, 'audit', False),
        ),
        {'ok': '[green]✓[/green]', 'skipped': '[dim]—[/dim]'},
    )
    return 0


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
            id_str = pid if pid else (f'DOI:{doi}' if doi else (f'arXiv:{arxiv}' if arxiv else (f'pmid:{pmid}' if pmid else 'N/A')))
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


def cmd_ingest(args: argparse.Namespace) -> int:
    '''Run the Google Drive delta ingestion job.'''
    from podarcis.jobs import run_job

    res = run_job(ROOT_DIR, 'gdrive_sync', dry_run=args.dry_run)
    return 0 if res.get('status') != 'error' else 1


def cmd_project(args: argparse.Namespace) -> int:
    '''Manage research projects.'''
    from podarcis.project import (
        list_projects, get_active_project_name, set_active_project,
        new_project, register_project, unregister_project,
        migrate_checkout_to_projects_dir, get_projects_dir
    )
    action = getattr(args, 'project_action', 'list') or 'list'

    if action == 'list':
        projects = list_projects()
        if getattr(args, 'json', False):
            print(json.dumps(projects, indent=2))
            return 0
        console.print('[bold #29b8db]Podarcis Research Projects:[/bold #29b8db]\n')
        active = get_active_project_name()
        for name, info in sorted(projects.items()):
            mark = '*' if name == active else ' '
            status = 'ready' if info.get('exists') else 'missing'
            console.print(f"  {mark} [bold]{name:<16}[/bold] [{status:<7}]  {info.get('path', ''):<35}  [dim]{info.get('description', '')}[/dim]")
        console.print(f'\nActive project:     [green]{active}[/green]')
        console.print(f'Projects directory: [dim]{get_projects_dir()}[/dim]\n')
        return 0

    elif action == 'current':
        active = get_active_project_name()
        projects = list_projects()
        p = projects.get(active, {})
        console.print(f"{active} ({p.get('path', 'not registered')})")
        return 0

    elif action == 'switch':
        name = args.name
        try:
            set_active_project(name)
            console.print(f'[bold green]✓ Switched active project to "{name}".[/bold green]')
            return 0
        except Exception as e:
            console.print(f'[bold red]Error:[/bold red] {e}')
            return 1

    elif action == 'new':
        try:
            proj = new_project(
                name=args.name,
                path=getattr(args, 'path', None),
                description=getattr(args, 'description', '') or '',
                wiki_remote=getattr(args, 'wiki_remote', '') or '',
                workspace_remote=getattr(args, 'workspace_remote', '') or '',
                sources_remote=getattr(args, 'sources_remote', '') or '',
                sources_backend=getattr(args, 'sources_backend', 'local') or 'local',
            )
            console.print(f'[bold green]✓ Initialized project "{proj.name}" at {proj.root}[/bold green]')
            return 0
        except Exception as e:
            console.print(f'[bold red]Error:[/bold red] {e}')
            return 1

    elif action == 'add':
        try:
            path = Path(args.path).resolve()
            name = getattr(args, 'name', None) or path.name
            register_project(name, path, description=getattr(args, 'description', '') or '', set_active=True)
            console.print(f'[bold green]✓ Registered project "{name}" at {path}[/bold green]')
            return 0
        except Exception as e:
            console.print(f'[bold red]Error:[/bold red] {e}')
            return 1

    elif action == 'remove':
        try:
            unregister_project(args.name, purge=getattr(args, 'purge', False))
            console.print(f'[bold green]✓ Removed project "{args.name}".[/bold green]')
            return 0
        except Exception as e:
            console.print(f'[bold red]Error:[/bold red] {e}')
            return 1

    elif action == 'migrate':
        try:
            name = getattr(args, 'name', 'default') or 'default'
            proj = migrate_checkout_to_projects_dir(ROOT_DIR, project_name=name)
            console.print(f'[bold green]✓ Migration complete. Project "{proj.name}" is now active at {proj.root}[/bold green]')
            return 0
        except Exception as e:
            console.print(f'[bold red]Error:[/bold red] {e}')
            return 1

    return 0


def _unsupported(_args: argparse.Namespace) -> int:
    '''Core subcommands are Rust now; this Python parser no longer knows them.'''
    console.print('[bold yellow]This command lives in the Rust `podarcis` CLI.[/bold yellow]')
    return 1


def main(argv: list[str] | None = None) -> int:
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
    parser.add_argument('-p', '--project', help='Target project name or directory path')
    parser.add_argument('--root', help='Podarcis checkout root (AGENTS.md + .podarcis/config.yaml or podarcis.yaml)')

    sub = parser.add_subparsers(dest='subcommand', title='Subcommands', help='Action to perform')
    parser.set_defaults(func=_unsupported)

    def add(name, help_text, handler, *, parent=sub, **kwargs):
        '''Register a subparser bound to its handler.'''
        p = parent.add_parser(name, help=help_text, **kwargs)
        p.set_defaults(func=handler)
        return p

    # ── project ───────────────────────────────────────────────────────────
    proj_p = add('project', 'Manage research projects (workspaces with wiki, workspace, sources)', cmd_project)
    proj_sub = proj_p.add_subparsers(dest='project_action', help='Project action')
    add('list', 'List registered projects', cmd_project, parent=proj_sub) \
        .add_argument('--json', action='store_true', help='Output in JSON format')
    add('current', 'Show active project', cmd_project, parent=proj_sub)
    add('switch', 'Switch active project', cmd_project, parent=proj_sub) \
        .add_argument('name', help='Project name')
    p_new = add('new', 'Create a new project workspace', cmd_project, parent=proj_sub)
    p_new.add_argument('name', help='Project name')
    p_new.add_argument('--path', help='Directory path (defaults to ~/.local/share/podarcis/projects/<name>)')
    p_new.add_argument('--description', default='', help='Project description')
    p_new.add_argument('--wiki-remote', default='', help='Remote Git URL for wiki')
    p_new.add_argument('--workspace-remote', default='', help='Remote Git URL for workspace')
    p_new.add_argument('--sources-remote', default='', help='Remote Git URL for sources')
    p_new.add_argument('--sources-backend', default='local', choices=['local', 'gdrive'], help='Sources backend')
    p_add = add('add', 'Register an existing directory as a project', cmd_project, parent=proj_sub)
    p_add.add_argument('path', help='Project directory path')
    p_add.add_argument('--name', help='Custom project name')
    p_add.add_argument('--description', default='', help='Project description')
    p_rm = add('remove', 'Unregister a project', cmd_project, parent=proj_sub)
    p_rm.add_argument('name', help='Project name')
    p_rm.add_argument('--purge', action='store_true', help='Also delete project directory on disk')
    p_mig = add('migrate', 'Migrate current checkout into ~/.local/share/podarcis/projects/<name>', cmd_project, parent=proj_sub)
    p_mig.add_argument('name', nargs='?', default='default', help='Target project name')

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
    # Only `push` remains on the Python engine; the rest is the Rust CLI.
    repo_p = add('repo', 'Manage and synchronize workspace repositories', _unsupported,
                 aliases=['repos'])
    repo_sub = repo_p.add_subparsers(dest='repo_action', help='Repository action')
    rp = add('push', 'Push local commits to remotes for workspace repositories',
             cmd_repo_push, parent=repo_sub)
    rp.add_argument('--commit', '-c', action='store_true', help='Commit uncommitted local changes before pushing')
    rp.add_argument(
        '--audit', action='store_true',
        help='Lint-gate: with --commit, lint then commit; without, lint and refuse a dirty/failing tree, then push existing commits',
    )
    rp.add_argument('--message', '-m', default='chore: sync workspace changes', help='Commit message')

    # ── config (interactive only) ─────────────────────────────────────────
    config_p = add('config', 'Configure components and repositories', cmd_interactive)
    config_sub = config_p.add_subparsers(dest='config_action', help='Config action')
    add('interactive', 'Launch interactive TUI menu', cmd_interactive, parent=config_sub)

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

    # ── lifecycle ─────────────────────────────────────────────────────────
    add('install', 'Run bootstrap installer', cmd_install) \
        .add_argument('remaining_args', nargs=argparse.REMAINDER)
    un = add('uninstall', 'Remove global symlink, virtualenv, and build artefacts', cmd_uninstall)
    un.add_argument('-y', '--yes', action='store_true', help='Skip all confirmations')
    un.add_argument('--dry-run', action='store_true', dest='dry_run', help='Preview without removing anything')
    un.add_argument('--purge', action='store_true', help='Also remove .podarcis/config.yaml')

    argv = list(sys.argv[1:] if argv is None else argv)

    # `argparse.REMAINDER` only starts collecting after a non-flag token, so
    # `podarcis install --xyz` would otherwise fail at the top-level parser.
    args, unknown = parser.parse_known_args(argv)
    if unknown:
        if not hasattr(args, 'remaining_args'):
            parser.error(f'unrecognized arguments: {" ".join(unknown)}')
        args.remaining_args = list(args.remaining_args or []) + unknown

    return cmd_interactive(args) if args.interactive else args.func(args)


if __name__ == '__main__':
    sys.exit(main())