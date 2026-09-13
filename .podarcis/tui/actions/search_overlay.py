'''Wiki search popup. Needs a TTY — bind as ``type = "popup"``, never a plugin action.'''

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from podarcis.tui.open_edit import open_page
from podarcis.tui.root import WikiRootError, find_wiki_root
from podarcis.tui.search import COLLECTION_DIRS, search
from podarcis.tui.session import in_wiki_session

COLLECTIONS = tuple(COLLECTION_DIRS)


def _next_collection(current: str) -> str:
    cols = list(COLLECTIONS)
    try:
        idx = cols.index(current)
    except ValueError:
        return cols[0]
    return cols[(idx + 1) % len(cols)]


def _pick(lines: list[str], collection: str) -> tuple[str | None, str]:
    '''Return ``(selection, action)`` where action is select/cycle/abort.'''
    if not lines:
        return None, 'abort'
    fzf = shutil.which('fzf')
    if fzf and sys.stdin.isatty() and sys.stdout.isatty():
        proc = subprocess.run(
            [
                fzf, '--prompt', f'{collection}> ', '--height', '100%', '--reverse',
                '--expect=ctrl-s',
                '--header', f'{collection}  enter open  ctrl-s cycle collection',
            ],
            input='\n'.join(lines) + '\n',
            capture_output=True, text=True, check=False,
        )
        out = [ln for ln in (proc.stdout or '').splitlines()]
        if not out:
            return None, 'abort'
        key, choice = out[0], (out[1] if len(out) > 1 else '')
        if key == 'ctrl-s':
            return choice or None, 'cycle'
        if proc.returncode not in (0, 1):
            return None, 'abort'
        return (choice or key or None), 'select'
    for i, line in enumerate(lines, 1):
        print(f'{i}. {line}')
    if not sys.stdin.isatty():
        return None, 'abort'
    raw = input(f'number [{collection}] (s cycle, empty abort)> ').strip()
    if not raw:
        return None, 'abort'
    if raw.lower() in {'s', 'ctrl-s'}:
        return None, 'cycle'
    try:
        idx = int(raw)
    except ValueError:
        return None, 'abort'
    if 1 <= idx <= len(lines):
        return lines[idx - 1], 'select'
    return None, 'abort'


def _format_hit(hit: dict) -> str:
    score = hit.get('score')
    score_s = f'{score:.2f}' if isinstance(score, (int, float)) else '--'
    title = hit.get('title') or Path(str(hit.get('path') or '')).stem
    return f'{score_s}\t{title}\t{hit.get("path")}'


def _run_query(wiki_root: Path, query: str, collection: str) -> int:
    while True:
        result = search(wiki_root, query, collection=collection, method='hybrid', no_rerank=True)
        if result.get('warning'):
            print(f'warning: {result["warning"]}', file=sys.stderr)
        hits = result.get('hits') or []
        if not hits:
            print(f'no hits for {query!r} in {collection}')
            if not sys.stdin.isatty():
                return 0
            nxt = _next_collection(collection)
            print(f'ctrl-s would cycle to {nxt}')
            return 0
        lines = [_format_hit(h) for h in hits]
        chosen, action = _pick(lines, collection)
        if action == 'cycle':
            collection = _next_collection(collection)
            print(f'collection: {collection}', file=sys.stderr)
            continue
        if not chosen:
            return 0
        rel = chosen.split('\t')[-1].strip()
        if rel:
            return open_page(wiki_root, rel)
        return 0


def main(argv: list[str] | None = None) -> int:
    if not in_wiki_session():
        return 0
    try:
        wiki_root = find_wiki_root()
    except WikiRootError as exc:
        print(exc.message, file=sys.stderr)
        return 1
    args = list(sys.argv[1:] if argv is None else argv)
    collection = 'wiki'
    if args and args[0] in COLLECTIONS:
        collection = args.pop(0)
    query = ' '.join(args).strip()
    if query:
        return _run_query(wiki_root, query, collection)
    if not sys.stdin.isatty():
        print('usage: python3 -m podarcis.tui.actions.search_overlay [collection] QUERY', file=sys.stderr)
        return 1
    print('enter a query; ctrl-s (or s) cycles collection; empty line exits.')
    while True:
        try:
            raw = input(f'search [{collection}]> ').strip()
        except EOFError:
            return 0
        if not raw:
            return 0
        if raw.startswith(':') and raw[1:] in COLLECTIONS:
            collection = raw[1:]
            continue
        _run_query(wiki_root, raw, collection)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
