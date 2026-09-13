'''Non-interactive repo sync. Safe as a plugin action (no TTY required).'''

from __future__ import annotations

import sys

from podarcis.repos import sync_repos_full
from podarcis.tui.root import WikiRootError, find_wiki_root
from podarcis.tui.session import in_wiki_session


def main(argv: list[str] | None = None) -> int:
    if not in_wiki_session():
        return 0
    try:
        wiki_root = find_wiki_root()
    except WikiRootError as exc:
        print(exc.message, file=sys.stderr)
        return 1
    results = sync_repos_full(wiki_root)
    rc = 0
    for name, info in results.items():
        status = (info or {}).get('status') or 'ok'
        message = (info or {}).get('message') or ''
        print(f'{name}: {status} {message}'.rstrip())
        if status == 'error':
            rc = 1
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
