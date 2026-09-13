'''Tests for `podarcis wiki` root discovery, dry-run, frontend dispatch, and server poll.'''

from __future__ import annotations

import os
import stat
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

from podarcis.tui.root import WikiRootError, find_wiki_root, find_wiki_root_or_none, is_wiki_root
from podarcis.tui.server import ensure_herdr_server
from podarcis.tui.deps import resolve_herdr


def _checkout(root: Path, *, frontend: str = 'none') -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / 'AGENTS.md').write_text('# Podarcis\n', encoding='utf-8')
    pod = root / '.podarcis'
    pod.mkdir(exist_ok=True)
    (pod / 'config.yaml').write_text(f'frontend: {frontend}\n', encoding='utf-8')
    return root


def _exe(path: Path, body: str = '#!/bin/sh\nexit 0\n') -> Path:
    path.write_text(body, encoding='utf-8')
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _synced(_root=None):
    return [
        {'repo': name, 'status': 'synced', 'branch': 'master', 'changes': 0,
         'ahead': 0, 'behind': 0, 'type': 'git', 'url': 'local'}
        for name in ('sources', 'wiki', 'workspace')
    ]


def _wiki_args(**kwargs):
    defaults = dict(
        root=None, dry_run=True, sync=False, reset_layout=False,
        reset_config=False, path=None,
    )
    defaults.update(kwargs)
    return Namespace(**defaults)


def test_find_wiki_root_explicit(tmp_path):
    checkout = _checkout(tmp_path / 'wiki')
    found = find_wiki_root(explicit=checkout)
    assert found == checkout.resolve()
    assert is_wiki_root(found)


def test_find_wiki_root_env(tmp_path, monkeypatch):
    checkout = _checkout(tmp_path / 'wiki')
    monkeypatch.setenv('PODARCIS_ROOT', str(checkout))
    assert find_wiki_root() == checkout.resolve()


def test_find_wiki_root_walk(tmp_path, monkeypatch):
    checkout = _checkout(tmp_path / 'wiki')
    nested = checkout / 'wiki' / 'topic'
    nested.mkdir(parents=True)
    monkeypatch.delenv('PODARCIS_ROOT', raising=False)
    assert find_wiki_root(cwd=nested) == checkout.resolve()


def test_find_wiki_root_invalid_explicit(tmp_path):
    with pytest.raises(WikiRootError):
        find_wiki_root(explicit=tmp_path / 'nope')


def test_find_wiki_root_ignores_package_root(tmp_path, monkeypatch):
    '''``podarcis.ROOT_DIR`` is site-packages under Nest; never a layout cwd.'''
    pkg = tmp_path / 'site-packages'
    _checkout(pkg)
    monkeypatch.setattr('podarcis.ROOT_DIR', pkg)
    monkeypatch.setattr('podarcis.cli.ROOT_DIR', pkg)
    outside = tmp_path / 'home'
    outside.mkdir()
    monkeypatch.chdir(outside)
    monkeypatch.delenv('PODARCIS_ROOT', raising=False)
    assert find_wiki_root_or_none() is None


def test_dry_run_resolution(tmp_path, monkeypatch, capsys):
    checkout = _checkout(tmp_path / 'wiki')
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    herdr = _exe(bindir / 'herdr')
    _exe(bindir / 'nvim')
    _exe(bindir / 'opencode')
    _exe(bindir / 'yazi')
    monkeypatch.setenv('HERDR_BIN', str(herdr))
    monkeypatch.setenv('PODARCIS_EDITOR', str(bindir / 'nvim'))
    monkeypatch.setenv('PATH', f'{bindir}{os.pathsep}{os.environ.get("PATH", "")}')
    monkeypatch.delenv('PODARCIS_ROOT', raising=False)
    monkeypatch.chdir(checkout)

    from podarcis.tui import launch as launch_mod
    monkeypatch.setattr(launch_mod, 'get_repo_status', _synced)

    from podarcis.tui.launch import cmd_wiki
    rc = cmd_wiki(_wiki_args(root=str(checkout)))
    assert rc == 0
    out = capsys.readouterr().out
    assert str(checkout) in out
    assert 'session' in out.lower()
    assert 'podarcis' in out
    assert 'files' in out
    assert 'edit' in out
    assert 'agent' in out
    assert 'yazi' in out
    assert 'nvim' in out
    assert 'opencode' in out


def test_missing_herdr_via_herdr_bin(tmp_path, monkeypatch, capsys):
    checkout = _checkout(tmp_path / 'wiki')
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    _exe(bindir / 'nvim')
    _exe(bindir / 'opencode')
    monkeypatch.setenv('HERDR_BIN', '/nonexistent/herdr')
    monkeypatch.setenv('PODARCIS_EDITOR', str(bindir / 'nvim'))
    monkeypatch.setenv('PATH', f'{bindir}{os.pathsep}{os.environ.get("PATH", "")}')
    monkeypatch.chdir(checkout)

    from podarcis.tui import launch as launch_mod
    monkeypatch.setattr(launch_mod, 'get_repo_status', _synced)

    assert resolve_herdr() is None
    from podarcis.tui.launch import cmd_wiki
    rc = cmd_wiki(_wiki_args(root=str(checkout)))
    assert rc == 1
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert 'herdr not found' in text
    assert 'HERDR_BIN=/nonexistent/herdr' in text
    assert 'https://herdr.dev/install.sh' in text


def test_dry_run_does_not_start_server(tmp_path, monkeypatch):
    checkout = _checkout(tmp_path / 'wiki')
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    herdr = _exe(bindir / 'herdr')
    _exe(bindir / 'nvim')
    _exe(bindir / 'opencode')
    monkeypatch.setenv('HERDR_BIN', str(herdr))
    monkeypatch.setenv('PODARCIS_EDITOR', str(bindir / 'nvim'))
    monkeypatch.setenv('PATH', f'{bindir}{os.pathsep}{os.environ.get("PATH", "")}')
    monkeypatch.chdir(checkout)

    from podarcis.tui import launch as launch_mod
    from podarcis.tui import server as server_mod
    monkeypatch.setattr(launch_mod, 'get_repo_status', _synced)

    popens: list = []
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: popens.append((a, k)) or None)
    monkeypatch.setattr(server_mod, 'ensure_herdr_server', lambda *a, **k: popens.append('server'))
    monkeypatch.setattr(launch_mod, 'ensure_herdr_server', lambda *a, **k: popens.append('server'))

    sockets: list = []
    monkeypatch.setattr('socket.socket', lambda *a, **k: sockets.append(a) or None)

    from podarcis.tui.launch import cmd_wiki
    rc = cmd_wiki(_wiki_args(root=str(checkout)))
    assert rc == 0
    assert popens == []
    assert sockets == []


def test_frontend_herdr_dispatches_to_cmd_wiki(tmp_path, monkeypatch):
    checkout = _checkout(tmp_path / 'wiki', frontend='herdr')
    pkg = tmp_path / 'site-packages'
    pkg.mkdir()
    from podarcis import cli
    monkeypatch.setattr(cli, 'ROOT_DIR', pkg)
    monkeypatch.chdir(checkout)
    monkeypatch.delenv('PODARCIS_ROOT', raising=False)

    called: list = []
    monkeypatch.setattr(cli, 'cmd_wiki', lambda args: called.append(args) or 0)

    rc = cli.cmd_frontend(Namespace(root=None))
    assert rc == 0
    assert len(called) == 1


def test_frontend_obsidian_outside_checkout_opens_gui(tmp_path, monkeypatch):
    engine = tmp_path / 'engine'
    engine.mkdir()
    (engine / '.podarcis').mkdir()
    (engine / '.podarcis' / 'config.yaml').write_text('frontend: obsidian\n', encoding='utf-8')
    outside = tmp_path / 'home'
    outside.mkdir()

    from podarcis import cli
    monkeypatch.setattr(cli, 'ROOT_DIR', engine)
    monkeypatch.chdir(outside)
    monkeypatch.delenv('PODARCIS_ROOT', raising=False)
    monkeypatch.setattr('podarcis.banner.display_project_banner', lambda root: None)

    opened: list = []

    class _Proc:
        pass

    def fake_popen(cmd, **kwargs):
        opened.append(cmd)
        return _Proc()

    monkeypatch.setattr(cli.subprocess, 'Popen', fake_popen)
    rc = cli.cmd_frontend(Namespace(root=None))
    assert rc == 0
    assert opened
    assert opened[0][0] == 'obsidian'


def test_config_frontend_herdr_requires_wiki_root(tmp_path, monkeypatch, capsys):
    from podarcis import cli
    monkeypatch.setattr(cli, 'ROOT_DIR', tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv('PODARCIS_ROOT', raising=False)
    rc = cli.cmd_config_frontend(Namespace(frontend_name='herdr', root=None))
    assert rc == 1
    assert 'not a Podarcis checkout' in capsys.readouterr().out


def test_config_frontend_herdr_writes_wiki_root(tmp_path, monkeypatch):
    checkout = _checkout(tmp_path / 'wiki')
    from podarcis import cli
    from podarcis.common import get_config_value
    monkeypatch.setattr(cli, 'ROOT_DIR', tmp_path / 'engine')
    monkeypatch.chdir(checkout)
    monkeypatch.delenv('PODARCIS_ROOT', raising=False)
    rc = cli.cmd_config_frontend(Namespace(frontend_name='herdr', root=None))
    assert rc == 0
    assert get_config_value(checkout, 'frontend') == 'herdr'
    engine_cfg = tmp_path / 'engine' / '.podarcis' / 'config.yaml'
    assert not engine_cfg.exists()


def test_frontends_map_has_no_herdr_sentinel():
    from podarcis.cli import FRONTEND_NAMES, FRONTENDS
    assert 'herdr' not in FRONTENDS
    assert 'herdr' in FRONTEND_NAMES
    assert FRONTENDS['none'] is None


def test_session_template_has_overlay_keys():
    from podarcis.tui.server import session_template
    text = session_template().read_text(encoding='utf-8')
    assert 'prefix = "ctrl+space"' in text
    assert 'sidebar_start_collapsed = false' in text
    assert 'allow_nested = false' in text
    assert 'onboarding = false' in text
    assert '[[keys.command]]' in text
    assert 'key = "prefix+/"' in text
    assert 'podarcis.tui.actions.search_overlay' in text
    assert 'key = "prefix+shift+l"' in text
    assert 'podarcis.tui.actions.lint_overlay' in text
    assert 'key = "prefix+shift+s"' in text
    assert 'key = "prefix+shift+c"' in text
    assert 'podarcis.tui.actions.commit' in text
    assert 'key = "prefix+c"' not in text


def test_ensure_herdr_server_polls_sock(tmp_path):
    sock = tmp_path / 'herdr.sock'
    cfg = tmp_path / 'config.toml'
    cfg.write_text('', encoding='utf-8')
    fake = _exe(tmp_path / 'herdr', (
        '#!/bin/sh\n'
        'while [ "$1" != "server" ] && [ -n "$1" ]; do shift; done\n'
        'if [ "$1" = "server" ]; then\n'
        '  sleep 0.15\n'
        f'  touch "{sock}"\n'
        '  exec sleep 30\n'
        'fi\n'
        'exit 0\n'
    ))
    proc = ensure_herdr_server(str(fake), sock=sock, config=cfg, timeout_s=2.0, interval_s=0.05)
    try:
        assert sock.exists()
        assert proc is not None
        assert proc.poll() is None
    finally:
        if proc is not None:
            proc.kill()
            proc.wait(timeout=2)


def test_ensure_herdr_server_timeout_kills_group(tmp_path):
    sock = tmp_path / 'herdr.sock'
    cfg = tmp_path / 'config.toml'
    cfg.write_text('', encoding='utf-8')
    fake = _exe(tmp_path / 'herdr', (
        '#!/bin/sh\n'
        'exec sleep 30\n'
    ))
    with pytest.raises(RuntimeError, match='did not create sock'):
        ensure_herdr_server(str(fake), sock=sock, config=cfg, timeout_s=0.4, interval_s=0.1)
    assert not sock.exists()


def test_ensure_herdr_server_skips_when_sock_exists(tmp_path, monkeypatch):
    sock = tmp_path / 'herdr.sock'
    sock.write_text('', encoding='utf-8')
    cfg = tmp_path / 'config.toml'
    cfg.write_text('', encoding='utf-8')
    called: list = []
    import podarcis.tui.server as server_mod
    monkeypatch.setattr(server_mod.subprocess, 'Popen', lambda *a, **k: called.append(a) or None)
    proc = ensure_herdr_server('/nonexistent/herdr', sock=sock, config=cfg)
    assert proc is None
    assert called == []


def test_parent_root_survives_wiki_subcommand(tmp_path, monkeypatch, capsys):
    '''``podarcis --root PATH wiki`` must not drop PATH to the wiki subparser default.'''
    checkout = _checkout(tmp_path / 'wiki')
    outside = tmp_path / 'home'
    outside.mkdir()
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    herdr = _exe(bindir / 'herdr')
    _exe(bindir / 'nvim')
    _exe(bindir / 'opencode')
    monkeypatch.setenv('HERDR_BIN', str(herdr))
    monkeypatch.setenv('PODARCIS_EDITOR', str(bindir / 'nvim'))
    monkeypatch.setenv('PATH', f'{bindir}{os.pathsep}{os.environ.get("PATH", "")}')
    monkeypatch.delenv('PODARCIS_ROOT', raising=False)
    monkeypatch.chdir(outside)

    from podarcis.tui import launch as launch_mod
    monkeypatch.setattr(launch_mod, 'get_repo_status', _synced)

    from podarcis import cli
    monkeypatch.setattr('sys.argv', ['podarcis', '--root', str(checkout), 'wiki', '--dry-run'])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert str(checkout) in out
    assert 'session' in out.lower()


def test_apply_socket_layout_passes_tab_id(tmp_path):
    from podarcis.herdr.layout import apply_socket_layout, layout_tree

    captured: list = []

    def rpc(method, params):
        captured.append((method, params))
        return {'type': 'layout_apply', 'layout': {'root': layout_tree(tmp_path)['root']}}

    apply_socket_layout(rpc, 'w1', tmp_path, tab_id='w1:t1')
    assert captured[0][0] == 'layout.apply'
    assert captured[0][1]['tab_id'] == 'w1:t1'
    assert captured[0][1]['workspace_id'] == 'w1'


def test_apply_socket_layout_falls_back_to_tab_list(tmp_path):
    from podarcis.herdr.layout import apply_socket_layout, layout_tree

    captured: list = []

    def rpc(method, params):
        captured.append((method, params))
        return {'type': 'layout_apply', 'layout': {'root': layout_tree(tmp_path)['root']}}

    def cli(*args):
        if args[:2] == ('workspace', 'get'):
            return {'workspace': {'workspace_id': 'w1'}}
        if args[:2] == ('tab', 'list'):
            return {'tabs': [{'tab_id': 'w1:t9'}]}
        raise AssertionError(args)

    apply_socket_layout(rpc, 'w1', tmp_path, cli=cli)
    assert captured[0][1]['tab_id'] == 'w1:t9'


def test_apply_socket_layout_requires_tab_id(tmp_path):
    from podarcis.herdr.layout import apply_socket_layout

    with pytest.raises(RuntimeError, match='no tab_id'):
        apply_socket_layout(lambda m, p: {}, 'w1', tmp_path)


def test_is_shell_foreground_requires_positive_signal():
    from podarcis.herdr.layout import is_shell_foreground

    assert is_shell_foreground({'process_info': {'foreground_processes': []}}) is False
    assert is_shell_foreground({
        'process_info': {'foreground_processes': [], 'shell_pid': 42},
    }) is True
    assert is_shell_foreground({
        'foreground_processes': [{'name': 'bash', 'pid': 1}],
    }) is True
    assert is_shell_foreground({
        'process_info': {
            'shell_pid': 42,
            'foreground_processes': [{'name': 'nvim', 'pid': 99}],
        },
    }) is False


def test_env_overrides_config_file_manager_and_harness(tmp_path, monkeypatch):
    checkout = _checkout(tmp_path / 'wiki')
    (checkout / '.podarcis' / 'config.yaml').write_text(
        'frontend: none\ntui:\n  file_manager: lf\n  harness: claude\n',
        encoding='utf-8',
    )
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    _exe(bindir / 'yazi')
    _exe(bindir / 'lf')
    _exe(bindir / 'opencode')
    _exe(bindir / 'claude')
    monkeypatch.setenv('PATH', f'{bindir}{os.pathsep}{os.environ.get("PATH", "")}')
    monkeypatch.setenv('PODARCIS_FILE_MANAGER', 'yazi')
    monkeypatch.setenv('PODARCIS_HARNESS', 'opencode')

    from podarcis.tui.deps import resolve_file_manager, resolve_harness
    fm = resolve_file_manager(checkout)
    assert fm is not None and fm[0] == 'yazi'
    assert resolve_harness(checkout) == 'opencode'
