'''Current-page pointer for agent panes: ``tmp/tui/current.json`` + snippet.'''

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import yaml

REPOS = ('wiki', 'sources', 'workspace')
BODY_LINES = 20
CITATION_CHAIN = 'citation chain: workspace → wiki → sources; do not bypass.'


def tui_dir(wiki_root: Path) -> Path:
    return Path(wiki_root) / 'tmp' / 'tui'


def current_json_path(wiki_root: Path) -> Path:
    return tui_dir(wiki_root) / 'current.json'


def current_md_path(wiki_root: Path) -> Path:
    return tui_dir(wiki_root) / 'current.md'


def rel_posix(wiki_root: Path, path: Path) -> str:
    path = Path(path).expanduser()
    root = Path(wiki_root).resolve()
    try:
        resolved = path.resolve() if path.exists() else (path if path.is_absolute() else root / path)
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def repo_for(rel: str) -> str:
    top = rel.split('/', 1)[0]
    if top in REPOS:
        return top
    return 'engine'


def split_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith('---'):
        return {}, text
    parts = text.split('---', 2)
    if len(parts) < 3:
        return {}, text
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, parts[2].lstrip('\n')


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _read_page(path: Path) -> tuple[dict, str]:
    try:
        text = path.read_text(encoding='utf-8')
    except OSError:
        return {}, ''
    return split_frontmatter(text)


def snippet_markdown(record: dict, body: str) -> str:
    lines = [
        f'# {record.get("title") or record.get("path") or "Current page"}',
        '',
        f'- path: {record.get("path") or ""}',
        f'- title: {record.get("title") or ""}',
        f'- type: {record.get("type") or ""}',
        f'- category: {record.get("category") or ""}',
        f'- repo: {record.get("repo") or ""}',
        '',
        CITATION_CHAIN,
        '',
    ]
    excerpt = [ln for ln in (body or '').splitlines()[:BODY_LINES]]
    if excerpt:
        lines.append('## Excerpt')
        lines.append('')
        lines.extend(excerpt)
        lines.append('')
    return '\n'.join(lines)


def write_current(wiki_root: Path, path: str | Path) -> dict:
    '''Write ``tmp/tui/current.json`` and ``current.md``. No resident watcher.'''
    root = Path(wiki_root)
    target = Path(path).expanduser()
    if not target.is_absolute():
        cwd_hit = Path.cwd() / target
        target = cwd_hit if cwd_hit.exists() else root / target
    rel = rel_posix(root, target)
    meta, body = _read_page(target) if target.is_file() else ({}, '')
    title = meta.get('title') or (target.stem if target.suffix else target.name) or None
    record = {
        'path': rel,
        'repo': repo_for(rel),
        'title': title,
        'type': meta.get('type'),
        'category': meta.get('category'),
        'status': meta.get('status'),
        'updated_at': _now_iso(),
    }
    dest = tui_dir(root)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / 'current.json').write_text(
        json.dumps(record, indent=2, ensure_ascii=False) + '\n', encoding='utf-8',
    )
    (dest / 'current.md').write_text(snippet_markdown(record, body), encoding='utf-8')
    return record


def read_current(wiki_root: Path) -> dict | None:
    path = current_json_path(wiki_root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def read_snippet(wiki_root: Path) -> str:
    path = current_md_path(wiki_root)
    if not path.is_file():
        rec = read_current(wiki_root)
        if rec is None:
            return ''
        return snippet_markdown(rec, '')
    try:
        return path.read_text(encoding='utf-8')
    except OSError:
        return ''
