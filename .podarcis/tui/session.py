'''Session-guard for herdr plugin commands. The plugin registry is user-global.'''

from __future__ import annotations

import os

from podarcis.tui import SESSION_NAME


def in_wiki_session(environ: dict[str, str] | None = None) -> bool:
    '''True only inside herdr session ``podarcis``. Nested ``vscode`` must no-op.'''
    env = os.environ if environ is None else environ
    if env.get('HERDR_SESSION') == SESSION_NAME:
        return True
    sock = env.get('HERDR_SOCKET_PATH', '') or env.get('HERDR_SOCKET', '')
    return f'/sessions/{SESSION_NAME}/' in sock
