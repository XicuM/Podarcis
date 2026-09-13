'''Wiki search popup. Needs a TTY — bind as ``type = "popup"``, never a plugin action.'''

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

from podarcis.tui.root import WikiRootError, find_wiki_root
from podarcis.tui.search import COLLECTION_DIRS, search
from podarcis.tui.session import in_wiki_session

COLLECTIONS = tuple(COLLECTION_DIRS)


def _pick(lines: list[str]) -> str | None:
    if not lines:
        return None
    fzf = shutil.which('fzf')
    if fzf and sys.stdin.isatty() and sys.stdout.isatty():
        proc = subprocess.run(
            [fzf, '--prompt', 'wiki> ', '--height', '100%', '--reverse'],
            input='\n'.join(lines) + '\n',
            capture_output=True, text=True, check=False,
        )
        if proc.returncode != 0:
            return None
        return (proc.stdout or '').strip() or None
    for i, line in enumerate(lines, 1):
        print(f'{i}. {line}')
    if not sys.stdin.isatty():
        return None
    raw = input('number> ').strip()
    if not raw:
        return None
    try:
        idx = int(raw)
    except ValueError:
        return None
    if 1 <= idx <= len(lines):
        return lines[idx - 1]
    return None


def _format_hit(hit: dict) -> str:
    score = hit.get('score')
    score_s = f'{score:.2f}' if isinstance(score, (int, float)) else '--'
    title = hit.get('title') or Path(str(hit.get('path') or '')).stem
    return f'{score_s}\t{title}\t{hit.get("path")}'


def _open_hit(wiki_root: Path, rel: str) -> None:
    path = wiki_root / rel
    print(path)
    if importlib.util.find_spec('podarcis.tui.actions.edit') is None:
        return
    subprocess.run(
        [sys.executable, '-m', 'podarcis.tui.actions.edit', '--', str(path)],
        check=False,
    )


def _run_query(wiki_root: Path, query: str, collection: str) -> int:
    result = search(wiki_root, query, collection=collection, method='hybrid', no_rerank=True)
    if result.get('warning'):
        print(f'warning: {result["warning"]}', file=sys.stderr)
    hits = result.get('hits') or []
    if not hits:
        print(f'no hits for {query!r} in {collection}')
        return 0
    lines = [_format_hit(h) for h in hits]
    chosen = _pick(lines)
    if not chosen:
        return 0
    rel = chosen.split('\t')[-1].strip()
    if rel:
        _open_hit(wiki_root, rel)
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
    print('prefix :wiki | :protocols | :sources | :all  to switch collection; empty line exits.')
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
