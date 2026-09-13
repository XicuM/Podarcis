'''Startup hook: session-guard, report sidebar tokens, then exit. Do not apply layouts.'''

from __future__ import annotations

import os
import shutil
import sys

from podarcis.herdr.layout import find_workspace
from podarcis.repos import get_repo_status
from podarcis.tui.root import WikiRootError, find_wiki_root
from podarcis.tui.server import HerdrSession, session_config, session_sock
from podarcis.tui.session import in_wiki_session
from podarcis.tui import SESSION_NAME


def _herdr_bin() -> str | None:
    for key in ('HERDR_BIN_PATH', 'HERDR_BIN'):
        val = (os.environ.get(key) or '').strip()
        if val:
            return val
    return shutil.which('herdr')


def report_repo_metadata(wiki_root, session: HerdrSession) -> int:
    ws = find_workspace(session.cli)
    if not ws:
        return 0
    ws_id = ws.get('workspace_id') or ws.get('id')
    if not ws_id:
        return 0
    argv = [
        'workspace', 'report-metadata', str(ws_id),
        '--source', 'podarcis.wiki',
        '--ttl-ms', '300000',
    ]
    for row in get_repo_status(wiki_root):
        name = row.get('repo')
        status = row.get('status') or 'unknown'
        if name:
            argv += ['--token', f'{name}={status}']
    session.cli(*argv)
    return 0


def main(argv: list[str] | None = None) -> int:
    if not in_wiki_session():
        return 0
    try:
        wiki_root = find_wiki_root()
    except WikiRootError:
        return 0
    herdr = _herdr_bin()
    if not herdr:
        return 0
    env = os.environ.copy()
    env.setdefault('HERDR_SESSION', SESSION_NAME)
    env.setdefault('HERDR_CONFIG_PATH', str(session_config()))
    session = HerdrSession(herdr, env=env, sock=session_sock())
    try:
        return report_repo_metadata(wiki_root, session)
    except Exception as exc:
        print(f'podarcis.tui.metadata: {exc}', file=sys.stderr)
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
