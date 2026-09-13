'''``podarcis wiki edit -- PATH``: write current-page context and target the edit pane.'''

from __future__ import annotations

import argparse
import os
import shlex
import sys
from pathlib import Path

from podarcis.console import console
from podarcis.herdr.layout import (
    editor_run_argv,
    foreground_occupant,
    is_helix,
    is_nvim,
    is_shell_foreground,
    panes_by_label,
)
from podarcis.tui import PANE_EDIT, SESSION_NAME
from podarcis.tui.context import write_current
from podarcis.tui.deps import resolve_deps
from podarcis.tui.root import WikiRootError, find_wiki_root
from podarcis.tui.server import HerdrSession, package_herdr_dir, session_sock
from podarcis.tui.session import in_wiki_session


def vim_escape(path: str) -> str:
    return (
        str(path)
        .replace('\\', '\\\\')
        .replace(' ', '\\ ')
        .replace('|', '\\|')
        .replace('"', '\\"')
    )


def resolve_edit_path(path: str, wiki_root: Path) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return candidate
    cwd_hit = Path.cwd() / candidate
    if cwd_hit.exists():
        return cwd_hit.resolve()
    hit = wiki_root / candidate
    return hit.resolve() if hit.exists() else hit


def open_in_edit_pane(
    session: HerdrSession,
    path: Path,
    *,
    editor: list[str],
    flavor_dir: Path,
) -> str | None:
    '''Target the pane labelled ``edit``. Return None on success, else an error.'''
    panes = panes_by_label(session.cli)
    edit_id = panes.get(PANE_EDIT)
    if not edit_id:
        return 'edit pane not found; focus it or open the file yourself.'
    info = session.cli('pane', 'process-info', '--pane', edit_id)
    if is_shell_foreground(info):
        argv = editor_run_argv(editor, flavor_dir, open_path=path)
        session.cli('pane', 'run', edit_id, shlex.join(argv))
        return None
    occupant = foreground_occupant(info)
    if is_nvim(occupant):
        # Always esc first: nvim may be in insert mode.
        session.cli('pane', 'send-keys', edit_id, 'esc')
        session.cli('pane', 'send-text', edit_id, f':e {vim_escape(str(path))}')
        session.cli('pane', 'send-keys', edit_id, 'enter')
        return None
    if is_helix(occupant):
        session.cli('pane', 'send-keys', edit_id, 'esc')
        session.cli('pane', 'send-text', edit_id, f':open {path}')
        session.cli('pane', 'send-keys', edit_id, 'enter')
        return None
    name = occupant or 'unknown'
    return f'edit pane is busy (process {name}); focus it or quit the occupant.'


def cmd_wiki_edit(args: argparse.Namespace) -> int:
    try:
        wiki_root = find_wiki_root(explicit=getattr(args, 'root', None))
    except WikiRootError as exc:
        console.print(f'[bold red]Error:[/bold red] {getattr(exc, "message", str(exc))}')
        return 1
    raw = getattr(args, 'path', None)
    if not raw:
        console.print('[bold red]Error:[/bold red] wiki edit requires a PATH (podarcis wiki edit -- PATH)')
        return 1
    path = resolve_edit_path(str(raw), wiki_root)
    write_current(wiki_root, path)

    deps = resolve_deps(wiki_root)
    if not deps.herdr:
        console.print(f'[bold red]Error:[/bold red] {deps.herdr_missing_message}')
        return 1
    if not session_sock().exists():
        console.print(
            '[bold red]Error:[/bold red] herdr session podarcis is not running; '
            'launch `podarcis wiki` first.'
        )
        return 1
    if not deps.editor:
        console.print('[bold red]Error:[/bold red] no editor found.')
        return 1

    env = os.environ.copy()
    env['HERDR_SESSION'] = SESSION_NAME
    env['PROJECT_ROOT'] = str(wiki_root)
    session = HerdrSession(deps.herdr, env=env, sock=session_sock())
    flavor = deps.flavor_dir or package_herdr_dir()
    err = open_in_edit_pane(session, path, editor=deps.editor, flavor_dir=flavor)
    if err:
        console.print(f'[yellow]{err}[/yellow]')
        return 1
    return 0


def cmd_wiki_context(args: argparse.Namespace) -> int:
    try:
        wiki_root = find_wiki_root(explicit=getattr(args, 'root', None))
    except WikiRootError as exc:
        console.print(f'[bold red]Error:[/bold red] {getattr(exc, "message", str(exc))}')
        return 1
    raw = getattr(args, 'path', None)
    if not raw:
        console.print('[bold red]Error:[/bold red] wiki context requires a PATH')
        return 1
    write_current(wiki_root, raw)
    return 0


def main(argv: list[str] | None = None) -> int:
    if not in_wiki_session():
        return 0
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == '--':
        argv = argv[1:]
    ns = argparse.Namespace(path=argv[0] if argv else None, root=os.environ.get('PROJECT_ROOT'))
    return cmd_wiki_edit(ns)


if __name__ == '__main__':
    raise SystemExit(main())
