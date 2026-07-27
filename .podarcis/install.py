#!/usr/bin/env python3
'''Automated bootstrap, MCP/skill config, and credential setup.'''

import os
import shutil
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
podarcis_dir = Path(__file__).resolve().parent
if str(root) not in sys.path:
    sys.path.insert(0, str(root))
if str(podarcis_dir) not in sys.path:
    sys.path.insert(0, str(podarcis_dir))

from banner import display_install_banner
from common import load_yaml, run_command, save_yaml
from console import console, QSTYLE
from rich.panel import Panel

def _say(*args: str) -> None:
    console.print(''.join(args))

def _box(title: str, text: str, border_style: str = '#29b8db') -> None:
    console.print(Panel(text.strip(), title=f'[bold {border_style}]{title}[/bold {border_style}]', border_style=border_style, width=76, expand=False))

def _hr() -> None:
    _say('[dim]' + '─' * 76 + '[/dim]')

def _select(prompt: str, choices: list[str], default: str | None = None, qmark: str = '?') -> str:
    '''Arrow-key select prompt.'''
    import questionary
    ans = questionary.select(
        prompt, choices=choices, default=default, qmark=qmark,
        style=questionary.Style(QSTYLE),
    ).ask()
    if ans is None:
        console.print('\n[yellow]Cancelled by user. Exiting.[/yellow]')
        raise SystemExit(1)
    return ans

# ── venv bootstrap ─────────────────────────────────────────────────────────

def _bootstrap_venv() -> None:
    venv_dir = root / '.venv'
    exe_suffix = 'Scripts' if sys.platform == 'win32' else 'bin'
    python = venv_dir / exe_suffix / 'python'
    pip = venv_dir / exe_suffix / ('pip.exe' if sys.platform == 'win32' else 'pip')

    if sys.executable == str(python):
        return  # already inside venv

    if not venv_dir.exists():
        _say('[green]✓ Creating Python virtual environment (.venv)...[/green]')
        run_command([sys.executable, '-m', 'venv', str(venv_dir)])

    deps_ok = (
        python.exists()
        and subprocess.run(
            [str(python), '-c', 'import rich, questionary'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0
    )

    if not deps_ok:
        req_file = podarcis_dir / 'requirements.txt'
        if not req_file.exists():
            req_file = root / 'requirements.txt'

        _say(f'[green]✓ Installing dependencies from {req_file.name}...[/green]')

        # Bootstrap pip inside the venv via ensurepip when pip binary is absent
        # (common on Debian/Ubuntu where system Python ships without pip).
        if not pip.exists():
            _say('[green]✓ Bootstrapping pip via ensurepip...[/green]')
            run_command([str(python), '-m', 'ensurepip', '--upgrade'])

        # Always prefer the venv pip; fall back to venv python -m pip (never system pip).
        pip_cmd: list[str] = [str(pip)] if pip.exists() else [str(python), '-m', 'pip']

        run_command(pip_cmd + ['install', '--upgrade', 'pip'])
        if req_file.exists():
            run_command(pip_cmd + ['install', '-r', str(req_file)])
        run_command(pip_cmd + ['install', '-e', str(root)])


        if (egg_info := root / 'podarcis.egg-info').exists():
            shutil.rmtree(egg_info, ignore_errors=True)


    if (root / 'podarcis').exists():
        os.chmod(root / 'podarcis', 0o755)

    if python.exists():
        os.execv(str(python), [str(python)] + sys.argv)

# ── .podarcis/config.yaml ──────────────────────────────────────────────────

def _create_podarcis_yaml() -> None: 
    if (yaml := root/'.podarcis'/'config.yaml').exists(): return
    save_yaml(yaml, {
        'apis': {}, 'repositories': {}, 'engines': {'qmd': False},
        'oneliners': [
        "Agile enough to catch a dangling reference.",
        "Quick on its feet. Quicker on the audit.",
        "Wall-crawling through the taxonomy tree, link by link.",
        "Filesystem traversal at gecko speed.",
        "Podarcis: endemic to knowledge graphs everywhere.",
    ],
    })

def _configure_repos() -> None:
    from repos import get_repo_names, prompt_configure_repo
    import questionary

    _box(
        'Workspace Repositories',
        'Podarcis manages knowledge across two Open Knowledge Format (OKF v0.2) repositories:\n'
        '  • wiki: Objective knowledge base (anonymized concepts & references)\n'
        '  • workspace: Actionable deliverables (user profiles, protocols, reviews)',
    )

    style = questionary.Style(QSTYLE)
    for name in get_repo_names(root):
        prompt_configure_repo(root, name, style=style)
    _say()

def _configure_mcp_servers() -> set[str]:
    import questionary
    from components import (
        discover_components, build_component_choices,
        get_enabled_mcp_servers, set_mcp_server_status, run_mcp_setup
    )

    servers, _, _ = discover_components(root)
    if not servers:
        _say('[dim]No MCP servers discovered.[/dim]')
        return set()

    _box(
        'MCP Servers Configuration',
        'Model Context Protocol (MCP) servers equip Podarcis subagents with external tool capabilities and data source integrations.',
    )

    enabled = get_enabled_mcp_servers(root)
    style = questionary.Style(QSTYLE)
    choices = build_component_choices(root, 'mcp', servers, enabled_set=enabled)
    selected = (
        questionary.checkbox(
            'Select MCP servers to activate:',
            choices=choices, style=style)
        .ask()
    )
    if selected is None:
        raise SystemExit(1)

    s = set(selected)

    for k in sorted(s):
        _hr()
        run_mcp_setup(root, k)

    for k, info in servers.items():
        set_mcp_server_status(root, k, k in s, info)
    _say('[bold green]✓ MCP server configurations updated.[/bold green]\n')
    return s

# ── skills ─────────────────────────────────────────────────────────────────

def _configure_skills() -> None:
    import questionary
    from components import discover_components, build_component_choices, set_skill_status

    _, skills, _ = discover_components(root)
    if not skills:
        _say('[dim]No skills discovered.[/dim]')
        return

    _box(
        'Agent Skills Configuration',
        'Skills extend subagents with specialized workflows, Python code execution tools, context compaction strategies, and runtime harnesses.',
    )

    style = questionary.Style(QSTYLE)
    choices = build_component_choices(root, 'skill', skills)
    selected = (
        questionary.checkbox(
            'Select skills to enable:', choices=choices, style=style)
        .ask()
    )
    if selected is None:
        raise SystemExit(1)

    s = set(selected)
    for k, info in skills.items():
        set_skill_status(root, k, k in s, info)
    _say('[bold green]✓ Skill configurations updated.[/bold green]\n')

# ── agents ─────────────────────────────────────────────────────────────────

def _configure_agents() -> None:
    import questionary
    from components import discover_components, build_component_choices, set_agent_status

    _, _, agents = discover_components(root)
    if not agents:
        _say('[dim]No agents discovered.[/dim]')
        return

    _box(
        'Subagent Personas Configuration',
        'Personas configure autonomous subagents defined under .agents/agents/ for multi-agent\n'
        'literature discovery, protocol architecture, and OKF concept verification.',
    )

    style = questionary.Style(QSTYLE)
    choices = build_component_choices(root, 'agent', agents)
    selected = (
        questionary.checkbox(
            'Select agents to enable:', choices=choices, style=style)
        .ask()
    )
    if selected is None:
        raise SystemExit(1)

    s = set(selected)
    for k, info in agents.items():
        set_agent_status(root, k, k in s, info)
    _say('[bold green]✓ Agent configurations updated.[/bold green]\n')

# ── global command ────────────────────────────────────────────────────────

def _configure_global_command() -> None:
    _box(
        'Global Executable Setup',
        'Optionally installs the "podarcis" CLI executable into ~/.local/bin '
        'allowing you to run status, config, test, and lint commands directly from any shell prompt.',
    )
    ans = _select('Install command globally in ~/.local/bin?', ['yes', 'no'], default='yes')
    if ans == 'yes':
        import questionary
        name = questionary.text(
            'Command name (Enter for "podarcis"):',
            default='podarcis',
            style=questionary.Style(QSTYLE),
        ).ask()
        if not name:
            name = 'podarcis'
        local_bin = Path.home() / '.local' / 'bin'
        local_bin.mkdir(parents=True, exist_ok=True)
        symlink_target = local_bin / name
        source_target = root / 'podarcis'
        try:
            if symlink_target.is_symlink() or symlink_target.exists():
                symlink_target.unlink()
            symlink_target.symlink_to(source_target)
            _say(f'[bold green]✓ Symlinked {symlink_target} → {source_target}[/bold green]')

            path_env = os.environ.get('PATH', '')
            if str(local_bin) not in path_env.split(os.pathsep):
                _say(f'[bold yellow]⚠️ Note: {local_bin} is not currently in your PATH.[/bold yellow]')
                _say(f'[dim]Add `export PATH="$HOME/.local/bin:$PATH"` to your ~/.bashrc or ~/.zshrc.[/dim]')
        except Exception as err:
            _say(f'[bold red]Failed to create symlink: {err}[/bold red]')
    else:
        _say('[dim]Skipped global command installation. You can run `./podarcis` from project root.[/dim]')
    _say()

# ── main ───────────────────────────────────────────────────────────────────

def main() -> None:
    _bootstrap_venv()

    console.clear()
    display_install_banner(root)

    _create_podarcis_yaml()
    _hr()

    enabled = _configure_mcp_servers()
    _hr()
    _configure_skills()
    _hr()
    _configure_agents()
    _hr()
    _configure_repos()
    _hr()
    _configure_global_command()
    _hr()

    if _select('Sync workspace repos now?', ['no', 'yes'], default='yes') == 'yes':
        from repos import sync_repos
        _say('[#29b8db]Syncing workspace repos...[/#29b8db]')
        sync_repos(root, clone_missing=True, update_remotes=True)
        _say()

    console.print(Panel(
            '[bold green]✓ Bootstrap & Setup Complete![/bold green]\n\n'
            '- Virtual environment configured, deps in .venv/\n'
            '- Configuration (.podarcis/config.yaml) prepared.\n\n'
            '[bold #29b8db]Quick Commands:[/bold #29b8db]\n'
            '  • Status:       [bold]podarcis status[/bold] (or [bold]podarcis status --json[/bold])\n'
            '  • CLI Config:   [bold]podarcis config enable/disable/repo[/bold]\n'
            '  • Interactive:  [bold]podarcis config interactive[/bold] (or [bold]make config[/bold])\n'
            '  • Test:         [bold]podarcis test[/bold] (or [bold]make test[/bold])\n'
            '  • Lint:         [bold]podarcis lint[/bold] (or [bold]make lint[/bold])',
            border_style='green', width=76, expand=False,
        ))

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
