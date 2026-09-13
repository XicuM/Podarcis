'''Open a path in the herdr pane labelled ``edit``. Shared by overlays and ``podarcis wiki edit``.'''

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

from podarcis.herdr.layout import is_shell_foreground, panes_by_label
from podarcis.tui import PANE_EDIT, SESSION_NAME
from podarcis.tui.deps import resolve_deps, resolve_herdr
from podarcis.tui.root import find_wiki_root
from podarcis.tui.server import HerdrSession, session_config, session_sock


def _proc_name(result: dict) -> str:
    info = result.get('process_info', result)
    procs = info.get('foreground_processes') or []
    if not procs:
        return ''
    raw = procs[0].get('name') or procs[0].get('argv0') or ''
    return Path(str(raw)).name.lstrip('-').lower()


def _resolve_path(path: str | Path, wiki_root: Path) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return candidate
    cwd_hit = Path.cwd() / candidate
    if cwd_hit.exists():
        return cwd_hit.resolve()
    return (wiki_root / candidate).resolve()


def _herdr_bin() -> str | None:
    for key in ('HERDR_BIN_PATH', 'HERDR_BIN'):
        val = (os.environ.get(key) or '').strip()
        if val:
            return val
    return resolve_herdr()


def _send_open(session: HerdrSession, pane_id: str, editor_cmd: str, path: Path) -> None:
    session.cli('pane', 'send-keys', pane_id, 'esc')
    session.cli('pane', 'send-text', pane_id, f'{editor_cmd} {path}')
    session.cli('pane', 'send-keys', pane_id, 'enter')


def open_in_edit_pane(wiki_root: Path, path: Path) -> int:
    '''Target the labelled edit pane. Fallback: tell the user to focus it.'''
    herdr = _herdr_bin()
    if not herdr:
        print(f'focus the edit pane and open {path}', file=sys.stderr)
        return 1
    env = os.environ.copy()
    env.setdefault('HERDR_SESSION', SESSION_NAME)
    env.setdefault('HERDR_CONFIG_PATH', str(session_config()))
    session = HerdrSession(herdr, env=env, sock=session_sock())
    try:
        panes = panes_by_label(session.cli)
    except Exception as exc:
        print(f'focus the edit pane and open {path} ({exc})', file=sys.stderr)
        return 1
    edit_id = panes.get(PANE_EDIT)
    if not edit_id:
        print(f'edit pane not found; focus it and open {path}', file=sys.stderr)
        return 1
    deps = resolve_deps(wiki_root)
    editor = list(deps.editor or [])
    try:
        info = session.cli('pane', 'process-info', '--pane', edit_id)
    except Exception as exc:
        print(f'focus the edit pane and open {path} ({exc})', file=sys.stderr)
        return 1
    if is_shell_foreground(info) and editor:
        session.cli('pane', 'run', edit_id, shlex.join([*editor, str(path)]))
        return 0
    name = _proc_name(info)
    if name in {'nvim', 'vim', 'vi'}:
        _send_open(session, edit_id, ':e', path)
        return 0
    if name in {'helix', 'hx'}:
        _send_open(session, edit_id, ':open', path)
        return 0
    occupant = name or 'unknown'
    print(
        f'edit pane is busy (process {occupant}); focus it or quit the occupant, then open {path}',
        file=sys.stderr,
    )
    return 1


def open_page(wiki_root: Path, rel_or_path: str | Path) -> int:
    path = _resolve_path(rel_or_path, wiki_root)
    print(path)
    return open_in_edit_pane(wiki_root, path)


def cmd_wiki_edit(path: str, *, root: str | None = None) -> int:
    wiki_root = find_wiki_root(explicit=root)
    return open_page(wiki_root, path)
