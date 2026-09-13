'''Right-pane herdr companion: agent list over the socket, not a nested herdr TUI.

Enter attaches the herdr client for that conversation (same as companion
showing the master terminal), then returns to the list when herdr detaches.
'''

from __future__ import annotations

import os
import select
import subprocess
import sys
import termios
import tty
from pathlib import Path

from podarcis.tui import PERSONAS, SESSION_NAME
from podarcis.tui.companion import (
    conversations,
    focus_conversation,
    snapshot,
    start_conversation,
)
from podarcis.tui.deps import resolve_deps
from podarcis.tui.server import HerdrSession, session_config, session_sock
from podarcis.tui.session import in_wiki_session

HELP = 'j/k select  n new  1-4 persona  enter attach  r refresh  q quit'


def _wiki_root() -> Path:
    raw = os.environ.get('PROJECT_ROOT') or os.getcwd()
    return Path(raw).resolve()


def _session(herdr: str, wiki_root: Path) -> HerdrSession:
    env = os.environ.copy()
    env['HERDR_SESSION'] = SESSION_NAME
    env['PROJECT_ROOT'] = str(wiki_root)
    cfg = session_config()
    if cfg.is_file():
        env['HERDR_CONFIG_PATH'] = str(cfg)
    return HerdrSession(herdr, env=env, sock=session_sock())


def _read_key(timeout_s: float = 1.0) -> str | None:
    fd = sys.stdin.fileno()
    ready, _, _ = select.select([sys.stdin], [], [], timeout_s)
    if not ready:
        return None
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == '\x1b':
            extra = sys.stdin.read(2)
            ch += extra
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _draw(convs, selected: int, status: str) -> None:
    sys.stdout.write('\033[2J\033[H')
    sys.stdout.write('herdr companion  session=%s\n' % SESSION_NAME)
    sys.stdout.write('%s\n\n' % (status or HELP))
    if not convs:
        sys.stdout.write('  (no conversations — press n to start one)\n')
    for i, conv in enumerate(convs):
        mark = '>' if i == selected else ' '
        emoji = (conv.emoji + ' ') if conv.emoji else ''
        sys.stdout.write(f' {mark} {emoji}{conv.label}  [{conv.status}]\n')
    sys.stdout.write('\n' + HELP + '\n')
    sys.stdout.flush()


def _attach(herdr: str, session: HerdrSession, conv) -> None:
    focus_conversation(session, conv)
    env = session.env
    subprocess.run(
        [herdr, '--session', SESSION_NAME],
        env=env,
        check=False,
    )


def main(argv: list[str] | None = None) -> int:
    wiki_root = _wiki_root()
    deps = resolve_deps(wiki_root)
    if not deps.herdr:
        print(deps.herdr_missing_message, file=sys.stderr)
        return 1
    session = _session(deps.herdr, wiki_root)
    selected = 0
    notice = ''
    if not in_wiki_session() and not session_sock().exists():
        print('herdr session podarcis is not running; start `podarcis wiki` first.', file=sys.stderr)
        return 1

    while True:
        try:
            snap = snapshot(session)
            convs = conversations(snap, wiki_root)
        except Exception as exc:
            convs = []
            notice = f'socket: {exc}'
        if convs:
            selected = max(0, min(selected, len(convs) - 1))
        else:
            selected = 0
        _draw(convs, selected, notice)
        notice = ''
        key = _read_key(1.0)
        if key is None:
            continue
        if key in ('q', 'Q', '\x03'):
            return 0
        if key in ('r', 'R'):
            continue
        if key in ('j', '\x1b[B') and convs:
            selected = min(selected + 1, len(convs) - 1)
        elif key in ('k', '\x1b[A') and convs:
            selected = max(selected - 1, 0)
        elif key in ('n', 'N'):
            kind = deps.harness or 'opencode'
            try:
                start_conversation(session, wiki_root, kind)
            except Exception as exc:
                notice = str(exc)
        elif key in ('1', '2', '3', '4'):
            name = PERSONAS[int(key) - 1]
            kind = deps.harness or 'opencode'
            args = ['--agent', name] if kind == 'claude' else None
            try:
                start_conversation(session, wiki_root, kind, name=name, args=args)
            except Exception as exc:
                notice = str(exc)
        elif key in ('\r', '\n') and convs:
            _attach(deps.herdr, session, convs[selected])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
