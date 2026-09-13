'''herdr-companion socket client (agent list / start), not a nested herdr TUI.'''

from __future__ import annotations

from pathlib import Path

from podarcis.tui.companion import conversations, find_workspace
from podarcis.tui.launch import _agents_panel_argv


def test_agents_panel_is_the_right_column():
    argv = _agents_panel_argv()
    assert argv[-1].endswith('podarcis.tui.agents_panel')
    assert 'herdr' not in Path(argv[0]).name


def test_conversations_from_snapshot(tmp_path):
    folder = tmp_path / 'wiki'
    folder.mkdir()
    snap = {
        'workspaces': [{'workspace_id': 'w1', 'label': 'wiki'}],
        'tabs': [
            {'tab_id': 't1', 'workspace_id': 'w1', 'label': 'opencode', 'agent_status': 'working'},
        ],
        'panes': [
            {
                'pane_id': 'p1',
                'tab_id': 't1',
                'workspace_id': 'w1',
                'cwd': str(folder),
                'agent': 'opencode',
                'agent_status': 'blocked',
            }
        ],
    }
    ws = find_workspace(snap, folder)
    assert ws is not None and ws['workspace_id'] == 'w1'
    convs = conversations(snap, folder)
    assert len(convs) == 1
    assert convs[0].status == 'blocked'
    assert convs[0].emoji == '🔴'
    assert convs[0].pane_id == 'p1'
