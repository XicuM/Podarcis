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
    '''A project/checkout has either AGENTS.md + .podarcis/config.yaml, podarcis.yaml, or wiki/ + workspace/.'''
    root = Path(path)
    if (root / 'AGENTS.md').is_file() and (root / '.podarcis' / 'config.yaml').is_file():
        return True
    if (root / 'podarcis.yaml').is_file():
        return True
    if (root / 'wiki').is_dir() and (root / 'workspace').is_dir():
        return True
    return False


def find_wiki_root(
    *,
    explicit: str | Path | None = None,
    cwd: Path | None = None,
    environ: dict[str, str] | None = None,
) -> Path:
    '''Resolve the wiki checkout or active project root.

    Order: ``--root`` / ``$PODARCIS_ROOT`` / walk ``cwd`` and parents / global active project.
    An explicit or env path that is not a checkout is an error (no walk fallback).
    '''
    env = os.environ if environ is None else environ

    if explicit is not None and str(explicit).strip():
        path = Path(explicit).expanduser().resolve()
        if not is_wiki_root(path):
            # Check if explicit is a registered project name
            from podarcis.project import list_projects
            projects = list_projects()
            name_str = str(explicit).strip()
            if name_str in projects and projects[name_str].get('path'):
                p_path = Path(projects[name_str]['path']).resolve()
                if is_wiki_root(p_path):
                    return p_path
            raise WikiRootError(
                f'not a Podarcis checkout at {path} '
                f'(no AGENTS.md + .podarcis/config.yaml or podarcis.yaml). Pass --root.'
            )
        return path

    env_root = (env.get('PODARCIS_ROOT') or env.get('PODARCIS_PROJECT') or env.get('PROJECT_ROOT') or '').strip()
    if env_root:
        path = Path(env_root).expanduser().resolve()
        if not is_wiki_root(path):
            from podarcis.project import list_projects
            projects = list_projects()
            if env_root in projects and projects[env_root].get('path'):
                p_path = Path(projects[env_root]['path']).resolve()
                if is_wiki_root(p_path):
                    return p_path
            raise WikiRootError(
                f'not a Podarcis checkout at {path} '
                f'(no AGENTS.md + .podarcis/config.yaml or podarcis.yaml). Pass --root.'
            )
        return path

    start = Path(cwd) if cwd is not None else Path.cwd()
    start = start.resolve()
    for candidate in (start, *start.parents):
        if is_wiki_root(candidate):
            return candidate

    # Fallback to global active project if configured
    try:
        from podarcis.project import resolve_project
        proj = resolve_project(cwd=cwd, environ=environ)
        if proj.exists() and is_wiki_root(proj.root):
            return proj.root
    except Exception:
        pass

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
