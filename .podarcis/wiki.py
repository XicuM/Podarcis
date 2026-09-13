'''``podarcis wiki``: hand off to the Rust front-end, and its ``search`` sibling.

The front-end is a compiled binary (``tui/``), not a Python process. This module
only resolves it, builds it on demand, and ``execvp``s into it — everything the
user sees is Rust.
'''

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from podarcis.console import console
from podarcis.root import WikiRootError, find_wiki_root

BINARY = 'podarcis-tui'
CRATE_DIR = 'tui'

INSTALL_HINT = (
    'Build it with `cargo build --release --manifest-path tui/Cargo.toml`, '
    'or run `podarcis install`.'
)


def crate_dir(root: Path) -> Path:
    '''The Cargo crate inside the checkout.'''
    return Path(root) / CRATE_DIR


def _source_mtime(root: Path) -> float:
    '''Newest mtime among files that change the compiled frontend.'''
    newest = 0.0
    crate = crate_dir(root)
    watched = [crate / 'Cargo.toml', crate / 'Cargo.lock', crate / 'src', crate / 'assets']
    for path in watched:
        if path.is_file():
            newest = max(newest, path.stat().st_mtime)
        elif path.is_dir():
            for child in path.rglob('*'):
                if child.is_file():
                    newest = max(newest, child.stat().st_mtime)
    return newest


def crate_binary_is_stale(root: Path, binary: Path) -> bool:
    '''True when ``binary`` is this crate's build and sources are newer.

    Env overrides and ``$PATH`` copies are left alone — those are not ours to rebuild.
    '''
    try:
        binary.resolve().relative_to((crate_dir(root) / 'target').resolve())
    except (ValueError, OSError):
        return False
    try:
        return _source_mtime(root) > binary.stat().st_mtime
    except OSError:
        return True


def find_binary(root: Path | None = None) -> Path | None:
    '''Resolve the front-end: release build, then debug, then ``$PATH``.

    Release first so a stale debug build never shadows a fresh release one.
    '''
    override = (os.environ.get('PODARCIS_TUI_BIN') or '').strip()
    if override:
        path = Path(override).expanduser()
        return path if path.is_file() else None
    if root is not None:
        for profile in ('release', 'debug'):
            candidate = crate_dir(root) / 'target' / profile / BINARY
            if candidate.is_file():
                return candidate
    found = shutil.which(BINARY)
    return Path(found) if found else None


def build(root: Path, *, release: bool = True, quiet: bool = False) -> bool:
    '''Compile the front-end. Returns False when cargo is unavailable.'''
    manifest = crate_dir(root) / 'Cargo.toml'
    if not manifest.is_file():
        if not quiet:
            console.print(f'[yellow]No crate at {manifest}; skipping frontend build.[/yellow]')
        return False
    cargo = shutil.which('cargo')
    if cargo is None:
        if not quiet:
            console.print(
                '[yellow]cargo not found — the wiki frontend will not be built.[/yellow]\n'
                '[dim]Install Rust from https://rustup.rs and re-run `podarcis install`.[/dim]'
            )
        return False
    cmd = [cargo, 'build', '--manifest-path', str(manifest)]
    if release:
        cmd.append('--release')
    if quiet:
        cmd.append('--quiet')
    return subprocess.run(cmd).returncode == 0


def version(root: Path | None = None) -> str | None:
    '''``podarcis-tui --version`` output, or None when it cannot be run.'''
    binary = find_binary(root)
    if binary is None:
        return None
    try:
        done = subprocess.run(
            [str(binary), '--version'], capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return (done.stdout or '').strip().split(' ')[-1] or None


def cmd_wiki(args: argparse.Namespace) -> int:
    '''Launch the front-end, replacing this process.'''
    try:
        wiki_root = find_wiki_root(explicit=getattr(args, 'root', None))
    except WikiRootError as exc:
        console.print(f'[bold red]Error:[/bold red] {exc.message}')
        return 1

    binary = find_binary(wiki_root)
    stale = binary is not None and crate_binary_is_stale(wiki_root, binary)
    if (binary is None or stale) and shutil.which('cargo') is not None:
        reason = 'sources changed' if stale else 'first run'
        console.print(f'[dim]Building the wiki frontend ({reason})…[/dim]')
        if not build(wiki_root):
            if binary is None:
                console.print(f'[bold red]Error:[/bold red] {BINARY} not found. {INSTALL_HINT}')
                return 1
        else:
            binary = find_binary(wiki_root)
    if binary is None:
        console.print(f'[bold red]Error:[/bold red] {BINARY} not found. {INSTALL_HINT}')
        return 1

    argv = [str(binary), '--root', str(wiki_root)]
    path = getattr(args, 'path', None)
    if path:
        argv.append(str(path))

    # Hand the terminal over: the front-end owns the screen from here.
    try:
        os.execv(str(binary), argv)
    except OSError as exc:
        console.print(f'[bold red]Error:[/bold red] could not run {binary}: {exc}')
        return 1
    return 0  # pragma: no cover - execv does not return


def cmd_wiki_search(args: argparse.Namespace) -> int:
    '''Structured wiki search. The front-end shells out to exactly this.'''
    from podarcis.search import search

    try:
        wiki_root = find_wiki_root(explicit=getattr(args, 'root', None))
    except WikiRootError as exc:
        console.print(f'[bold red]Error:[/bold red] {exc.message}')
        return 1

    query = args.query if isinstance(args.query, str) else ' '.join(args.query)
    result = search(
        wiki_root, query,
        collection=getattr(args, 'collection', 'wiki') or 'wiki',
        method=getattr(args, 'method', 'hybrid') or 'hybrid',
        limit=getattr(args, 'limit', 20) or 20,
        no_rerank=bool(getattr(args, 'no_rerank', False)),
    )
    if getattr(args, 'json', False):
        print(json.dumps(result, indent=2))
        return 0
    if result.get('warning'):
        console.print(f'[yellow]{result["warning"]}[/yellow]')
    hits = result.get('hits') or []
    if not hits:
        console.print(f'[yellow]No hits for {query!r} in {result.get("collection")}.[/yellow]')
        return 0
    for hit in hits:
        score = hit.get('score')
        score_s = f'{score:.2f}' if isinstance(score, (int, float)) else '--'
        console.print(f'[bold]{score_s}[/bold]  {hit.get("title")}  [dim]{hit.get("path")}[/dim]')
        if hit.get('snippet'):
            console.print(f'   {hit["snippet"]}')
    return 0


def cmd_wiki_build(args: argparse.Namespace) -> int:
    '''Compile the front-end explicitly.'''
    try:
        wiki_root = find_wiki_root(explicit=getattr(args, 'root', None))
    except WikiRootError as exc:
        console.print(f'[bold red]Error:[/bold red] {exc.message}')
        return 1
    ok = build(wiki_root, release=not getattr(args, 'debug', False))
    if not ok:
        return 1
    binary = find_binary(wiki_root)
    console.print(f'[bold green]✓ Built {binary}[/bold green]')
    return 0


SUBCOMMANDS = ('open', 'search', 'build')


GLOBAL_FLAGS_WITH_VALUE = ('--root',)
GLOBAL_FLAGS = ('-v', '--version', '-i', '--interactive')


def _skip_flags(argv: list[str], start: int, with_value: tuple[str, ...]) -> int | None:
    '''Index of the first non-flag token at or after ``start``.

    Returns None when ``--help`` is present, so help is never rewritten.
    '''
    i = start
    while i < len(argv):
        token = argv[i]
        if token in ('-h', '--help'):
            return None
        if token in with_value:
            i += 2
            continue
        if any(token.startswith(f'{flag}=') for flag in with_value) or token in GLOBAL_FLAGS:
            i += 1
            continue
        if token.startswith('-'):
            return i
        return i
    return i


def normalize_argv(argv: list[str]) -> list[str]:
    '''Insert the implied ``open`` so ``podarcis wiki PAGE.md`` still works.

    argparse cannot have both an optional positional and subparsers on one
    parser — the first token is always claimed by whichever is declared first.
    Rather than hand-rolling a second parser (which is how the old front-end
    ended up with two disagreeing copies of its flag handling), the ambiguity is
    resolved once, here, the way git resolves it: a subcommand name wins, and
    anything else is a path.
    '''
    argv = list(argv)
    # `wiki` only means the subcommand in the subcommand slot: `podarcis lint
    # wiki` is a path argument and must be left alone.
    at = _skip_flags(argv, 0, GLOBAL_FLAGS_WITH_VALUE)
    if at is None or at >= len(argv) or argv[at] != 'wiki':
        return argv

    after = _skip_flags(argv, at + 1, ('--root',))
    if after is None:
        return argv
    if after < len(argv) and argv[after] in SUBCOMMANDS:
        return argv
    return argv[:after] + ['open'] + argv[after:]


def add_arguments(parser: argparse.ArgumentParser, *, add) -> None:
    '''Wire ``podarcis wiki`` and its subcommands onto ``parser``.

    A real subparser, not ``argparse.REMAINDER``: the old parser swallowed every
    flag after ``wiki`` and re-parsed it by hand in two places that disagreed.
    '''
    sub = parser.add_subparsers(dest='wiki_command', metavar='')

    open_p = add('open', 'Open the wiki frontend', cmd_wiki, parent=sub)
    open_p.add_argument('path', nargs='?', default=None, help='Page to open')

    search_p = add(
        'search', 'Search wiki, protocols and sources', cmd_wiki_search, parent=sub,
    )
    search_p.add_argument('query', nargs='+', help='Search query')
    search_p.add_argument('--json', action='store_true', help='Output structured hits as JSON')
    search_p.add_argument(
        '--collection', default='wiki', choices=['wiki', 'protocols', 'sources', 'all'],
        help='Collection to search (default: wiki)',
    )
    search_p.add_argument(
        '--method', default='hybrid', choices=['hybrid', 'semantic', 'keyword'],
        help='Search strategy (default: hybrid)',
    )
    search_p.add_argument('--limit', type=int, default=20, help='Maximum hits')
    search_p.add_argument('--no-rerank', action='store_true', dest='no_rerank')

    build_p = add('build', 'Compile the wiki frontend', cmd_wiki_build, parent=sub)
    build_p.add_argument('--debug', action='store_true', help='Build the debug profile')
