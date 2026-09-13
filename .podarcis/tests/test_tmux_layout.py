'''tmux compositor: files | editor | herdr-agents.'''

from __future__ import annotations

from podarcis.tui.tmux_layout import TMUX_SESSION, create_session, resolve_tmux, window_target


def test_window_target_has_no_pane_index():
    assert window_target() == f'{TMUX_SESSION}:wiki'
    assert '.0' not in window_target()


def test_create_session_uses_shell_commands_not_pane_zero(tmp_path, monkeypatch):
    calls: list[list[str]] = []

    class Result:
        returncode = 0
        stdout = '0 %1\n40 %2\n80 %3\n'
        stderr = ''

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return Result()

    monkeypatch.setattr('podarcis.tui.tmux_layout.subprocess.run', fake_run)
    create_session(
        '/usr/bin/tmux',
        tmp_path,
        files_argv=['/usr/bin/yazi', str(tmp_path)],
        edit_argv=['/usr/bin/nvim', 'wiki/_index.md'],
        herdr_argv=['/usr/bin/herdr', '--session', 'podarcis'],
        environ={'PROJECT_ROOT': str(tmp_path), 'PATH': '/usr/bin'},
    )
    joined = [' '.join(c) for c in calls]
    assert any('new-session' in j and 'yazi' in j for j in joined)
    assert any('split-window' in j and 'nvim' in j for j in joined)
    assert any('split-window' in j and 'herdr --session podarcis' in j for j in joined)
    assert not any('wiki.0' in j for j in joined)
    assert not any('send-keys' in j for j in joined)


def test_resolve_tmux_honors_env(tmp_path, monkeypatch):
    fake = tmp_path / 'tmux'
    fake.write_text('#!/bin/sh\n', encoding='utf-8')
    fake.chmod(0o755)
    monkeypatch.setenv('PODARCIS_TMUX', str(fake))
    assert resolve_tmux() == str(fake.resolve())
