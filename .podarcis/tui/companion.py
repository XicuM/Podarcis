'''Herdr companion client — same socket protocol as herdr-companion.

VS Code's extension shows agent status and starts conversations over
``session.snapshot`` / ``agent.start``. This is that client for the wiki TUI.
herdr is a sidecar daemon, not the window manager.
'''

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from podarcis.tui.server import HerdrSession

STATUS_EMOJI = {
    'working': '🟡',
    'blocked': '🔴',
    'done': '🟢',
    'idle': '⚪',
}


@dataclass
class Conversation:
    tab_id: str
    workspace_id: str
    pane_id: str
    agent: str | None
    status: str
    label: str
    running: bool

    @property
    def emoji(self) -> str:
        return STATUS_EMOJI.get(self.status, '')


def snapshot(session: HerdrSession) -> dict:
    data = session.rpc('session.snapshot')
    snap = data.get('snapshot', data)
    return snap if isinstance(snap, dict) else {}


def find_workspace(snap: dict, folder: Path) -> dict | None:
    norm = str(folder.resolve())
    base = folder.resolve().name
    workspaces = snap.get('workspaces') or []
    panes = snap.get('panes') or []

    def pane_in_folder(ws_id: str) -> bool:
        for pane in panes:
            if pane.get('workspace_id') != ws_id:
                continue
            cwd = pane.get('cwd') or pane.get('foreground_cwd') or ''
            try:
                if str(Path(cwd).resolve()) == norm:
                    return True
            except OSError:
                continue
        return False

    labelled = [w for w in workspaces if w.get('label') == base]
    for ws in labelled:
        if pane_in_folder(ws.get('workspace_id', '')):
            return ws
    for ws in workspaces:
        if pane_in_folder(ws.get('workspace_id', '')):
            return ws
    return labelled[0] if labelled else None


def ensure_workspace(session: HerdrSession, folder: Path, snap: dict | None = None) -> str:
    snap = snap if snap is not None else snapshot(session)
    existing = find_workspace(snap, folder)
    if existing and existing.get('workspace_id'):
        return str(existing['workspace_id'])
    created = session.rpc(
        'workspace.create',
        {'cwd': str(folder.resolve()), 'label': folder.resolve().name, 'focus': True},
    )
    ws = created.get('workspace') or created
    return str(ws['workspace_id'])


def conversations(snap: dict, folder: Path) -> list[Conversation]:
    ws = find_workspace(snap, folder)
    if not ws:
        return []
    ws_id = ws.get('workspace_id')
    panes = [p for p in (snap.get('panes') or []) if p.get('workspace_id') == ws_id]
    out: list[Conversation] = []
    for tab in snap.get('tabs') or []:
        if tab.get('workspace_id') != ws_id:
            continue
        pane = next((p for p in panes if p.get('tab_id') == tab.get('tab_id')), None) or {}
        agent = pane.get('agent') or None
        status = pane.get('agent_status') or tab.get('agent_status') or 'unknown'
        label = tab.get('label') or agent or 'shell'
        pane_id = str(pane.get('pane_id') or '')
        out.append(
            Conversation(
                tab_id=str(tab.get('tab_id') or ''),
                workspace_id=str(ws_id),
                pane_id=pane_id,
                agent=str(agent) if agent else None,
                status=str(status),
                label=str(label),
                running=bool(agent),
            )
        )
    return out


def focus_conversation(session: HerdrSession, conv: Conversation) -> None:
    if conv.workspace_id:
        try:
            session.rpc('workspace.focus', {'workspace_id': conv.workspace_id})
        except RuntimeError:
            pass
    if conv.tab_id:
        try:
            session.rpc('tab.focus', {'tab_id': conv.tab_id})
        except RuntimeError:
            pass
    if conv.pane_id:
        try:
            session.rpc('pane.focus', {'pane_id': conv.pane_id})
        except RuntimeError:
            pass


def start_conversation(
    session: HerdrSession,
    folder: Path,
    kind: str,
    *,
    name: str | None = None,
    args: list[str] | None = None,
) -> Conversation:
    '''Companion ``newConversation``: tab.create, then agent.start on the root pane.'''
    snap = snapshot(session)
    ws_id = ensure_workspace(session, folder, snap)
    session.rpc('workspace.focus', {'workspace_id': ws_id})
    tab_res = session.rpc('tab.create', {'workspace_id': ws_id, 'label': name, 'focus': True})
    tab = tab_res.get('tab') or {}
    root = tab_res.get('root_pane') or {}
    pane_id = str(root.get('pane_id') or '')
    tab_id = str(tab.get('tab_id') or '')
    if not pane_id:
        raise RuntimeError('herdr tab.create returned no root pane')
    agent_name = name or f'{kind}_{Path(folder).name}'
    params: dict = {'name': agent_name, 'kind': kind, 'pane_id': pane_id, 'timeout_ms': 45000}
    if args:
        params['args'] = args
    # Shell may not be ready yet; companion retries this.
    last_err: Exception | None = None
    for _ in range(8):
        try:
            session.rpc('agent.start', params, timeout_s=50.0)
            last_err = None
            break
        except RuntimeError as exc:
            last_err = exc
            if 'available shell' not in str(exc).lower():
                raise
            import time
            time.sleep(0.5)
    if last_err is not None:
        raise last_err
    return Conversation(
        tab_id=tab_id,
        workspace_id=ws_id,
        pane_id=pane_id,
        agent=kind,
        status='working',
        label=agent_name,
        running=True,
    )
