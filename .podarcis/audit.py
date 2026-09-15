'''Lint-gated commit. Same gate as agent jobs, and the same linter.

The linter itself lives in Rust (``tui/src/vault/lint.rs``) and is reached
through the ``podarcis`` binary. It used to live here too, in two identical
``check_links.py`` copies policed against the Rust port by a differ; the port
was proven equal over the whole corpus, so the copies and the differ are gone.
This module keeps the *gate* — what a failing lint means for a commit — which
is the part that was never duplicated.
'''

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from podarcis.repos import get_repo_names


def podarcis_bin(root: Path | str | None = None) -> str:
    '''Path to the Rust ``podarcis`` binary, which owns the linter.

    ``PODARCIS_BIN`` first so a test or a container can point at one build,
    then the checkout's own release binary, then ``PATH``. Deliberately never
    falls back to a Python entry point: there is no Python linter left to fall
    back to, and a gate that silently passes because it could not find its
    checker is worse than one that fails.
    '''
    if (env := os.environ.get('PODARCIS_BIN')) and Path(env).is_file():
        return env
    if root is not None:
        local = Path(root) / 'tui' / 'target' / 'release' / 'podarcis'
        if local.is_file():
            return str(local)
    from podarcis import ROOT_DIR
    engine = ROOT_DIR / 'tui' / 'target' / 'release' / 'podarcis'
    if engine.is_file():
        return str(engine)
    if found := shutil.which('podarcis'):
        return found
    raise FileNotFoundError(
        'the podarcis binary is not built — run `podarcis build` '
        '(or set PODARCIS_BIN); the linter lives there'
    )


def audit_gate(root: Path | str) -> tuple[bool, str]:
    '''Run ``podarcis lint``, which exits non-zero on findings.

    Returns ``(False, detail)`` when the linter reports findings *or* cannot be
    run at all. Both are reasons not to commit.
    '''
    root_path = Path(root)
    try:
        binary = podarcis_bin(root_path)
    except FileNotFoundError as err:
        return False, str(err)
    proc = subprocess.run(
        [binary, 'lint', str(root_path)],
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
