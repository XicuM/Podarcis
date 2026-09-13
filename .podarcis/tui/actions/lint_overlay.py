'''Wiki lint popup. Needs a TTY — bind as ``type = "popup"``. Check-only.'''

from __future__ import annotations

import sys

from podarcis.audit import lint
from podarcis.tui.open_edit import open_page
from podarcis.tui.root import WikiRootError, find_wiki_root
from podarcis.tui.session import in_wiki_session


def _pick_path(paths: list[str]) -> str | None:
    if not paths:
        return None
    for i, path in enumerate(paths, 1):
        print(f'{i}. {path}')
    if not sys.stdin.isatty():
        return None
    raw = input('open number (empty to close)> ').strip()
    if not raw:
        return None
    try:
        idx = int(raw)
    except ValueError:
        return None
    if 1 <= idx <= len(paths):
        return paths[idx - 1]
    return None


def main(argv: list[str] | None = None) -> int:
    if not in_wiki_session():
        return 0
    try:
        wiki_root = find_wiki_root()
    except WikiRootError as exc:
        print(exc.message, file=sys.stderr)
        return 1
    payload = lint(wiki_root)
    files = payload.get('files') or {}
    if payload.get('ok') or not files:
        print('Audit passed: no issues found.')
        return 0
    print(f'lint failed ({len(files)} path(s))\n')
    for path, issues in files.items():
        print(f'--- {path} ---')
        for issue in issues:
            print(f'  {issue.get("code")}: {issue.get("detail")}')
        print()
    chosen = _pick_path(list(files))
    if chosen:
        open_page(wiki_root, chosen)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
