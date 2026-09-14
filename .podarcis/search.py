'''Structured wiki search. Does not import the FastMCP wiki server.'''

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

COLLECTION_DIRS: dict[str, tuple[str, ...]] = {
    'wiki': ('wiki',),
    'protocols': ('workspace/protocols',),
    'sources': ('sources/literature',),
    'all': ('wiki', 'workspace/protocols', 'sources/literature'),
}

_TITLE_RE = re.compile(r'^title:\s*(.*)$', re.MULTILINE)
_VECTORS_RE = re.compile(r'Vectors:\s+([\d,]+)\s+embedded', re.IGNORECASE)
_PENDING_RE = re.compile(r'Pending:\s+([\d,]+)\s+need embedding', re.IGNORECASE)
_UPDATED_RE = re.compile(r'Updated:\s+(\d+)([smhd])\s+ago', re.IGNORECASE)


def collection_dirs(root: Path, collection: str) -> list[Path]:
    names = COLLECTION_DIRS.get(collection) or COLLECTION_DIRS['wiki']
    return [p for name in names if (p := Path(root) / name).exists()]


def _title_of(path: Path) -> str:
    try:
        text = path.read_text(encoding='utf-8', errors='replace')[:4000]
    except OSError:
        return path.stem
    fm = re.match(r'^---\s*\n(.*?)\n---', text, re.DOTALL)
    if fm:
        m = _TITLE_RE.search(fm.group(1))
        if m:
            return m.group(1).strip().strip('\'"')
    return path.stem


def _relpath(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _infer_collection(rel: str) -> str:
    if rel.startswith('workspace/protocols/') or rel.startswith('workspace/protocols'):
        return 'protocols'
    if rel.startswith('sources/'):
        return 'sources'
    if rel.startswith('wiki/'):
        return 'wiki'
    return 'wiki'


def parse_index_health(status_text: str) -> str | None:
    '''Return a warning for index conditions qmd does not report itself.'''
    m = _VECTORS_RE.search(status_text)
    vectors = int(m.group(1).replace(',', '')) if m else None
    if vectors == 0:
        p = _PENDING_RE.search(status_text)
        pending = int(p.group(1).replace(',', '')) if p else 0
        return (
            f'QMD index has NO embeddings ({pending} documents pending). Semantic and '
            'hybrid search cannot work — results below are keyword-only. Run `qmd embed`.'
        )
    u = _UPDATED_RE.search(status_text)
    if u:
        amount, unit = int(u.group(1)), u.group(2).lower()
        days = {'s': 0, 'm': 0, 'h': amount / 24.0, 'd': amount}[unit]
        if days >= 7:
            return (
                f'QMD index was last updated {amount}{unit} ago and may not reflect '
                'recent edits — run `qmd update`.'
            )
    return None


QMD_ABSENT = (
    "semantic search needs the 'qmd' binary on PATH — install it, "
    'or set engines.qmd: true in .podarcis/config.yaml.'
)
QMD_OFF = 'QMD engine is off (engines.qmd: false in .podarcis/config.yaml).'


def qmd_status(root: Path, *, environ: dict[str, str] | None = None) -> tuple[str, str]:
    '''disabled | enabled_ok | enabled_broken.

    An absent `engines.qmd` is not a decision the user made, so it follows the
    binary rather than defaulting to off and then blaming a config line nobody
    wrote. An explicit value, from the key or from $ENABLE_QMD, always wins.
    '''
    env = os.environ if environ is None else environ
    env_flag = env.get('ENABLE_QMD')
    if env_flag is not None:
        enabled = env_flag.lower() in ('true', '1', 'yes')
    else:
        enabled = None
        yaml_path = Path(root) / '.podarcis' / 'config.yaml'
        if yaml_path.is_file():
            try:
                import yaml
                data = yaml.safe_load(yaml_path.read_text(encoding='utf-8')) or {}
                raw = (data.get('engines') or {}).get('qmd')
                enabled = None if raw is None else bool(raw)
            except Exception:
                enabled = None
    if enabled is False:
        return 'disabled', QMD_OFF
    qmd_bin = shutil.which('qmd')
    if not qmd_bin:
        # Nobody asked for it and it is not installed: nothing is broken.
        return ('enabled_broken', "'qmd' binary not found in PATH.") if enabled else ('disabled', QMD_ABSENT)
    return 'enabled_ok', qmd_bin


def _qmd(root: Path, *args: str, json_output: bool = False, timeout_s: float = 20.0) -> str:
    cmd = ['qmd', *args]
    if json_output:
        cmd.append('--json')
    proc = subprocess.run(
        cmd, cwd=str(root), capture_output=True, text=True, check=False, timeout=timeout_s,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or '').strip()
        raise RuntimeError(f"qmd {' '.join(args)} failed: {err}")
    return proc.stdout


def _qmd_index_warning(root: Path) -> str | None:
    try:
        text = _qmd(root, 'status', timeout_s=8.0)
    except Exception as exc:
        return f'QMD index health could not be determined ({exc}).'
    return parse_index_health(text)


def _normalize_qmd_path(raw: str, root: Path) -> str:
    s = (raw or '').strip()
    if s.startswith('qmd://'):
        s = s[6:]
    if s.startswith('./'):
        s = s[2:]
    p = Path(s)
    if p.is_absolute():
        return _relpath(p, root)
    return s.lstrip('/')


def _hits_from_qmd_json(payload: object, root: Path, collection: str, limit: int) -> list[dict]:
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = next(
            (payload[k] for k in ('hits', 'results', 'items') if isinstance(payload.get(k), list)),
            [],
        )
    else:
        items = []
    hits: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        raw = item.get('path') or item.get('file') or item.get('filepath') or ''
        rel = _normalize_qmd_path(str(raw), root)
        if not rel:
            continue
        abs_path = (root / rel) if not Path(rel).is_absolute() else Path(rel)
        title = str(item.get('title') or '').strip() or (_title_of(abs_path) if abs_path.is_file() else Path(rel).stem)
        score = item.get('score')
        try:
            score_f = float(score) if score is not None else None
        except (TypeError, ValueError):
            score_f = None
        snippet = item.get('snippet') or item.get('body') or item.get('text') or ''
        if isinstance(snippet, str) and len(snippet) > 240:
            snippet = snippet[:237] + '...'
        hits.append({
            'path': rel,
            'title': title,
            'score': score_f,
            'collection': _infer_collection(rel) if collection == 'all' else collection,
            'snippet': snippet.strip() if isinstance(snippet, str) else '',
        })
        if len(hits) >= limit:
            break
    return hits


def _parse_rg_line(line: str, root: Path) -> tuple[Path, str] | None:
    '''Parse ``path:line:text``; skip context (``--``) lines.'''
    if not line or line.startswith('--'):
        return None
    # Windows drive letters aside: split on the first two colons after the path.
    m = re.match(r'^(.*?):(\d+):(.*)$', line)
    if not m:
        return None
    raw_path, _lineno, text = m.group(1), m.group(2), m.group(3)
    path = Path(raw_path)
    if not path.is_absolute():
        path = root / path
    return path, text.strip()


def keyword_search(root: Path, query: str, collection: str, limit: int) -> list[dict]:
    dirs = collection_dirs(root, collection)
    if not dirs:
        return []
    lines: list[str] = []
    rg_bin = shutil.which('rg')
    if rg_bin:
        cmd = [
            rg_bin, '-i', '-n', '--no-heading', '--fixed-strings',
            '-g', '*.md', query, *[str(d) for d in dirs],
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        lines = (proc.stdout or '').splitlines()
    else:
        pattern = re.compile(re.escape(query), re.IGNORECASE)
        for sdir in dirs:
            for md_file in sdir.rglob('*.md'):
                try:
                    content = md_file.read_text(encoding='utf-8', errors='replace')
                except OSError:
                    continue
                for line in content.splitlines():
                    if pattern.search(line):
                        lines.append(f'{md_file}:{0}:{line.strip()}')
                        break

    grouped: dict[str, dict] = {}
    for line in lines:
        parsed = _parse_rg_line(line, root)
        if parsed is None:
            continue
        path, snippet = parsed
        rel = _relpath(path, root)
        if rel in grouped:
            continue
        grouped[rel] = {
            'path': rel,
            'title': _title_of(path) if path.is_file() else Path(rel).stem,
            'score': None,
            'collection': _infer_collection(rel) if collection == 'all' else collection,
            'snippet': snippet[:240],
        }
        if len(grouped) >= limit:
            break
    return list(grouped.values())


def search(
    root: Path | str,
    query: str,
    *,
    collection: str = 'wiki',
    method: str = 'hybrid',
    limit: int = 20,
    no_rerank: bool = False,
    environ: dict[str, str] | None = None,
) -> dict:
    '''Return ``{query, collection, method, warning, hits}``. Default collection is wiki.'''
    wiki_root = Path(root)
    coll = collection if collection in COLLECTION_DIRS else 'wiki'
    meth = method if method in ('hybrid', 'semantic', 'keyword') else 'hybrid'
    query = (query or '').strip()
    out = {
        'query': query,
        'collection': coll,
        'method': meth,
        'warning': None,
        'hits': [],
    }
    if not query:
        return out

    status, info = qmd_status(wiki_root, environ=environ)
    use_qmd = status == 'enabled_ok' and meth in ('hybrid', 'semantic')

    if status == 'enabled_broken' and meth in ('hybrid', 'semantic'):
        out['warning'] = (
            f'QMD is enabled but unavailable ({info}). Falling back to keyword search.'
        )
    elif status == 'disabled' and meth in ('hybrid', 'semantic'):
        out['warning'] = f'{info} Operating in keyword search mode.'

    if use_qmd:
        if meth == 'semantic':
            args = ['vsearch', query, '-n', str(limit)]
        else:
            args = ['query', query, '-n', str(limit)]
            if no_rerank:
                args.append('--no-rerank')
        if coll != 'all':
            args += ['-c', coll]
        try:
            raw = _qmd(wiki_root, *args, json_output=True)
            payload = json.loads(raw) if raw.strip() else []
            out['hits'] = _hits_from_qmd_json(payload, wiki_root, coll, limit)
            health = _qmd_index_warning(wiki_root)
            if health:
                out['warning'] = health
            return out
        except Exception as exc:
            out['warning'] = f'QMD failed ({exc}). Falling back to keyword search.'

    out['hits'] = keyword_search(wiki_root, query, coll, limit)
    if meth == 'keyword':
        out['warning'] = None if status != 'enabled_broken' else out['warning']
    return out
