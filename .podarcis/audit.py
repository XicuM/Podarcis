'''Lint-gated commit. Same gate as agent jobs; Python is ``sys.executable``.'''

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

from podarcis.repos import get_repo_names
from podarcis.tui.root import is_wiki_root

MAX_WORDS = 1500


def python_bin() -> str:
    '''Interpreter that can import this package. Never checkout ``.venv/bin/python``.'''
    if sys.executable:
        return sys.executable
    return shutil.which('python3') or 'python3'


def bundled_check_links() -> Path:
    return Path(__file__).resolve().parent / 'wiki_check_links.py'


def check_links_path(root: Path | str | None = None) -> Path:
    '''Prefer the checkout copy (run_audit), then engine tree, then package data.

    JSON mapping lives in this module so an older checkout script without
    ``to_json_payload`` still works for ``podarcis lint --json``.
    '''
    if root is not None:
        local = Path(root) / '.agents' / 'mcp' / 'wiki' / 'check_links.py'
        if local.is_file():
            return local
    from podarcis import ROOT_DIR
    engine = ROOT_DIR / '.agents' / 'mcp' / 'wiki' / 'check_links.py'
    if engine.is_file():
        return engine
    bundled = bundled_check_links()
    if bundled.is_file():
        return bundled
    raise FileNotFoundError('check_links.py not found')


def load_check_links(root: Path | str | None = None):
    path = check_links_path(root)
    spec = importlib.util.spec_from_file_location('_podarcis_check_links', path)
    if spec is None or spec.loader is None:
        raise ImportError(f'cannot load {path}')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def lint_json_path(root: Path) -> Path:
    return Path(root) / 'tmp' / 'tui' / 'lint.json'


def file_issues(path: str, res: dict, *, max_words: int = MAX_WORDS) -> list[dict]:
    '''Map one ``run_audit`` result to ``{code, detail}`` records.'''
    issues: list[dict] = []
    if res.get('bloated_directory'):
        issues.append({'code': 'bloated_directory', 'detail': str(res['bloated_directory'])})
    parts = Path(path).as_posix().split('/')
    if 'wiki' in parts or 'user' in parts:
        if res.get('word_count', 0) > max_words:
            issues.append({'code': 'page_length', 'detail': str(res['word_count'])})
    for err in res.get('yaml_errors') or []:
        issues.append({'code': 'yaml_error', 'detail': str(err)})
    for item in res.get('broken_links') or []:
        if isinstance(item, (tuple, list)) and item:
            link = item[0]
            target = item[1] if len(item) > 1 else ''
            detail = f'{link} -> {target}' if target else str(link)
        else:
            detail = str(item)
        issues.append({'code': 'broken_link', 'detail': detail})
    for ref in res.get('missing_footnotes') or []:
        issues.append({'code': 'missing_footnote', 'detail': str(ref)})
    for ref in res.get('unused_footnotes') or []:
        issues.append({'code': 'unused_footnote', 'detail': str(ref)})
    for ref in res.get('unmatched_sources') or []:
        issues.append({'code': 'unmatched_source', 'detail': str(ref)})
    for ref in res.get('positional_footnotes') or []:
        issues.append({'code': 'positional_footnote', 'detail': str(ref)})
    for item in res.get('missing_frontmatter') or []:
        issues.append({'code': 'missing_frontmatter', 'detail': str(item)})
    return issues


def to_json_payload(audit_results: dict, root: str, *, max_words: int = MAX_WORDS) -> dict:
    '''``podarcis lint --json`` object: path → list of ``{code, detail}``.'''
    root_path = Path(root).resolve()
    files: dict[str, list[dict]] = {}
    for path, res in (audit_results or {}).items():
        issues = file_issues(str(path), res, max_words=max_words)
        if not issues:
            continue
        try:
            rel = Path(path).resolve().relative_to(root_path).as_posix()
        except ValueError:
            rel = str(path)
        files[rel] = issues
    return {'ok': not files, 'root': str(root_path), 'files': files}


def write_lint_json(root: Path, payload: dict) -> Path | None:
    if not is_wiki_root(root):
        return None
    dest = lint_json_path(root)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
    return dest


def lint(root: Path | str, path: str | Path | None = None, *, fix: bool = False) -> dict:
    '''Return the ``podarcis lint --json`` object. Does not spawn ``.venv/bin/python``.'''
    root_path = Path(root).resolve()
    target = Path(path).resolve() if path else root_path
    mod = load_check_links(root_path)
    results = mod.run_audit(str(target), do_fix=fix)
    max_words = int(getattr(mod, 'MAX_WORDS', MAX_WORDS) or MAX_WORDS)
    payload = to_json_payload(results, str(root_path), max_words=max_words)
    write_lint_json(root_path, payload)
    return payload


def audit_gate(root: Path | str) -> tuple[bool, str]:
    '''Run the same link/frontmatter audit as ``podarcis lint``.'''
    root_path = Path(root)
    script = check_links_path(root_path)
    proc = subprocess.run(
        [python_bin(), str(script), str(root_path)],
        capture_output=True, text=True, check=False, cwd=str(root_path),
    )
    if proc.returncode != 0:
        return False, (proc.stdout or proc.stderr or '').strip()[-2000:]
    return True, 'Audit passed.'


def dirty_repos(root: Path | str) -> list[Path]:
    root_path = Path(root)
    found: list[Path] = []
    for name in get_repo_names(root_path):
        repo = (root_path / name).resolve()
        if repo == root_path.resolve():
            continue
        if not (repo / '.git').exists():
            continue
        st = subprocess.run(
            ['git', 'status', '--porcelain'],
            cwd=repo, capture_output=True, text=True, check=False,
        )
        if st.stdout.strip():
            found.append(repo)
    return found


def _commit_repos(repos: list[Path], message: str) -> tuple[list[str], str]:
    done: list[str] = []
    errors: list[str] = []
    for repo in repos:
        repo = Path(repo).resolve()
        if not (repo / '.git').is_dir():
            continue
        added = subprocess.run(
            ['git', 'add', '-A'], cwd=repo, capture_output=True, text=True, check=False,
        )
        if added.returncode != 0:
            errors.append(f'{repo.name} add: {(added.stderr or added.stdout).strip()}')
            continue
        proc = subprocess.run(
            ['git', 'commit', '-m', message],
            cwd=repo, capture_output=True, text=True, check=False,
        )
        if proc.returncode == 0:
            done.append(repo.name)
        else:
            errors.append(f'{repo.name}: {(proc.stderr or proc.stdout).strip()}')
    return done, '\n'.join(errors)


def commit_repos(repos: list[Path], message: str) -> list[str]:
    '''Per-repo ``git add``; never flatten workspace into wiki or add at engine root.'''
    done, _err = _commit_repos(repos, message)
    return done


def audit_and_commit(root: Path | str, message: str) -> dict:
    '''Lint-gate, then commit dirty workspace repos. Does not push.'''
    root_path = Path(root)
    ok, detail = audit_gate(root_path)
    if not ok:
        return {'ok': False, 'committed': [], 'message': detail}
    dirty = dirty_repos(root_path)
    if not dirty:
        return {'ok': True, 'committed': [], 'message': 'nothing to commit'}
    committed, err = _commit_repos(dirty, message)
    if len(committed) < len(dirty):
        return {
            'ok': False,
            'committed': committed,
            'message': err or 'commit failed',
        }
    return {'ok': True, 'committed': committed, 'message': 'committed ' + ', '.join(committed)}


def run_lint(
    root: Path | str,
    target: str | Path | None = None,
    *,
    as_json: bool = False,
    fix: bool = False,
) -> int:
    '''CLI helper for ``podarcis lint``. Human text stays the default.'''
    root_path = Path(root)
    dest = Path(target) if target else root_path
    if as_json:
        payload = lint(root_path, dest, fix=fix)
        print(json.dumps(payload, indent=2))
        return 0 if payload.get('ok') else 1
    cmd = [python_bin(), str(check_links_path(root_path))]
    if fix:
        cmd.append('--fix')
    cmd.append(str(dest))
    return subprocess.run(cmd).returncode
