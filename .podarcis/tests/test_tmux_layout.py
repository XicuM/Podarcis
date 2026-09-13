'''tmux compositor: files | editor | herdr-agents.'''

from __future__ import annotations

from pathlib import Path

from podarcis.tui.tmux_layout import TMUX_SESSION, create_session, pane_target, resolve_tmux


def test_pane_targets():
    assert pane_target(0).startswith(TMUX_SESSION)
    assert pane_target(0).endswith('.0')
    assert pane_target(2).endswith('.2')


def test_create_session_sends_three_columns(tmp_path, monkeypatch):
    calls: list[list[str]] = []

    class Result:
        returncode = 0

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return Result()

    monkeypatch.setattr('podarcis.tui.tmux_layout.subprocess.run', fake_run)
    create_session(
        '/usr/bin/tmux',
        tmp_path,
        files_argv=['/usr/bin/lf', str(tmp_path)],
        edit_argv=['/usr/bin/nvim', 'wiki/_index.md'],
        herdr_argv=['/usr/bin/herdr', '--session', 'podarcis'],
        environ={'PROJECT_ROOT': str(tmp_path), 'PATH': '/usr/bin'},
    )
    joined = [' '.join(c) for c in calls]
    assert any('new-session' in j for j in joined)
    assert any('lf' in j for j in joined)
    assert any('nvim' in j for j in joined)
    assert any('herdr --session podarcis' in j for j in joined)
    assert any('split-window' in j for j in joined)


def test_resolve_tmux_honors_env(tmp_path, monkeypatch):
    fake = tmp_path / 'tmux'
    fake.write_text('#!/bin/sh\n', encoding='utf-8')
    fake.chmod(0o755)
    monkeypatch.setenv('PODARCIS_TMUX', str(fake))
    assert resolve_tmux() == str(fake.resolve())
