'''``podarcis wiki``: preflight, daemonize herdr, apply labelled layout, exec the client.'''

from __future__ import annotations

import argparse
import os
import shlex
from pathlib import Path

from rich.table import Table

from podarcis.console import console
from podarcis.herdr.layout import (
    apply_socket_layout,
    create_split_layout,
    editor_run_argv,
    files_run_argv,
    find_workspace,
    layout_tree,
    panes_by_label,
    wait_for_shell,
)
from podarcis.repos import get_repo_status, sync_repos_full
from podarcis.tui import AGENT_NAME, PANE_AGENT, PANE_EDIT, PANE_FILES, SESSION_NAME
from podarcis.tui.deps import ResolvedDeps, resolve_deps
from podarcis.tui.root import WikiRootError, find_wiki_root
from podarcis.tui.server import (
    HerdrSession,
    ensure_checkout_herdr,
    ensure_herdr_server,
    ensure_session_config,
    package_herdr_dir,
    resolved_herdr_dir,
    session_config,
    session_sock,
    write_linked_plugin,
)


def _die(msg: str) -> int:
    console.print(f'[bold red]Error:[/bold red] {msg}')
    return 1


def _debug(wiki_root: Path, msg: str) -> None:
    if os.environ.get('PODARCIS_TUI_DEBUG') != '1':
        return
    log = wiki_root / 'tmp' / 'tui' / 'launch.log'
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open('a', encoding='utf-8') as fh:
        fh.write(msg.rstrip() + '\n')


def _resolve_open_path(path: str, wiki_root: Path) -> Path:
    '''Treat ``path`` as a file to open, never as a checkout root.'''
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return candidate
    cwd_hit = Path.cwd() / candidate
    if cwd_hit.exists():
        return cwd_hit.resolve()
    return (wiki_root / candidate)


def _flavor_dir(deps: ResolvedDeps, wiki_root: Path) -> Path:
    return deps.flavor_dir or resolved_herdr_dir(wiki_root) or package_herdr_dir()


def _files_argv(deps: ResolvedDeps, wiki_root: Path) -> list[str] | None:
    return files_run_argv(
        deps.file_manager, deps.file_manager_name, wiki_root, _flavor_dir(deps, wiki_root),
    )


def _edit_argv(deps: ResolvedDeps, wiki_root: Path, open_path: Path | None) -> list[str]:
    return editor_run_argv(
        list(deps.editor or []), _flavor_dir(deps, wiki_root), open_path=open_path,
    )


def _link_plugin(session: HerdrSession, wiki_root: Path) -> None:
    herdr_dir = ensure_checkout_herdr(wiki_root)
    try:
        dest = write_linked_plugin(herdr_dir)
        session.cli('plugin', 'link', str(dest))
    except (RuntimeError, OSError) as exc:
        msg = str(exc).lower()
        if 'already' in msg or 'exists' in msg or 'linked' in msg:
            return
        console.print(f'[yellow]plugin.link failed: {exc}[/yellow]')


def _print_plan(
    wiki_root: Path,
    deps: ResolvedDeps,
    *,
    open_path: Path | None,
    statuses: list[dict],
    reset_config: bool,
    reset_layout: bool,
) -> None:
    console.print('[bold #29b8db]podarcis wiki --dry-run[/bold #29b8db]\n')
    console.print(f'  wiki root : {wiki_root}')
    console.print(f'  session   : {SESSION_NAME}')
    console.print(f'  herdr     : {deps.herdr or "(missing)"}')
    console.print(f'  editor    : {shlex.join(deps.editor) if deps.editor else "(missing)"}')
    console.print(f'  files     : {deps.file_manager_name or "shell"}')
    console.print(f'  harness   : {deps.harness or "(missing)"}')
    console.print(f'  sock      : {session_sock()}')
    console.print(f'  config    : {session_config()}')
    if reset_config:
        console.print('  config    : would copy session.toml template (--reset-config)')
    if reset_layout:
        console.print('  layout    : would apply labelled shells (--reset-layout)')

    tree = layout_tree(wiki_root)
    files_argv = _files_argv(deps, wiki_root)
    edit_argv = _edit_argv(deps, wiki_root, open_path)
    agent_argv = [
        deps.herdr or 'herdr', '--session', SESSION_NAME,
        'agent', 'start', AGENT_NAME, '--kind', deps.harness or '?',
        '--pane', '<agent-id>',
    ]

    table = Table(title='Intended layout', border_style='cyan', expand=True)
    table.add_column('label', style='bold white', no_wrap=True)
    table.add_column('argv', overflow='fold')
    table.add_column('env', style='dim', overflow='fold')
    env_s = ' '.join(f'{k}={v}' for k, v in deps.pane_env.items())
    table.add_row(
        PANE_FILES,
        shlex.join(files_argv) if files_argv else '(shell)',
        env_s,
    )
    table.add_row(PANE_EDIT, shlex.join(edit_argv) if edit_argv else '(missing editor)', env_s)
    table.add_row(PANE_AGENT, shlex.join(agent_argv), env_s)
    console.print(table)
    console.print(f'  [dim]layout tree tab_label={tree["tab_label"]} ratio={tree["root"]["ratio"]}[/dim]')

    repos = Table(title='Workspace repositories', border_style='cyan')
    repos.add_column('Repo', style='bold white')
    repos.add_column('Status')
    repos.add_column('Branch')
    repos.add_column('Changes', justify='right')
    repos.add_column('Ahead/Behind', justify='right')
    for s in statuses:
        st = s.get('status') or ''
        ab = f"+{s.get('ahead', 0)} / -{s.get('behind', 0)}" if (s.get('ahead') or s.get('behind')) else '—'
        repos.add_row(
            s.get('repo', ''),
            st,
            s.get('branch') or '—',
            str(s.get('changes') or 0),
            ab,
        )
    console.print(repos)
    for warning in deps.warnings:
        console.print(f'[yellow]{warning}[/yellow]')


def _preflight(wiki_root: Path, deps: ResolvedDeps, statuses: list[dict]) -> str | None:
    if not deps.herdr:
        return deps.herdr_missing_message
    if not deps.editor:
        return 'no editor found (set $PODARCIS_EDITOR / $EDITOR, or install helix/nvim/vi).'
    if not deps.harness:
        return 'no agent harness found (install opencode or claude).'
    missing = [s['repo'] for s in statuses if s.get('status') == 'missing']
    if missing:
        return (
            'repository missing: ' + ', '.join(missing)
            + '. Run `podarcis repo sync`.'
        )
    return None


def _herdr_env(wiki_root: Path, cfg: Path) -> dict[str, str]:
    env = os.environ.copy()
    env['HERDR_CONFIG_PATH'] = str(cfg)
    env['HERDR_SESSION'] = SESSION_NAME
    env['PROJECT_ROOT'] = str(wiki_root)
    return env


def _run_occupants(session: HerdrSession, panes: dict[str, str], files_argv: list[str] | None, edit_argv: list[str]) -> None:
    if files_argv:
        session.cli('pane', 'run', panes[PANE_FILES], shlex.join(files_argv))
    session.cli('pane', 'run', panes[PANE_EDIT], shlex.join(edit_argv))
    wait_for_shell(session.cli, panes[PANE_AGENT])


def _start_agent(session: HerdrSession, panes: dict[str, str], harness: str) -> None:
    session.cli(
        'agent', 'start', AGENT_NAME,
        '--kind', harness,
        '--pane', panes[PANE_AGENT],
    )


def _open_path_in_edit(
    session: HerdrSession,
    path: Path,
    *,
    editor: list[str],
    flavor_dir: Path,
) -> None:
    from podarcis.tui.actions.edit import open_in_edit_pane
    err = open_in_edit_pane(session, path, editor=editor, flavor_dir=flavor_dir)
    if err:
        console.print(f'[yellow]{err}[/yellow]')


def _complete_labels(session: HerdrSession, panes: dict[str, str]) -> dict[str, str]:
    '''Fill any labels layout.apply did not keep from ``pane list``.'''
    listed = panes_by_label(session.cli)
    for label in (PANE_FILES, PANE_EDIT, PANE_AGENT):
        if label not in panes and label in listed:
            panes[label] = listed[label]
        elif label in panes and label not in listed:
            session.cli('pane', 'rename', panes[label], label)
    return panes


def _rest_after(rest: list[str]) -> list[str]:
    if rest and rest[0] == '--':
        return rest[1:]
    return rest


def dispatch_wiki(args: argparse.Namespace) -> int:
    '''Route ``podarcis wiki {edit,persona,context}`` without a subparser (avoids flag clobber).'''
    rest = list(getattr(args, 'wiki_rest', None) or [])
    if rest:
        head, *tail = rest
        if head == 'edit':
            from podarcis.tui.actions.edit import cmd_wiki_edit
            path_parts = _rest_after(tail)
            if not path_parts:
                return _die('wiki edit requires a PATH (podarcis wiki edit -- PATH)')
            args.path = path_parts[0]
            return cmd_wiki_edit(args)
        if head == 'persona':
            from podarcis.tui.actions.spawn_persona import cmd_wiki_persona
            name_parts = _rest_after(tail)
            if not name_parts:
                return _die('wiki persona requires a NAME (podarcis wiki persona researcher)')
            args.name = name_parts[0]
            return cmd_wiki_persona(args)
        if head == 'context':
            from podarcis.tui.actions.edit import cmd_wiki_context
            path_parts = _rest_after(tail)
            if not path_parts:
                return _die('wiki context requires a PATH')
            args.path = path_parts[0]
            return cmd_wiki_context(args)
        path_parts = _rest_after(rest)
        args.path = path_parts[0] if path_parts else None
    return cmd_wiki(args)


def cmd_wiki(args: argparse.Namespace) -> int:
    '''Attach herdr session ``podarcis`` with a files | edit | agent layout.'''
    try:
        wiki_root = find_wiki_root(explicit=getattr(args, 'root', None))
    except WikiRootError as exc:
        return _die(getattr(exc, 'message', str(exc)))

    dry_run = bool(getattr(args, 'dry_run', False))
    reset_config = bool(getattr(args, 'reset_config', False))
    reset_layout = bool(getattr(args, 'reset_layout', False))
    do_sync = bool(getattr(args, 'sync', False))
    raw_path = getattr(args, 'path', None)

    deps = resolve_deps(wiki_root)
    open_path = _resolve_open_path(raw_path, wiki_root) if raw_path else None
    if open_path is not None and not dry_run:
        from podarcis.tui.context import write_current
        write_current(wiki_root, open_path)

    if do_sync and not dry_run:
        console.print('[bold #29b8db]Synchronizing workspace repositories...[/bold #29b8db]')
        sync_repos_full(wiki_root)

    statuses = get_repo_status(wiki_root)
    if dry_run:
        _print_plan(
            wiki_root, deps,
            open_path=open_path, statuses=statuses,
            reset_config=reset_config, reset_layout=reset_layout,
        )
        err = _preflight(wiki_root, deps, statuses)
        return _die(err) if err else 0

    err = _preflight(wiki_root, deps, statuses)
    if err:
        return _die(err)

    for warning in deps.warnings:
        console.print(f'[yellow]{warning}[/yellow]')

    assert deps.herdr and deps.editor and deps.harness
    _debug(wiki_root, f'wiki_root={wiki_root} herdr={deps.herdr} harness={deps.harness}')

    cfg = ensure_session_config(reset=reset_config)
    ensure_checkout_herdr(wiki_root)
    try:
        ensure_herdr_server(deps.herdr, sock=session_sock(), config=cfg)
    except RuntimeError as exc:
        return _die(str(exc))

    env = _herdr_env(wiki_root, cfg)
    session = HerdrSession(deps.herdr, env=env, sock=session_sock())
    files_argv = _files_argv(deps, wiki_root)
    edit_argv = _edit_argv(deps, wiki_root, open_path)
    _link_plugin(session, wiki_root)

    try:
        existing = find_workspace(session.cli)
        if existing and not reset_layout:
            session.cli('workspace', 'focus', existing['workspace_id'])
            if open_path is not None:
                _open_path_in_edit(
                    session, open_path,
                    editor=deps.editor, flavor_dir=_flavor_dir(deps, wiki_root),
                )
        else:
            if existing and reset_layout:
                panes = apply_socket_layout(
                    session.rpc, existing['workspace_id'], wiki_root,
                    tab_id=existing.get('active_tab_id'),
                    cli=session.cli,
                )
                panes = _complete_labels(session, panes)
                workspace_id = existing['workspace_id']
            else:
                panes = create_split_layout(session.cli, wiki_root)
                workspace_id = panes['workspace']
            for label in (PANE_FILES, PANE_EDIT, PANE_AGENT):
                if label not in panes:
                    return _die(f'layout is missing pane labelled {label}')
            _run_occupants(session, panes, files_argv, edit_argv)
            _start_agent(session, panes, deps.harness)
            session.cli('workspace', 'focus', workspace_id)
            _debug(wiki_root, f'panes={panes}')
    except RuntimeError as exc:
        return _die(str(exc))

    try:
        os.execvpe(deps.herdr, [deps.herdr, '--session', SESSION_NAME], env)
    except OSError as exc:
        return _die(f'execvp {deps.herdr} failed: {exc}')
    return _die(f'execvp {deps.herdr} failed')
