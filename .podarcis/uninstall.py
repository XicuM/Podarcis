#!/usr/bin/env python3
'''Podarcis uninstaller — removes symlinks, virtualenv, and build artefacts created by install.py.

Does NOT touch wiki/, workspace/, or sources/.
'''

import argparse
import os
import shutil
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
podarcis_dir = Path(__file__).resolve().parent
if str(root) not in sys.path:
    sys.path.insert(0, str(root))
if str(podarcis_dir) not in sys.path:
    sys.path.insert(0, str(podarcis_dir))

from console import console
from rich.panel import Panel
from rich.table import Table


# ── helpers ─────────────────────────────────────────────────────────────────

def _say(*args: str) -> None:
    console.print(''.join(args))


def _hr() -> None:
    _say('[dim]' + '─' * 76 + '[/dim]')


def _confirm(prompt: str, default: bool = False, yes: bool = False) -> bool:
    '''Return True if the user confirms (or --yes was passed).'''
    if yes:
        return True
    suffix = ' [Y/n]' if default else ' [y/N]'
    try:
        answer = input(f'{prompt}{suffix}: ').strip().lower()
    except (EOFError, KeyboardInterrupt):
        _say('\n[yellow]Cancelled.[/yellow]')
        sys.exit(1)
    if not answer:
        return default
    return answer in ('y', 'yes')


# ── removal steps ────────────────────────────────────────────────────────────

def _find_global_symlinks() -> list[Path]:
    '''Return all symlinks in ~/.local/bin that point at <root>/podarcis.'''
    local_bin = Path.home() / '.local' / 'bin'
    podarcis_script = root / 'podarcis'
    found: list[Path] = []
    if not local_bin.exists():
        return found
    for entry in local_bin.iterdir():
        if entry.is_symlink():
            try:
                target = entry.resolve()
                if target == podarcis_script.resolve():
                    found.append(entry)
            except OSError:
                pass
    return found


def _remove_global_symlinks(symlinks: list[Path], dry_run: bool) -> int:
    '''Remove the discovered symlinks. Returns count removed.'''
    removed = 0
    for link in symlinks:
        if dry_run:
            _say(f'  [dim](dry-run)[/dim] Would remove symlink [cyan]{link}[/cyan]')
        else:
            try:
                link.unlink()
                _say(f'  [green]✓[/green] Removed symlink [cyan]{link}[/cyan]')
                removed += 1
            except OSError as exc:
                _say(f'  [red]✗[/red] Failed to remove {link}: {exc}')
    return removed


def _remove_venv(dry_run: bool) -> bool:
    '''Remove the .venv directory. Returns True if action was taken.'''
    venv_dir = root / '.venv'
    if not venv_dir.exists():
        _say('  [dim].venv not found, skipping.[/dim]')
        return False
    size_mb = sum(
        f.stat().st_size for f in venv_dir.rglob('*') if f.is_file()
    ) / (1024 * 1024)
    if dry_run:
        _say(f'  [dim](dry-run)[/dim] Would remove [cyan]{venv_dir}[/cyan] ({size_mb:.1f} MB)')
        return False
    shutil.rmtree(venv_dir, ignore_errors=True)
    _say(f'  [green]✓[/green] Removed [cyan]{venv_dir}[/cyan] ({size_mb:.1f} MB freed)')
    return True


def _remove_build_artefacts(dry_run: bool) -> list[Path]:
    '''Remove egg-info, __pycache__, and .pyc files. Returns list of removed paths.'''
    removed: list[Path] = []

    # egg-info directories
    for egg_info in root.glob('*.egg-info'):
        if dry_run:
            _say(f'  [dim](dry-run)[/dim] Would remove [cyan]{egg_info.name}/[/cyan]')
        else:
            shutil.rmtree(egg_info, ignore_errors=True)
            _say(f'  [green]✓[/green] Removed [cyan]{egg_info.name}/[/cyan]')
            removed.append(egg_info)

    # inner egg-info (e.g. .podarcis/*.egg-info)
    for egg_info in (root / '.podarcis').glob('*.egg-info'):
        if dry_run:
            _say(f'  [dim](dry-run)[/dim] Would remove [cyan].podarcis/{egg_info.name}/[/cyan]')
        else:
            shutil.rmtree(egg_info, ignore_errors=True)
            _say(f'  [green]✓[/green] Removed [cyan].podarcis/{egg_info.name}/[/cyan]')
            removed.append(egg_info)

    # __pycache__ directories (project tree only, skip .venv to avoid re-scanning a removed dir)
    for cache_dir in root.rglob('__pycache__'):
        if '.venv' in cache_dir.parts:
            continue
        if dry_run:
            _say(f'  [dim](dry-run)[/dim] Would remove [cyan]{cache_dir.relative_to(root)}/[/cyan]')
        else:
            shutil.rmtree(cache_dir, ignore_errors=True)
            _say(f'  [green]✓[/green] Removed [cyan]{cache_dir.relative_to(root)}/[/cyan]')
            removed.append(cache_dir)

    # .pyc files outside __pycache__ (uncommon but possible)
    for pyc in root.rglob('*.pyc'):
        if '.venv' in pyc.parts or '__pycache__' in pyc.parts:
            continue
        if dry_run:
            _say(f'  [dim](dry-run)[/dim] Would remove [cyan]{pyc.relative_to(root)}[/cyan]')
        else:
            pyc.unlink(missing_ok=True)
            _say(f'  [green]✓[/green] Removed [cyan]{pyc.relative_to(root)}[/cyan]')
            removed.append(pyc)

    # pytest cache
    pytest_cache = root / '.pytest_cache'
    if pytest_cache.exists():
        if dry_run:
            _say(f'  [dim](dry-run)[/dim] Would remove [cyan].pytest_cache/[/cyan]')
        else:
            shutil.rmtree(pytest_cache, ignore_errors=True)
            _say(f'  [green]✓[/green] Removed [cyan].pytest_cache/[/cyan]')
            removed.append(pytest_cache)

    return removed


def _remove_config(dry_run: bool) -> bool:
    '''Remove .podarcis/config.yaml. Returns True if action taken.'''
    config_yaml = root / '.podarcis' / 'config.yaml'
    if not config_yaml.exists():
        _say('  [dim]config.yaml not found, skipping.[/dim]')
        return False
    if dry_run:
        _say(f'  [dim](dry-run)[/dim] Would remove [cyan].podarcis/config.yaml[/cyan]')
        return False
    config_yaml.unlink()
    _say('  [green]✓[/green] Removed [cyan].podarcis/config.yaml[/cyan]')
    return True


# ── preview table ────────────────────────────────────────────────────────────

def _print_preview(symlinks: list[Path], purge: bool) -> None:
    table = Table(show_header=True, header_style='bold #29b8db', box=None, padding=(0, 2))
    table.add_column('Item', style='cyan')
    table.add_column('Path', style='white')
    table.add_column('Note', style='dim')

    for link in symlinks:
        table.add_row('symlink', str(link), 'global CLI entry')

    venv_dir = root / '.venv'
    if venv_dir.exists():
        table.add_row('.venv/', str(venv_dir), 'virtualenv & dependencies')

    table.add_row('build artefacts', str(root), 'egg-info, __pycache__, .pyc, .pytest_cache')

    if purge:
        config_yaml = root / '.podarcis' / 'config.yaml'
        if config_yaml.exists():
            table.add_row('config.yaml', str(config_yaml), '--purge flag set')

    console.print(table)


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog='podarcis uninstall',
        description='Remove Podarcis tooling artefacts (symlink, venv, build files).',
    )
    parser.add_argument('-y', '--yes', action='store_true', help='Skip all confirmations')
    parser.add_argument('--dry-run', action='store_true', help='Show what would be removed without touching anything')
    parser.add_argument('--purge', action='store_true', help='Also remove .podarcis/config.yaml')
    args = parser.parse_args()

    dry_run: bool = args.dry_run
    yes: bool = args.yes
    purge: bool = args.purge

    console.print(Panel(
        '[bold #29b8db]Podarcis Uninstaller[/bold #29b8db]\n\n'
        'Removes the global CLI symlink, virtual environment, and build artefacts\n'
        'created by [bold]podarcis install[/bold].\n\n'
        '[dim]Wiki, workspace, and source repositories are [bold]never[/bold] touched.[/dim]',
        border_style='#29b8db', width=76, expand=False,
    ))
    _hr()

    symlinks = _find_global_symlinks()

    _say('\n[bold white]The following will be removed:[/bold white]\n')
    _print_preview(symlinks, purge)

    if dry_run:
        _say('\n[bold yellow]⚠  Dry-run mode — no files will be modified.[/bold yellow]\n')
        _hr()

    if not dry_run:
        _say()
        if not _confirm('Proceed with uninstall?', default=False, yes=yes):
            _say('[yellow]Aborted.[/yellow]')
            sys.exit(0)
        _say()

    # ── 1. symlinks ──────────────────────────────────────────────────────────
    _say('[bold white]Removing global symlink(s)...[/bold white]')
    if symlinks:
        _remove_global_symlinks(symlinks, dry_run)
    else:
        _say('  [dim]No symlinks found in ~/.local/bin pointing at this project.[/dim]')
    _say()

    # ── 2. venv ──────────────────────────────────────────────────────────────
    _say('[bold white]Removing virtual environment...[/bold white]')
    _remove_venv(dry_run)
    _say()

    # ── 3. build artefacts ───────────────────────────────────────────────────
    _say('[bold white]Removing build artefacts...[/bold white]')
    _remove_build_artefacts(dry_run)
    _say()

    # ── 4. config (optional) ─────────────────────────────────────────────────
    if purge:
        _say('[bold white]Removing config.yaml (--purge)...[/bold white]')
        _remove_config(dry_run)
        _say()

    _hr()

    if dry_run:
        console.print(Panel(
            '[bold yellow]Dry-run complete.[/bold yellow]\n'
            'No files were modified. Run without [bold]--dry-run[/bold] to apply.',
            border_style='yellow', width=76, expand=False,
        ))
    else:
        console.print(Panel(
            '[bold green]✓ Uninstall complete.[/bold green]\n\n'
            'Tooling artefacts removed. Your research data (wiki/, workspace/) is intact.\n\n'
            '[dim]To reinstall at any time, run:[/dim]\n'
            '  [bold]python .podarcis/install.py[/bold]  or  [bold]make install[/bold]',
            border_style='green', width=76, expand=False,
        ))


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
