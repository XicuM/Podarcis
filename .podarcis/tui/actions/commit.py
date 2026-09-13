'''Lint-gated commit popup. Needs a TTY. Does not push and does not auto-commit via repos.push.'''

from __future__ import annotations

import sys

from podarcis.audit import audit_and_commit, dirty_repos
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
    dirty = dirty_repos(wiki_root)
    if not dirty:
        print('nothing to commit')
        return 0
    print('Dirty repositories (per-repo add; wiki/workspace/sources stay separate):')
    for repo in dirty:
        print(f'  • {repo.name}')
    print('\nCommit is blocked on `podarcis lint`. This does not push.')
    if not sys.stdin.isatty():
        print('no TTY; abort', file=sys.stderr)
        return 1
    try:
        ans = input('commit? [y/N] ').strip().lower()
    except EOFError:
        return 1
    if ans not in ('y', 'yes'):
        print('aborted')
        return 0
    args = list(sys.argv[1:] if argv is None else argv)
    message = ' '.join(args).strip() or 'chore: wiki commit'
    result = audit_and_commit(wiki_root, message)
    if not result.get('ok'):
        print('audit gate failed; nothing committed.', file=sys.stderr)
        print(result.get('message') or '', file=sys.stderr)
        return 1
    committed = result.get('committed') or []
    if committed:
        print('committed: ' + ', '.join(committed))
    else:
        print(result.get('message') or 'nothing committed')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
