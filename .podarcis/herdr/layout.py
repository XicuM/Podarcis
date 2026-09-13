'''Wiki layout: labelled shells, then pane run / agent start.'''

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Callable

from podarcis.tui import PANE_AGENT, PANE_EDIT, PANE_FILES, WORKSPACE_LABEL


FILES_RATIO = 0.20
EDIT_RATIO = 0.625
SHELL_NAMES = frozenset({'bash', 'zsh', 'fish', 'sh', 'dash', 'ash', 'ksh', 'mksh', 'nu'})
NVIM_NAMES = frozenset({'nvim', 'nvim.exe', 'nvim-qt'})
HELIX_NAMES = frozenset({'helix', 'hx', 'helix.exe'})

Cli = Callable[..., dict]


def pane_env(wiki_root: Path, *, yazi_config_home: str | None = None) -> dict[str, str]:
    env = {'PROJECT_ROOT': str(wiki_root)}
    bindir = str(Path(sys.executable).parent)
    path = os.environ.get('PATH', '')
    if bindir and bindir not in path.split(os.pathsep):
        env['PATH'] = bindir + os.pathsep + path
    if yazi_config_home:
        env['YAZI_CONFIG_HOME'] = yazi_config_home
    return env


def yazi_flavor_dir(flavor_dir: Path) -> Path | None:
    cfg = Path(flavor_dir) / 'flavors' / 'yazi'
    if (cfg / 'yazi.toml').is_file():
        return cfg
    return None


def nvim_wiki_lua(flavor_dir: Path) -> Path | None:
    lua = Path(flavor_dir) / 'flavors' / 'nvim' / 'wiki.lua'
    return lua if lua.is_file() else None


def helix_flavor_config(flavor_dir: Path) -> Path | None:
    cfg = Path(flavor_dir) / 'flavors' / 'helix' / 'config.toml'
    return cfg if cfg.is_file() else None


def _bin_name(argv: list[str]) -> str:
    if not argv:
        return ''
    return Path(argv[0]).name.lstrip('-').lower()


def files_run_argv(
    file_manager: list[str] | None,
    file_manager_name: str | None,
    wiki_root: Path,
    flavor_dir: Path,
) -> list[str] | None:
    '''Pane-run argv for the files pane. lf stays unflavored (no LF_CONFIG_HOME).'''
    if not file_manager:
        return None
    argv = list(file_manager)
    if file_manager_name == 'yazi':
        cfg = yazi_flavor_dir(flavor_dir)
        if cfg is not None:
            argv = ['env', f'YAZI_CONFIG_HOME={cfg}', *argv]
    return [*argv, str(wiki_root)]


def editor_run_argv(
    editor: list[str],
    flavor_dir: Path,
    open_path: Path | None = None,
) -> list[str]:
    '''Pane-run argv for the edit pane. nvim keeps user ~/.config/nvim (luafile only).'''
    argv = list(editor)
    name = _bin_name(argv)
    if name in NVIM_NAMES or name.startswith('nvim'):
        lua = nvim_wiki_lua(flavor_dir)
        if lua is not None:
            argv += ['-c', f'luafile {lua}']
    elif name in HELIX_NAMES:
        cfg = helix_flavor_config(flavor_dir)
        if cfg is not None:
            argv += ['--config', str(cfg)]
    if open_path is not None:
        argv.append(str(open_path))
    return argv


def layout_tree(wiki_root: Path) -> dict:
    '''Portable layout.apply tree. Commands omitted: shells only.'''
    cwd = str(wiki_root)
    env = pane_env(wiki_root)
    return {
        'tab_label': 'wiki',
        'root': {
            'type': 'split',
            'direction': 'right',
            'ratio': FILES_RATIO,
            'first': {'type': 'pane', 'label': PANE_FILES, 'cwd': cwd, 'env': env},
            'second': {
                'type': 'split',
                'direction': 'right',
                'ratio': EDIT_RATIO,
                'first': {'type': 'pane', 'label': PANE_EDIT, 'cwd': cwd, 'env': env},
                'second': {'type': 'pane', 'label': PANE_AGENT, 'cwd': cwd, 'env': env},
            },
        },
    }


def _proc_name(proc: dict) -> str:
    raw = proc.get('name') or proc.get('argv0') or ''
    return Path(str(raw)).name.lstrip('-').lower()


def is_shell_foreground(result: dict) -> bool:
    '''True only with a positive shell signal — empty process lists keep polling.'''
    info = result.get('process_info', result)
    procs = info.get('foreground_processes') or []
    if procs:
        return all(_proc_name(p) in SHELL_NAMES for p in procs)
    return info.get('shell_pid') is not None


def is_named_shell_foreground(result: dict) -> bool:
    '''True only when a named shell is in ``foreground_processes``. Empty lists are busy.'''
    info = result.get('process_info', result)
    procs = info.get('foreground_processes') or []
    if not procs:
        return False
    return all(_proc_name(p) in SHELL_NAMES for p in procs)


def foreground_occupant(result: dict) -> str | None:
    '''Foreground process name. Empty lists are not idle, even with ``shell_pid``.'''
    info = result.get('process_info', result)
    procs = info.get('foreground_processes') or []
    non_shell = []
    for proc in procs:
        name = _proc_name(proc)
        if name and name not in SHELL_NAMES:
            non_shell.append(name)
    if non_shell:
        return non_shell[0]
    if procs:
        return _proc_name(procs[0]) or 'shell'
    return None


def is_nvim(name: str | None) -> bool:
    n = (name or '').lower()
    return n in NVIM_NAMES or n.startswith('nvim')


def is_helix(name: str | None) -> bool:
    n = (name or '').lower()
    return n in HELIX_NAMES or n.startswith('helix')


def wait_for_shell(cli: Cli, pane_id: str, *, timeout_s: float = 5.0, interval_s: float = 0.1) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        info = cli('pane', 'process-info', '--pane', pane_id)
        if is_shell_foreground(info):
            return
        time.sleep(interval_s)
    raise RuntimeError(
        f'agent pane {pane_id} did not become a shell within {timeout_s:.0f}s'
    )


def _pane_id(result: dict) -> str:
    pane = result.get('pane')
    if isinstance(pane, dict) and pane.get('pane_id'):
        return pane['pane_id']
    pane_id = result.get('pane_id')
    if pane_id:
        return str(pane_id)
    raise RuntimeError(f'herdr response has no pane_id: {result!r}')


def find_workspace(cli: Cli, label: str = WORKSPACE_LABEL) -> dict | None:
    data = cli('workspace', 'list')
    for ws in data.get('workspaces') or []:
        if ws.get('label') == label:
            return ws
    return None


def panes_by_label(cli: Cli) -> dict[str, str]:
    data = cli('pane', 'list')
    out: dict[str, str] = {}
    for pane in data.get('panes') or []:
        label = pane.get('label')
        pane_id = pane.get('pane_id')
        if label and pane_id:
            out[str(label)] = str(pane_id)
    return out


def _collect_labelled(node: dict, acc: dict[str, str]) -> None:
    ntype = node.get('type')
    if ntype == 'pane':
        label, pane_id = node.get('label'), node.get('pane_id')
        if label and pane_id:
            acc[str(label)] = str(pane_id)
        return
    if ntype == 'split':
        _collect_labelled(node.get('first') or {}, acc)
        _collect_labelled(node.get('second') or {}, acc)


def create_split_layout(cli: Cli, wiki_root: Path) -> dict[str, str]:
    '''Create shells, capture IDs from JSON, rename to files/edit/agent.'''
    env_arg = f'PROJECT_ROOT={wiki_root}'
    cwd = str(wiki_root)
    created = cli(
        'workspace', 'create',
        '--cwd', cwd, '--label', WORKSPACE_LABEL, '--env', env_arg, '--no-focus',
    )
    root_id = created['root_pane']['pane_id']
    workspace_id = created['workspace']['workspace_id']
    rest = cli(
        'pane', 'split', root_id,
        '--direction', 'right', '--ratio', str(FILES_RATIO),
        '--cwd', cwd, '--env', env_arg, '--no-focus',
    )
    rest_id = _pane_id(rest)
    agent = cli(
        'pane', 'split', rest_id,
        '--direction', 'right', '--ratio', str(EDIT_RATIO),
        '--cwd', cwd, '--env', env_arg, '--no-focus',
    )
    agent_id = _pane_id(agent)
    cli('pane', 'rename', root_id, PANE_FILES)
    cli('pane', 'rename', rest_id, PANE_EDIT)
    cli('pane', 'rename', agent_id, PANE_AGENT)
    return {
        'workspace': workspace_id,
        PANE_FILES: root_id,
        PANE_EDIT: rest_id,
        PANE_AGENT: agent_id,
    }


def _tab_id_for_workspace(cli: Cli, workspace_id: str) -> str | None:
    data = cli('workspace', 'get', workspace_id)
    ws = data.get('workspace') or data
    if ws.get('active_tab_id'):
        return str(ws['active_tab_id'])
    listed = cli('tab', 'list', '--workspace', workspace_id)
    for tab in listed.get('tabs') or []:
        if tab.get('tab_id'):
            return str(tab['tab_id'])
    return None


def apply_socket_layout(
    rpc: Callable[[str, dict], dict],
    workspace_id: str,
    wiki_root: Path,
    *,
    tab_id: str | None = None,
    cli: Cli | None = None,
) -> dict[str, str]:
    '''``layout.apply`` replacing ``tab_id`` (commands omitted), then labelled pane ids.

    Omitting ``tab_id`` would create a new tab and leave the old PTYs alive.
    '''
    resolved = (tab_id or '').strip() or None
    if resolved is None and cli is not None:
        resolved = _tab_id_for_workspace(cli, workspace_id)
    if not resolved:
        raise RuntimeError(f'cannot reset layout: no tab_id for workspace {workspace_id}')
    tree = layout_tree(wiki_root)
    result = rpc('layout.apply', {
        'workspace_id': workspace_id,
        'tab_id': resolved,
        'tab_label': tree['tab_label'],
        'focus': True,
        'root': tree['root'],
    })
    layout = result.get('layout') or result
    acc: dict[str, str] = {'workspace': workspace_id}
    _collect_labelled(layout.get('root') or {}, acc)
    return acc
