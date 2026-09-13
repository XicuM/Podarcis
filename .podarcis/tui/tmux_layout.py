'''Three-column wiki workspace: files | editor | herdr agents.

tmux is the compositor. herdr is only the right pane (agent multiplexer),
not the outer UI.

Pane indexes are never used: user configs often set pane-base-index 1, so
``session:window.0`` fails with ``can't find pane: 0``. Commands are the
pane process (new-session/split-window shell-command). Later targeting uses
``#{pane_id}`` sorted by ``#{pane_left}``.
'''

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

TMUX_SESSION = 'podarcis-wiki'
WINDOW = 'wiki'
ENV_KEYS = (
    'PROJECT_ROOT', 'HERDR_SESSION', 'HERDR_CONFIG_PATH',
    'YAZI_CONFIG_HOME', 'PATH',
)


def resolve_tmux(*, environ: dict[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    hinted = (env.get('PODARCIS_TMUX') or env.get('TMUX_BIN') or '').strip()
    if hinted:
        path = Path(hinted).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path.resolve())
        return shutil.which(hinted)
    return shutil.which('tmux')


def window_target() -> str:
    return f'{TMUX_SESSION}:{WINDOW}'


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


def panes_left_to_right(tmux: str) -> list[str]:
    '''Unique pane ids (``%N``) from left to right. Independent of pane-base-index.'''
    r = subprocess.run(
        [tmux, 'list-panes', '-t', window_target(), '-F', '#{pane_left} #{pane_id}'],
        capture_output=True,
        text=True,
        check=True,
    )
    rows: list[tuple[int, str]] = []
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        rows.append((int(parts[0]), parts[1]))
    rows.sort()
    return [pid for _, pid in rows]


def pane_count(tmux: str) -> int:
    if not has_session(tmux):
        return 0
    try:
        return len(panes_left_to_right(tmux))
    except subprocess.CalledProcessError:
        return 0


def _env_flags(environ: dict[str, str]) -> list[str]:
    flags: list[str] = []
    for key in ENV_KEYS:
        if key in environ:
            flags.extend(['-e', f'{key}={environ[key]}'])
    return flags


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
    cwd = str(wiki_root)
    flags = _env_flags(environ)
    files_cmd = shlex.join(files_argv) if files_argv else (environ.get('SHELL') or '/bin/bash')
    wt = window_target()
    try:
        subprocess.run(
            [
                tmux, 'new-session', '-d',
                '-s', TMUX_SESSION, '-n', WINDOW, '-c', cwd,
                *flags,
                files_cmd,
            ],
            check=True,
            env=environ,
        )
        subprocess.run(
            [
                tmux, 'split-window', '-h', '-t', wt, '-c', cwd,
                *flags,
                shlex.join(edit_argv),
            ],
            check=True,
            env=environ,
        )
        subprocess.run(
            [
                tmux, 'split-window', '-h', '-t', wt, '-c', cwd,
                *flags,
                shlex.join(herdr_argv),
            ],
            check=True,
            env=environ,
        )
        subprocess.run(
            [tmux, 'select-layout', '-t', wt, 'even-horizontal'],
            check=True,
        )
        ids = panes_left_to_right(tmux)
        if len(ids) >= 3:
            subprocess.run([tmux, 'resize-pane', '-t', ids[0], '-x', '22%'], check=True)
            subprocess.run([tmux, 'resize-pane', '-t', ids[2], '-x', '32%'], check=True)
            subprocess.run([tmux, 'select-pane', '-t', ids[1]], check=True)
    except Exception:
        kill_session(tmux)
        raise


def attach(tmux: str, environ: dict[str, str]) -> None:
    os.execvpe(tmux, [tmux, 'attach-session', '-t', TMUX_SESSION], environ)


def send_to_edit(tmux: str, keys: list[str]) -> None:
    '''Send key names to the center editor pane (`Escape`, `Enter`, literals).'''
    ids = panes_left_to_right(tmux)
    if len(ids) < 2:
        raise RuntimeError('wiki tmux session has no editor pane')
    subprocess.run(
        [tmux, 'send-keys', '-t', ids[1], *keys],
        check=True,
    )
