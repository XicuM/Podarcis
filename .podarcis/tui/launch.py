'''``podarcis wiki``: tmux files | editor | herdr-agents. herdr is the right pane only.'''

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
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
from podarcis.tui.keys import merge_overlay_keys, overlay_key_blocks, parse_key_blocks
from podarcis.tui.metadata import report_repo_metadata
from podarcis.tui.root import WikiRootError, find_wiki_root
from podarcis.tui.server import (
    ensure_checkout_herdr,
    ensure_herdr_server,
    ensure_session_config,
    package_herdr_dir,
    resolved_herdr_dir,
    session_config,
    session_sock,
)
from podarcis.tui.tmux_layout import (
    TMUX_SESSION,
    attach as tmux_attach,
    create_session as tmux_create,
    has_session as tmux_has_session,
    kill_session as tmux_kill,
    pane_count as tmux_pane_count,
    resolve_tmux,
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


def _agents_panel_argv() -> list[str]:
    '''Right pane is the companion list, not the herdr TUI.'''
    prog = sys.argv[0] if sys.argv else 'podarcis'
    return [prog, 'wiki', 'agents']


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
    tmux = resolve_tmux()
    console.print(f'  wiki root : {wiki_root}')
    console.print(f'  compositor: tmux ({tmux or "missing"}) session {TMUX_SESSION}')
    console.print(f'  herdr     : {deps.herdr or "(missing)"}  (sidecar daemon, session {SESSION_NAME})')
    console.print(f'  editor    : {shlex.join(deps.editor) if deps.editor else "(missing)"}')
    console.print(f'  files     : {deps.file_manager_name or "shell"}')
    console.print(f'  harness   : {deps.harness or "(missing)"}')
    console.print(f'  sock      : {session_sock()}')
    console.print(f'  config    : {session_config()}')
    if reset_config:
        console.print('  config    : would copy session.toml template (--reset-config)')
    if reset_layout:
        console.print('  layout    : would apply labelled shells (--reset-layout)')

    files_argv = _files_argv(deps, wiki_root)
    edit_argv = _edit_argv(deps, wiki_root, open_path)
    agents_argv = _agents_panel_argv()
    env_s = ' '.join(f'{k}={v}' for k, v in deps.pane_env.items())
    console.print('Intended tmux columns (files | editor | herdr companion):')
    console.print(f'  files    : {shlex.join(files_argv) if files_argv else "(shell at wiki root)"}')
    console.print(f'  editor   : {shlex.join(edit_argv) if edit_argv else "(missing editor)"}')
    console.print(f'  companion: {shlex.join(agents_argv)}')
    if env_s:
        console.print(f'  env    : {env_s}')
    if reset_layout:
        console.print('  layout : would kill and recreate the tmux session (--reset-layout)')

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
    live_cfg = session_config()
    live_blocks = parse_key_blocks(live_cfg.read_text(encoding='utf-8')) if live_cfg.is_file() else []
    bound = {b.get('key'): b for b in live_blocks if b.get('key')}
    console.print('[bold]Overlay keys[/bold]')
    for spec in overlay_key_blocks(python_bin=sys.executable or 'python3'):
        live = bound.get(spec['key'])
        if live:
            console.print(
                f"  {spec['key']}: {live.get('type') or ''}  {live.get('command') or ''}"
            )
        else:
            console.print(
                f"  {spec['key']} (will merge): {spec['type']}  {spec['command']}"
            )
    for warning in deps.warnings:
        console.print(f'[yellow]{warning}[/yellow]')


def _preflight(wiki_root: Path, deps: ResolvedDeps, statuses: list[dict]) -> str | None:
    if not resolve_tmux():
        return 'tmux not found on PATH (needed as the files|editor|herdr compositor).'
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
        if head == 'agents':
            from podarcis.tui.agents_panel import main as agents_main
            return agents_main()
        if head == 'search':
            from podarcis.cli import cmd_wiki_search
            search_args = argparse.Namespace()
            rest_q = _rest_after(tail)
            search_args.query = rest_q
            search_args.json = '--json' in rest_q
            search_args.collection = 'wiki'
            search_args.method = 'hybrid'
            search_args.limit = 20
            search_args.no_rerank = False
            search_args.root = getattr(args, 'root', None)
            # pull flags out of remainder
            query_tokens = []
            i = 0
            while i < len(rest_q):
                tok = rest_q[i]
                if tok == '--json':
                    search_args.json = True
                elif tok == '--no-rerank':
                    search_args.no_rerank = True
                elif tok == '--collection' and i + 1 < len(rest_q):
                    i += 1
                    search_args.collection = rest_q[i]
                elif tok == '--method' and i + 1 < len(rest_q):
                    i += 1
                    search_args.method = rest_q[i]
                elif tok == '--limit' and i + 1 < len(rest_q):
                    i += 1
                    search_args.limit = int(rest_q[i])
                elif tok == '--root' and i + 1 < len(rest_q):
                    i += 1
                    search_args.root = rest_q[i]
                else:
                    query_tokens.append(tok)
                i += 1
            search_args.query = query_tokens
            if not search_args.query:
                return _die('wiki search requires a QUERY')
            return cmd_wiki_search(search_args)
        path_parts = _rest_after(rest)
        args.path = path_parts[0] if path_parts else None
    return cmd_wiki(args)


def cmd_wiki(args: argparse.Namespace) -> int:
    '''Open files | editor | herdr-agents in tmux. herdr is not the outer UI.'''
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
    tmux = resolve_tmux()
    assert tmux
    _debug(wiki_root, f'wiki_root={wiki_root} tmux={tmux} herdr={deps.herdr}')

    cfg = ensure_session_config(reset=reset_config)
    merge_overlay_keys(cfg)
    ensure_checkout_herdr(wiki_root)
    try:
        ensure_herdr_server(deps.herdr, sock=session_sock(), config=cfg)
    except RuntimeError as exc:
        return _die(str(exc))

    env = _herdr_env(wiki_root, cfg)
    flavor = _flavor_dir(deps, wiki_root)
    if deps.file_manager_name == 'yazi':
        from podarcis.herdr.layout import yazi_flavor_dir
        cfg_home = yazi_flavor_dir(flavor)
        if cfg_home is not None:
            env['YAZI_CONFIG_HOME'] = str(cfg_home)
    files_argv = _files_argv(deps, wiki_root)
    edit_argv = _edit_argv(deps, wiki_root, open_path)
    herdr_argv = _agents_panel_argv()

    if tmux_has_session(tmux) and (reset_layout or tmux_pane_count(tmux) != 3):
        tmux_kill(tmux)

    try:
        if not tmux_has_session(tmux):
            tmux_create(
                tmux, wiki_root,
                files_argv=files_argv,
                edit_argv=edit_argv,
                herdr_argv=herdr_argv,
                environ=env,
            )
        elif open_path is not None:
            from podarcis.tui.actions.edit import open_in_tmux_edit
            err = open_in_tmux_edit(tmux, open_path, editor=deps.editor)
            if err:
                console.print(f'[yellow]{err}[/yellow]')
    except (RuntimeError, subprocess.CalledProcessError, OSError) as exc:
        return _die(str(exc))

    try:
        tmux_attach(tmux, env)
    except OSError as exc:
        return _die(f'execvp tmux failed: {exc}')
    return _die('execvp tmux failed')
