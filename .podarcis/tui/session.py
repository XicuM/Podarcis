'''Session-guard for herdr plugin commands.

Plugin installation is user-global; isolation is this check, not the registry.
'''

from __future__ import annotations

import os

from podarcis.tui import SESSION_NAME


def in_wiki_session(environ: dict[str, str] | None = None) -> bool:
    '''True when the process is inside herdr session ``podarcis``.'''
    env = os.environ if environ is None else environ
    if env.get('HERDR_SESSION') == SESSION_NAME:
        return True
    sock = env.get('HERDR_SOCKET_PATH', '')
    return f'/sessions/{SESSION_NAME}/' in sock
