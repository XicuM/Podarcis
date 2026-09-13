'''Three-column wiki workspace: files | editor | herdr agents.

tmux is the compositor. herdr is only the right pane (agent multiplexer),
not the outer UI.
'''

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

TMUX_SESSION = 'podarcis-wiki'
WINDOW = 'wiki'
PANE_FILES = 0
PANE_EDIT = 1
PANE_AGENT = 2


def resolve_tmux(*, environ: dict[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    hinted = (env.get('PODARCIS_TMUX') or env.get('TMUX_BIN') or '').strip()
    if hinted:
        path = Path(hinted).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path.resolve())
        return shutil.which(hinted)
    return shutil.which('tmux')


def pane_target(index: int) -> str:
    return f'{TMUX_SESSION}:{WINDOW}.{index}'


def has_session(tmux: str) -> bool:
    r = subprocess.run(
        [tmux, 'has-session', '-t', TMUX_SESSION],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return r.returncode == 0


def kill_session(tmux: str) -> None:
    subprocess.run(
        [tmux, 'kill-session', '-t', TMUX_SESSION],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _send_command(tmux: str, pane: int, argv: list[str]) -> None:
    subprocess.run(
        [tmux, 'send-keys', '-t', pane_target(pane), shlex.join(argv), 'C-m'],
        check=True,
    )


def _setenv(tmux: str, key: str, value: str) -> None:
    subprocess.run(
        [tmux, 'set-environment', '-t', TMUX_SESSION, key, value],
        check=True,
    )


def create_session(
    tmux: str,
    wiki_root: Path,
    *,
    files_argv: list[str] | None,
    edit_argv: list[str],
    herdr_argv: list[str],
    environ: dict[str, str],
) -> None:
    '''Create a detached 3-pane session. Left files, center editor, right herdr.'''
    subprocess.run(
        [
            tmux, 'new-session', '-d',
            '-s', TMUX_SESSION,
            '-n', WINDOW,
            '-c', str(wiki_root),
        ],
        check=True,
        env=environ,
    )
    for key in (
        'PROJECT_ROOT', 'HERDR_SESSION', 'HERDR_CONFIG_PATH',
        'YAZI_CONFIG_HOME', 'PATH',
    ):
        if key in environ:
            _setenv(tmux, key, environ[key])

    if files_argv:
        _send_command(tmux, PANE_FILES, files_argv)

    subprocess.run(
        [
            tmux, 'split-window', '-h',
            '-t', pane_target(PANE_FILES),
            '-c', str(wiki_root),
        ],
        check=True,
        env=environ,
    )
    _send_command(tmux, PANE_EDIT, edit_argv)

    subprocess.run(
        [
            tmux, 'split-window', '-h',
            '-t', pane_target(PANE_EDIT),
            '-c', str(wiki_root),
        ],
        check=True,
        env=environ,
    )
    _send_command(tmux, PANE_AGENT, herdr_argv)

    subprocess.run(
        [tmux, 'select-layout', '-t', f'{TMUX_SESSION}:{WINDOW}', 'even-horizontal'],
        check=True,
    )
    subprocess.run(
        [tmux, 'resize-pane', '-t', pane_target(PANE_FILES), '-x', '22%'],
        check=True,
    )
    subprocess.run(
        [tmux, 'resize-pane', '-t', pane_target(PANE_AGENT), '-x', '32%'],
        check=True,
    )
    subprocess.run(
        [tmux, 'select-pane', '-t', pane_target(PANE_EDIT)],
        check=True,
    )


def attach(tmux: str, environ: dict[str, str]) -> None:
    os.execvpe(tmux, [tmux, 'attach-session', '-t', TMUX_SESSION], environ)


def send_to_edit(tmux: str, keys: list[str]) -> None:
    '''Send key names to the center editor pane (`Escape`, `Enter`, literals).'''
    subprocess.run(
        [tmux, 'send-keys', '-t', pane_target(PANE_EDIT), *keys],
        check=True,
    )
