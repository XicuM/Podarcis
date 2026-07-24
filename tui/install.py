#!/usr/bin/env python3
'''Automated bootstrap, Git configuration, dependency setup, and Google Drive integration.'''

import os
import shutil
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

from tui.common import run_command
from tui.console import HAS_RICH, console
from tui.gdrive import setup_google_drive
from tui.repos import load_repos_config, set_repo_protocol, sync_repos

if HAS_RICH:
    from rich.panel import Panel
    from rich.prompt import Confirm, Prompt


def main() -> None:
    '''Bootstrap project dependencies, git options, submodules, and credentials.'''
    venv_dir = root / '.venv'
    venv_python = venv_dir / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
    venv_pip = venv_dir / ('Scripts/pip.exe' if sys.platform == 'win32' else 'bin/pip')

    if sys.executable != str(venv_python):
        if not venv_dir.exists():
            console.print('[green]✓ Creating Python virtual environment (.venv)...[/green]')
            run_command([sys.executable, '-m', 'venv', str(venv_dir)])

        has_rich = False
        if venv_python.exists():
            try:
                subprocess.run([str(venv_python), '-c', 'import rich'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                has_rich = True
            except subprocess.CalledProcessError:
                has_rich = False

        if not has_rich:
            console.print('[green]✓ Installing dependencies from requirements.txt...[/green]')
            pip_bin = str(venv_pip) if venv_pip.exists() else [sys.executable, '-m', 'pip']
            run_command([pip_bin, 'install', '--upgrade', 'pip'] if isinstance(pip_bin, str) else pip_bin + ['install', '--upgrade', 'pip'])
            run_command([pip_bin, 'install', '-r', 'requirements.txt'] if isinstance(pip_bin, str) else pip_bin + ['install', '-r', 'requirements.txt'])

        if venv_python.exists():
            console.print('[#29b8db]Re-launching setup inside virtual environment...[/#29b8db]')
            os.execv(str(venv_python), [str(venv_python)] + sys.argv)

    if HAS_RICH:
        console.print(Panel(
            '[bold green]Agentic Wiki Builder[/bold green]\n[dim]Complete Bootstrap, Setup, and Google Drive Integration[/dim]',
            border_style='green', expand=False
        ))
    else:
        console.rule('Agentic Wiki Builder — Complete Setup & Bootstrap')

    # 1. Git LFS Initialization
    if not shutil.which('git-lfs'):
        console.print('[bold yellow]⚠️  WARNING: "git-lfs" command not found.[/bold yellow]')
        console.print('   Please install Git LFS (https://git-lfs.com/) for tracking large raw sources.')
    else:
        console.print('[green]✓ Git LFS found. Registering settings...[/green]')
        run_command(['git', 'lfs', 'install'])
    console.print()

    # 2. Environment & Configuration Setup (podarcis.yaml)
    yaml_file, example_yaml = root / 'podarcis.yaml', root / 'podarcis.example.yaml'
    if not yaml_file.exists():
        if example_yaml.exists():
            console.print('[green]✓ Creating podarcis.yaml from podarcis.example.yaml...[/green]')
            shutil.copy(example_yaml, yaml_file)
            console.print('👉 Created podarcis.yaml. Update API credentials & repo URLs if necessary.')
        else:
            console.print('[bold yellow]⚠️ Could not find podarcis.example.yaml to generate podarcis.yaml.[/bold yellow]')
    else:
        console.print('[green]✓ podarcis.yaml configuration file found.[/green]')
    console.print()

    # 3. Optional Tool Engine Setup (QMD Vector DB)
    from tui.common import load_podarcis_config, set_engine_status
    pod_cfg = load_podarcis_config(root)
    current_qmd_enabled = bool(pod_cfg.get('engines', {}).get('qmd', False))

    console.print('[bold #29b8db]Optional Tool Engines Configuration:[/bold #29b8db]')
    console.print('   QMD Vector DB Engine provides semantic embedding search across the wiki.')
    console.print('   Native keyword search works cleanly without QMD.\n')

    if HAS_RICH:
        enable_qmd = Confirm.ask('Enable QMD Vector DB Search Engine in podarcis.yaml?', default=current_qmd_enabled)
    else:
        ans = input(f'Enable QMD Vector DB Search Engine in podarcis.yaml? (y/n) [{"y" if current_qmd_enabled else "n"}]: ').strip().lower()
        enable_qmd = ans == 'y' if ans else current_qmd_enabled

    set_engine_status(root, 'qmd', enable_qmd)

    if enable_qmd:
        if not shutil.which('qmd'):
            console.print('[bold yellow]⚠️ QMD CLI binary ("qmd") not found in PATH.[/bold yellow]')
            if shutil.which('npm'):
                if HAS_RICH:
                    install_now = Confirm.ask('Attempt installing @tobilu/qmd globally via npm now?', default=True)
                else:
                    install_now = input('Attempt installing @tobilu/qmd globally via npm now? (y/n): ').strip().lower() == 'y'
                if install_now:
                    console.print('[#29b8db]Installing @tobilu/qmd globally...[/#29b8db]')
                    run_command(['npm', 'install', '-g', '@tobilu/qmd'], check=False)
                    if shutil.which('qmd'):
                        console.print('[green]✓ QMD CLI installed successfully.[/green]')
                    else:
                        console.print('[bold yellow]⚠️ QMD CLI could not be automatically installed. wiki-mcp will warn and fall back to native search.[/bold yellow]')
            else:
                console.print('[bold red]⚠️ npm not found. Please install Node.js/npm and install @tobilu/qmd manually if desired.[/bold red]')
        else:
            console.print('[green]✓ QMD CLI found and enabled.[/green]')
    else:
        console.print('[yellow]✓ QMD Vector DB Engine disabled. wiki-mcp will use Native Keyword Search mode.[/yellow]')
    console.print()

    # 4. Workspace Repositories Check & Clone Protocol Setup
    console.print('[green]✓ Configuring workspace repository Git clone protocol...[/green]')
    repos_cfg = load_repos_config(root)
    current_proto = repos_cfg.get('protocol', 'ssh')

    if HAS_RICH:
        chosen_proto = Prompt.ask(
            '  Select Git clone protocol for workspace repositories',
            choices=['ssh', 'https'],
            default=current_proto
        )
    else:
        ans = input(f'  Select Git clone protocol for workspace repositories (ssh/https) [{current_proto}]: ').strip().lower()
        chosen_proto = ans if ans in ['ssh', 'https'] else current_proto

    set_repo_protocol(root, chosen_proto, update_existing_remotes=True)
    console.print(f'[green]✓ Git clone protocol set to: [bold]{chosen_proto.upper()}[/bold][/green]')

    console.print('[#29b8db]Synchronizing workspace repositories (wiki, workspace)...[/#29b8db]')
    sync_repos(root, clone_missing=True, update_remotes=True)
    console.print()

    # 5. Submodule Initialization
    console.print('[green]✓ Initializing and updating git submodules...[/green]')
    run_command(['git', '-c', 'protocol.file.allow=always', 'submodule', 'update', '--init', '--recursive'], check=False)
    console.print()

    # 6. Configure Git settings
    console.print('[green]✓ Configuring local Git settings for team collaboration...[/green]')
    run_command(['git', 'config', 'submodule.recurse', 'true'])
    run_command(['git', 'config', 'push.recurseSubmodules', 'on-demand'])
    console.print()


    # 7. Google Drive Credentials Setup
    try:
        setup_google_drive(root)
    except Exception as e:
        console.print(f'[bold yellow]⚠️ Google Drive credentials setup encountered an issue: {e}[/bold yellow]')
    console.print()

    if HAS_RICH:
        console.print(Panel(
            '[bold green]✓ Bootstrap & Setup Complete![/bold green]\n\n'
            '- Submodules will now automatically pull updates during a standard "git pull".\n'
            '- Virtual environment configured and dependencies installed in .venv/\n'
            '- Configuration & secrets (podarcis.yaml) prepared.\n\n'
            '[bold #29b8db]Quick Commands:[/bold #29b8db]\n'
            '  • Manage servers/skills: [bold]make config[/bold]\n'
            '  • Run tests:             [bold]make test[/bold]\n'
            '  • Sync workspace:        [bold]make sync[/bold]\n'
            '  • Lint links:            [bold]make lint[/bold]',
            border_style='green', expand=False
        ))
    else:
        console.rule('Setup Complete!')
        print('Run "make test" or "pytest" to verify installation.')


if __name__ == '__main__':
    main()
