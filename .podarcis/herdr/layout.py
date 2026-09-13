'''Wiki layout: labelled shells, then pane run / agent start.'''

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from podarcis.tui import PANE_AGENT, PANE_EDIT, PANE_FILES, WORKSPACE_LABEL


FILES_RATIO = 0.20
EDIT_RATIO = 0.625
SHELL_NAMES = frozenset({'bash', 'zsh', 'fish', 'sh', 'dash', 'ash', 'ksh', 'mksh', 'nu'})

Cli = Callable[..., dict]


def pane_env(wiki_root: Path) -> dict[str, str]:
    return {'PROJECT_ROOT': str(wiki_root)}


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
    info = result.get('process_info', result)
    procs = info.get('foreground_processes') or []
    if not procs:
        return True
    return all(_proc_name(p) in SHELL_NAMES for p in procs)


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


def apply_socket_layout(rpc: Callable[[str, dict], dict], workspace_id: str, wiki_root: Path) -> dict[str, str]:
    '''``layout.apply`` with commands omitted, then read labelled pane ids.'''
    tree = layout_tree(wiki_root)
    result = rpc('layout.apply', {
        'workspace_id': workspace_id,
        'tab_label': tree['tab_label'],
        'focus': True,
        'root': tree['root'],
    })
    layout = result.get('layout') or result
    acc: dict[str, str] = {'workspace': workspace_id}
    _collect_labelled(layout.get('root') or {}, acc)
    return acc
