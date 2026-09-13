'''Resolve herdr, editor, file manager, and harness binaries.'''

from __future__ import annotations

import os
import shlex
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from podarcis.common import get_config_value
from podarcis.herdr.layout import pane_env, yazi_flavor_dir
from podarcis.tui.server import resolved_herdr_dir


HERDR_INSTALL = 'https://herdr.dev/install.sh'
EDITOR_FALLBACKS = ('helix', 'nvim', 'vi')
FILE_MANAGERS = ('yazi', 'lf')
HARNESS_FALLBACKS = ('opencode', 'claude')


@dataclass
class ResolvedDeps:
    herdr: str | None
    editor: list[str] | None
    file_manager: list[str] | None
    file_manager_name: str | None
    harness: str | None
    pane_env: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    flavor_dir: Path | None = None

    @property
    def herdr_missing_message(self) -> str:
        hinted = os.environ.get('HERDR_BIN', '')
        where = f' (HERDR_BIN={hinted})' if hinted else ' on PATH'
        return (
            f'herdr not found{where}. Install from {HERDR_INSTALL}.'
        )


def _executable(spec: str) -> str | None:
    '''Return an executable path for a name or absolute path, or None.'''
    if not spec:
        return None
    path = Path(spec).expanduser()
    if path.is_file() and os.access(path, os.X_OK):
        return str(path.resolve())
    return shutil.which(spec)


def _argv(spec: str) -> list[str] | None:
    parts = shlex.split(spec)
    if not parts:
        return None
    resolved = _executable(parts[0])
    if not resolved:
        return None
    return [resolved, *parts[1:]]


def resolve_herdr(*, environ: dict[str, str] | None = None) -> str | None:
    '''``$HERDR_BIN`` wins, including a missing path (no silent PATH fallback).'''
    env = os.environ if environ is None else environ
    if 'HERDR_BIN' in env:
        spec = (env.get('HERDR_BIN') or '').strip()
        return _executable(spec) if spec else None
    return shutil.which('herdr')


def resolve_editor(wiki_root: Path | None = None, *, environ: dict[str, str] | None = None) -> list[str] | None:
    '''``$PODARCIS_EDITOR`` → ``$EDITOR`` → helix → nvim → vi.'''
    env = os.environ if environ is None else environ
    candidates: list[str] = []
    for key in ('PODARCIS_EDITOR', 'EDITOR'):
        val = (env.get(key) or '').strip()
        if val:
            candidates.append(val)
    if wiki_root is not None:
        configured = (get_config_value(wiki_root, 'tui', 'editor') or '').strip()
        if configured:
            candidates.append(configured)
    candidates.extend(EDITOR_FALLBACKS)
    seen: set[str] = set()
    for spec in candidates:
        if spec in seen:
            continue
        seen.add(spec)
        argv = _argv(spec)
        if argv:
            return argv
    return None


def resolve_file_manager(
    wiki_root: Path | None = None,
    *,
    environ: dict[str, str] | None = None,
) -> tuple[str, list[str]] | None:
    '''yazi preferred, lf fallback. Flavor env is applied at pane-run, not here.'''
    env = os.environ if environ is None else environ
    names: list[str] = []
    env_fm = (env.get('PODARCIS_FILE_MANAGER') or '').strip()
    if env_fm:
        names.append(env_fm)
    if wiki_root is not None:
        configured = (get_config_value(wiki_root, 'tui', 'file_manager') or '').strip()
        if configured:
            names.append(configured)
    names.extend(FILE_MANAGERS)
    seen: set[str] = set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        path = _executable(name)
        if path:
            return name, [path]
    return None


def resolve_harness(wiki_root: Path | None = None, *, environ: dict[str, str] | None = None) -> str | None:
    '''Configured harness if on PATH, else first of opencode / claude.'''
    env = os.environ if environ is None else environ
    names: list[str] = []
    env_h = (env.get('PODARCIS_HARNESS') or '').strip()
    if env_h:
        names.append(env_h)
    if wiki_root is not None:
        for keys in (('tui', 'harness'), ('harness',)):
            configured = (get_config_value(wiki_root, *keys) or '').strip()
            if configured:
                names.append(configured)
                break
    names.extend(HARNESS_FALLBACKS)
    seen: set[str] = set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        if _executable(name):
            return name
    return None


def resolve_deps(wiki_root: Path, *, environ: dict[str, str] | None = None) -> ResolvedDeps:
    '''Resolve every binary the launcher needs, with warnings for optional ones.'''
    env = os.environ if environ is None else environ
    fm = resolve_file_manager(wiki_root, environ=env)
    warnings: list[str] = []
    if fm is None:
        warnings.append(
            'yazi/lf not found; files pane is a shell at the wiki root.'
        )
    flavor_dir = resolved_herdr_dir(wiki_root)
    yazi_home = None
    if fm is not None and fm[0] == 'yazi':
        cfg = yazi_flavor_dir(flavor_dir)
        if cfg is not None:
            yazi_home = str(cfg)
    return ResolvedDeps(
        herdr=resolve_herdr(environ=env),
        editor=resolve_editor(wiki_root, environ=env),
        file_manager=None if fm is None else fm[1],
        file_manager_name=None if fm is None else fm[0],
        harness=resolve_harness(wiki_root, environ=env),
        pane_env=pane_env(wiki_root, yazi_config_home=yazi_home),
        warnings=warnings,
        flavor_dir=flavor_dir,
    )
