'''Discover the wiki checkout root. Never use ``podarcis.ROOT_DIR``.'''

from __future__ import annotations

import os
from pathlib import Path


WIKI_ROOT_ERROR = (
    'not a Podarcis checkout (no AGENTS.md + .podarcis/config.yaml). Pass --root.'
)


class WikiRootError(SystemExit):
    '''Raised when no checkout root can be resolved.'''

    def __init__(self, message: str = WIKI_ROOT_ERROR):
        super().__init__(message)
        self.message = message


def is_wiki_root(path: Path) -> bool:
    '''A checkout is both AGENTS.md and .podarcis/config.yaml, not the engine package.'''
    root = Path(path)
    return (root / 'AGENTS.md').is_file() and (root / '.podarcis' / 'config.yaml').is_file()


def find_wiki_root(
    *,
    explicit: str | Path | None = None,
    cwd: Path | None = None,
    environ: dict[str, str] | None = None,
) -> Path:
    '''Resolve the wiki checkout.

    Order: ``--root`` / ``$PODARCIS_ROOT`` / walk ``cwd`` and parents.
    An explicit or env path that is not a checkout is an error (no walk fallback).
    '''
    env = os.environ if environ is None else environ

    if explicit is not None and str(explicit).strip():
        path = Path(explicit).expanduser().resolve()
        if not is_wiki_root(path):
            raise WikiRootError(
                f'not a Podarcis checkout at {path} '
                f'(no AGENTS.md + .podarcis/config.yaml). Pass --root.'
            )
        return path

    env_root = (env.get('PODARCIS_ROOT') or '').strip()
    if env_root:
        path = Path(env_root).expanduser().resolve()
        if not is_wiki_root(path):
            raise WikiRootError(
                f'not a Podarcis checkout at {path} '
                f'(no AGENTS.md + .podarcis/config.yaml). Pass --root.'
            )
        return path

    start = Path(cwd) if cwd is not None else Path.cwd()
    start = start.resolve()
    for candidate in (start, *start.parents):
        if is_wiki_root(candidate):
            return candidate

    raise WikiRootError()


def find_wiki_root_or_none(
    *,
    explicit: str | Path | None = None,
    cwd: Path | None = None,
    environ: dict[str, str] | None = None,
) -> Path | None:
    '''Like ``find_wiki_root`` but returns None instead of raising on a walk miss.

    An explicit ``--root`` / ``$PODARCIS_ROOT`` that is not a checkout still raises:
    the user asked for a specific path.
    '''
    env = os.environ if environ is None else environ
    pinned = (explicit is not None and str(explicit).strip()) or (env.get('PODARCIS_ROOT') or '').strip()
    try:
        return find_wiki_root(explicit=explicit, cwd=cwd, environ=environ)
    except WikiRootError:
        if pinned:
            raise
        return None
