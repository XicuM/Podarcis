'''Tests for current-page context, edit targeting, persona argv, session guards, flavors.'''

from __future__ import annotations

import json
import os
import stat
from argparse import Namespace
from pathlib import Path

import pytest

from podarcis.herdr.layout import (
    editor_run_argv,
    files_run_argv,
    foreground_occupant,
    is_named_shell_foreground,
)
from podarcis.tui.actions.edit import helix_escape, open_in_edit_pane, vim_escape
from podarcis.tui.actions.spawn_persona import persona_prompt_text, persona_start_argv, spawn_persona
from podarcis.tui.context import write_current
from podarcis.tui.session import in_wiki_session


def _exe(path: Path, body: str = '#!/bin/sh\nexit 0\n') -> Path:
    path.write_text(body, encoding='utf-8')
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _checkout(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / 'AGENTS.md').write_text('# Podarcis\n', encoding='utf-8')
    pod = root / '.podarcis'
    pod.mkdir(exist_ok=True)
    (pod / 'config.yaml').write_text('frontend: none\n', encoding='utf-8')
    return root


def _page(root: Path) -> Path:
    page = root / 'wiki' / 'health' / 'caffeine.md'
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        '---\n'
        'type: concept\n'
        'title: Caffeine\n'
        'category: health/nutrition\n'
        'status: draft\n'
        '---\n'
        '\n'
        'Caffeine acts as a nonselective antagonist of adenosine receptors.\n'
        'Second line of body.\n',
        encoding='utf-8',
    )
    return page


class FakeSession:
    def __init__(self, occupant: str | None = 'bash', *, shell_pid: int | None = None):
        self.calls: list[tuple] = []
        self.labels = {'files': 'w1:p1', 'edit': 'w1:p2', 'agent': 'w1:p3'}
        self.occupant = occupant
        self.shell_pid = shell_pid
        self.next_pane = 'w1:p4'

    def cli(self, *args: str) -> dict:
        self.calls.append(args)
        if args[:2] == ('pane', 'list'):
            return {
                'panes': [
                    {'label': label, 'pane_id': pane_id}
                    for label, pane_id in self.labels.items()
                ],
            }
        if args[:2] == ('pane', 'process-info'):
            if self.occupant is None and not self.shell_pid:
                return {'foreground_processes': []}
            if self.occupant is None:
                return {'foreground_processes': [], 'shell_pid': self.shell_pid}
            return {'foreground_processes': [{'name': self.occupant, 'pid': 9}]}
        if args[:2] == ('pane', 'split'):
            pane_id = self.next_pane
            return {'pane': {'pane_id': pane_id}}
        if args[:2] == ('pane', 'rename'):
            self.labels[args[3]] = args[2]
            return {}
        return {}


def test_write_current_schema(tmp_path, monkeypatch):
    checkout = _checkout(tmp_path / 'wiki')
    page = _page(checkout)
    monkeypatch.chdir(checkout)
    rec = write_current(checkout, page)
    assert rec['path'] == 'wiki/health/caffeine.md'
    assert rec['repo'] == 'wiki'
    assert rec['title'] == 'Caffeine'
    assert rec['type'] == 'concept'
    assert rec['category'] == 'health/nutrition'
    assert rec['status'] == 'draft'
    assert rec['updated_at'].endswith('Z')
    blob = json.loads((checkout / 'tmp' / 'tui' / 'current.json').read_text(encoding='utf-8'))
    assert blob == rec
    snippet = (checkout / 'tmp' / 'tui' / 'current.md').read_text(encoding='utf-8')
    assert 'wiki/health/caffeine.md' in snippet
    assert 'Caffeine' in snippet
    assert 'concept' in snippet
    assert 'citation chain: workspace → wiki → sources; do not bypass.' in snippet
    assert 'nonselective antagonist' in snippet
    assert '---' not in snippet.split('Excerpt', 1)[-1]


def test_write_current_does_not_touch_live_config(tmp_path, monkeypatch):
    checkout = _checkout(tmp_path / 'wiki')
    page = _page(checkout)
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(checkout)
    write_current(checkout, page)
    assert not (home / '.podarcis').exists()
    assert (checkout / 'tmp' / 'tui' / 'current.json').is_file()


def test_write_current_outside_repos_is_engine(tmp_path, monkeypatch):
    checkout = _checkout(tmp_path / 'wiki')
    readme = checkout / 'README.md'
    readme.write_text('# Engine\n', encoding='utf-8')
    monkeypatch.chdir(checkout)
    rec = write_current(checkout, readme)
    assert rec['repo'] == 'engine'
    assert rec['path'] == 'README.md'


@pytest.mark.parametrize(
    ('occupant', 'expect_run', 'expect_keys', 'expect_text', 'expect_err'),
    [
        ('bash', True, False, False, None),
        ('zsh', True, False, False, None),
        ('nvim', False, True, ':e ', None),
        ('helix', False, True, ':open ', None),
        ('hx', False, True, ':open ', None),
        ('python', False, False, False, 'busy'),
        (None, False, False, False, 'busy'),
    ],
)
def test_edit_targeting_branches(tmp_path, occupant, expect_run, expect_keys, expect_text, expect_err):
    session = FakeSession(occupant=occupant)
    flavor = tmp_path / 'flavor'
    (flavor / 'flavors' / 'nvim').mkdir(parents=True)
    (flavor / 'flavors' / 'nvim' / 'wiki.lua').write_text('-- hook\n', encoding='utf-8')
    path = tmp_path / 'page.md'
    path.write_text('# hi\n', encoding='utf-8')
    editor = [str(tmp_path / 'nvim')]
    err = open_in_edit_pane(session, path, editor=editor, flavor_dir=flavor)
    kinds = [c[1] for c in session.calls if c and c[0] == 'pane']
    if expect_err:
        assert err is not None and expect_err in err
        assert 'run' not in kinds
        assert 'send-keys' not in kinds
        return
    assert err is None
    if expect_run:
        assert 'run' in kinds
        run = [c for c in session.calls if c[:2] == ('pane', 'run')][0]
        assert str(path) in run[3]
    else:
        assert 'run' not in kinds
    if expect_keys:
        keys = [c for c in session.calls if c[:2] == ('pane', 'send-keys')]
        assert keys[0][3] == 'esc'
        assert keys[-1][3] == 'enter'
        texts = [c[3] for c in session.calls if c[:2] == ('pane', 'send-text')]
        assert any(expect_text in t for t in texts)


def test_edit_nvim_always_sends_esc_first():
    session = FakeSession(occupant='nvim')
    path = Path('/tmp/wiki/page.md')
    err = open_in_edit_pane(
        session, path, editor=['nvim'], flavor_dir=Path('/nonexistent'),
    )
    assert err is None
    send = [c for c in session.calls if c[0] == 'pane' and c[1] in ('send-keys', 'send-text')]
    assert send[0][:4] == ('pane', 'send-keys', 'w1:p2', 'esc')


def test_edit_missing_pane():
    session = FakeSession(occupant='bash')
    session.labels.pop('edit')
    err = open_in_edit_pane(
        session, Path('/x.md'), editor=['nvim'], flavor_dir=Path('/n'),
    )
    assert err is not None and 'edit pane not found' in err


def test_empty_process_list_is_not_idle_shell():
    assert foreground_occupant({'foreground_processes': []}) is None
    assert foreground_occupant({'process_info': {'foreground_processes': []}}) is None
    assert foreground_occupant({
        'process_info': {'foreground_processes': [], 'shell_pid': 7},
    }) is None
    assert is_named_shell_foreground({
        'process_info': {'foreground_processes': [], 'shell_pid': 7},
    }) is False
    assert is_named_shell_foreground({
        'foreground_processes': [{'name': 'bash', 'pid': 1}],
    }) is True


def test_edit_empty_process_list_with_shell_pid_is_busy(tmp_path):
    session = FakeSession(occupant=None, shell_pid=1)
    err = open_in_edit_pane(
        session, tmp_path / 'page.md', editor=['nvim'], flavor_dir=tmp_path,
    )
    kinds = [c[1] for c in session.calls if c and c[0] == 'pane']
    assert err is not None and 'busy' in err
    assert 'run' not in kinds
    assert 'send-keys' not in kinds


@pytest.mark.parametrize(
    ('name', 'kind', 'extra'),
    [
        ('researcher', 'opencode', []),
        ('synthesizer', 'claude', ['--', '--agent', 'synthesizer']),
        ('auditor', 'grok', []),
        ('protocol-architect', 'opencode', []),
    ],
)
def test_persona_start_argv_table(name, kind, extra):
    argv = persona_start_argv(name, kind, 'w1:p9')
    assert argv[:6] == ['agent', 'start', name, '--kind', kind, '--pane']
    assert argv[6] == 'w1:p9'
    assert argv[7:] == extra


@pytest.mark.parametrize(
    ('kind', 'snippet', 'expect'),
    [
        ('opencode', 'hello', '@researcher\n\nhello'),
        ('opencode', '', '@researcher'),
        ('claude', 'hello', 'hello'),
        ('claude', '', ''),
        ('grok', 'hello', 'hello'),
    ],
)
def test_persona_prompt_text_table(kind, snippet, expect):
    assert persona_prompt_text('researcher', kind, snippet) == expect


def test_spawn_persona_opencode_prompt_includes_at_and_snippet(tmp_path):
    session = FakeSession(occupant='bash', shell_pid=1)
    session.occupant = 'bash'
    root = _checkout(tmp_path / 'wiki')
    (root / 'tmp' / 'tui').mkdir(parents=True)
    (root / 'tmp' / 'tui' / 'current.md').write_text('page snippet\n', encoding='utf-8')
    spawn_persona(session, 'researcher', kind='opencode', wiki_root=root)
    starts = [c for c in session.calls if c[:2] == ('agent', 'start')]
    assert starts and starts[0][4] == 'opencode'
    assert '--agent' not in starts[0]
    prompts = [c for c in session.calls if c[:2] == ('agent', 'prompt')]
    assert prompts
    assert prompts[0][2] == 'researcher'
    assert prompts[0][3].startswith('@researcher')
    assert 'page snippet' in prompts[0][3]


def test_spawn_persona_claude_uses_agent_flag(tmp_path):
    session = FakeSession(occupant='bash', shell_pid=1)
    root = _checkout(tmp_path / 'wiki')
    spawn_persona(session, 'auditor', kind='claude', wiki_root=root, snippet='ctx')
    starts = [c for c in session.calls if c[:2] == ('agent', 'start')]
    assert starts[0][-4:] == ('--pane', session.next_pane, '--', '--agent') or starts[0][-2:] == ('--agent', 'auditor')
    assert starts[0][-2:] == ('--agent', 'auditor')
    prompts = [c for c in session.calls if c[:2] == ('agent', 'prompt')]
    assert prompts[0][3] == 'ctx'
    assert '@auditor' not in prompts[0][3]


def test_in_wiki_session_guards():
    assert in_wiki_session({'HERDR_SESSION': 'podarcis'}) is True
    assert in_wiki_session({
        'HERDR_SOCKET_PATH': '/home/u/.config/herdr/sessions/podarcis/herdr.sock',
    }) is True
    assert in_wiki_session({'HERDR_SESSION': 'vscode'}) is False
    assert in_wiki_session({
        'HERDR_SOCKET_PATH': '/home/u/.config/herdr/sessions/vscode/herdr.sock',
    }) is False
    assert in_wiki_session({}) is False


def test_plugin_spawn_persona_noops_outside_wiki_session(monkeypatch):
    monkeypatch.delenv('HERDR_SESSION', raising=False)
    monkeypatch.delenv('HERDR_SOCKET_PATH', raising=False)
    from podarcis.tui.actions import spawn_persona as sp
    assert sp.main(['researcher']) == 0


def test_plugin_spawn_persona_usage_without_name(monkeypatch, capsys):
    monkeypatch.setenv('HERDR_SESSION', 'podarcis')
    monkeypatch.delenv('PODARCIS_PERSONA', raising=False)
    from podarcis.tui.actions import spawn_persona as sp
    assert sp.main([]) == 1
    err = capsys.readouterr()
    text = err.err + err.out
    assert 'usage:' in text
    assert 'researcher' in text
    assert 'synthesizer' in text
    assert 'protocol-architect' in text
    assert 'auditor' in text


def test_plugin_edit_noops_outside_wiki_session(monkeypatch):
    monkeypatch.delenv('HERDR_SESSION', raising=False)
    monkeypatch.delenv('HERDR_SOCKET_PATH', raising=False)
    from podarcis.tui.actions import edit as edit_mod
    assert edit_mod.main(['/tmp/x.md']) == 0


def test_plugin_spawn_persona_runs_in_wiki_session(tmp_path, monkeypatch, capsys):
    checkout = _checkout(tmp_path / 'wiki')
    monkeypatch.setenv('HERDR_SESSION', 'podarcis')
    monkeypatch.setenv('PROJECT_ROOT', str(checkout))
    monkeypatch.chdir(checkout)
    from podarcis.tui.actions import spawn_persona as sp
    rc = sp.main(['not-a-persona'])
    assert rc == 1
    assert 'unknown persona' in capsys.readouterr().out


def test_flavor_yazi_env_and_nvim_luafile(tmp_path):
    flavor = tmp_path / 'herdr'
    yazi = flavor / 'flavors' / 'yazi'
    yazi.mkdir(parents=True)
    (yazi / 'yazi.toml').write_text('[mgr]\n', encoding='utf-8')
    nvim = flavor / 'flavors' / 'nvim'
    nvim.mkdir(parents=True)
    lua = nvim / 'wiki.lua'
    lua.write_text('-- hook\n', encoding='utf-8')
    wiki = tmp_path / 'checkout'
    yazi_bin = tmp_path / 'bin' / 'yazi'
    nvim_bin = tmp_path / 'bin' / 'nvim'
    yazi_bin.parent.mkdir()
    _exe(yazi_bin)
    _exe(nvim_bin)

    files = files_run_argv([str(yazi_bin)], 'yazi', wiki, flavor)
    assert files is not None
    assert files[0] == 'env'
    assert files[1] == f'YAZI_CONFIG_HOME={yazi}'
    assert 'LF_CONFIG_HOME' not in ' '.join(files)
    assert str(wiki) == files[-1]

    lf = files_run_argv([str(tmp_path / 'bin' / 'lf')], 'lf', wiki, flavor)
    assert lf is not None
    assert all(not p.startswith('LF_CONFIG_HOME=') and p != 'LF_CONFIG_HOME' for p in lf)
    assert lf[0].endswith('lf')

    nvim_argv = editor_run_argv([str(nvim_bin)], flavor, open_path=wiki / 'page.md')
    assert '-c' in nvim_argv
    assert any(str(lua) in part for part in nvim_argv)
    assert nvim_argv[-1].endswith('page.md')

    helix_argv = editor_run_argv(['helix'], flavor)
    assert '--config' not in helix_argv


def test_flavor_helix_config_only_when_present(tmp_path):
    flavor = tmp_path / 'herdr'
    hx = flavor / 'flavors' / 'helix'
    hx.mkdir(parents=True)
    cfg = hx / 'config.toml'
    cfg.write_text('# helix\n', encoding='utf-8')
    argv = editor_run_argv(['helix'], flavor)
    assert argv[:3] == ['helix', '--config', str(cfg)]


def test_dry_run_layout_shows_flavor_argv(tmp_path, monkeypatch, capsys):
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
    monkeypatch.chdir(checkout)

    from podarcis.tui import launch as launch_mod
    monkeypatch.setattr(launch_mod, 'get_repo_status', lambda _root=None: [
        {'repo': n, 'status': 'synced', 'branch': 'master', 'changes': 0,
         'ahead': 0, 'behind': 0, 'type': 'git', 'url': 'local'}
        for n in ('sources', 'wiki', 'workspace')
    ])
    from podarcis.tui.launch import cmd_wiki
    rc = cmd_wiki(Namespace(
        root=str(checkout), dry_run=True, sync=False, reset_layout=False,
        reset_config=False, path=None, wiki_rest=[],
    ))
    assert rc == 0
    out = capsys.readouterr().out
    assert 'YAZI_CONFIG_HOME=' in out
    assert 'luafile' in out
    assert 'wiki.lua' in out
    assert 'LF_CONFIG_HOME' not in out
    assert 'plugin.link' not in out.lower() or 'would' in out.lower()


def test_write_linked_plugin_rewrites_python(tmp_path, monkeypatch):
    from podarcis.tui.server import package_herdr_dir, write_linked_plugin
    dest = tmp_path / 'plugin'
    python = str(tmp_path / 'venv' / 'bin' / 'python')
    out = write_linked_plugin(package_herdr_dir(), python=python, dest=dest)
    text = (out / 'herdr-plugin.toml').read_text(encoding='utf-8')
    assert python in text
    assert '"python3"' not in text
    assert 'podarcis.tui.actions.spawn_persona' in text
    assert 'id = "podarcis.wiki"' in text


def test_yazi_flavor_files_cover_design_rules():
    from podarcis.tui.server import package_herdr_dir
    flavor = package_herdr_dir() / 'flavors' / 'yazi'
    yazi = (flavor / 'yazi.toml').read_text(encoding='utf-8')
    keymap = (flavor / 'keymap.toml').read_text(encoding='utf-8')
    init = (flavor / 'init.lua').read_text(encoding='utf-8')
    theme = (flavor / 'theme.toml').read_text(encoding='utf-8')
    smart = (flavor / 'plugins' / 'smart-index.yazi' / 'main.lua').read_text(encoding='utf-8')
    assert 'podarcis wiki edit --' in yazi
    assert 'linemode = "lint"' in yazi
    assert 'show_hidden = false' in yazi
    assert 'plugin cd-root -- wiki' in keymap
    assert 'plugin cd-root -- sources' in keymap
    assert 'plugin cd-root -- workspace' in keymap
    assert 'plugin cd-root -- tmp' in keymap
    cd_root = (flavor / 'plugins' / 'cd-root.yazi' / 'main.lua').read_text(encoding='utf-8')
    assert 'PROJECT_ROOT' in cd_root
    assert 'root .. "/" .. dest' in cd_root
    for name in ('tmp', '.git', '.venv', '__pycache__', 'node_modules', '.obsidian', '.claude', '.opencode'):
        assert name in init
    assert 'lint.json' in init
    assert 'path:match("([^/]+)$")' not in init
    assert '_index.md' in theme
    assert 'index.md' in theme
    assert '_index.md' in smart
    assert 'podarcis wiki edit --' in smart
    assert not (package_herdr_dir() / 'flavors' / 'lf').exists()


def test_nvim_flavor_is_luafile_only():
    from podarcis.tui.server import package_herdr_dir
    lua = (package_herdr_dir() / 'flavors' / 'nvim' / 'wiki.lua').read_text(encoding='utf-8')
    assert 'BufEnter' in lua
    assert 'wiki' in lua and 'context' in lua
    assert 'XDG_CONFIG_HOME=' not in lua
    assert 'XDG_CONFIG_HOME =' not in lua
    assert 'jobstart' in lua


def test_vim_escape_spaces():
    assert '\\ ' in vim_escape('/tmp/my file.md')


def test_helix_escape_quotes_spaces():
    assert helix_escape('/tmp/my file.md') == '"/tmp/my file.md"'


def test_helix_open_quotes_spaces(tmp_path):
    session = FakeSession(occupant='helix')
    path = tmp_path / 'my page.md'
    path.write_text('# hi\n', encoding='utf-8')
    err = open_in_edit_pane(session, path, editor=['helix'], flavor_dir=tmp_path)
    assert err is None
    texts = [c[3] for c in session.calls if c[:2] == ('pane', 'send-text')]
    assert any(':open "' in t and 'my page.md' in t for t in texts)


def test_dispatch_wiki_edit_and_persona(tmp_path, monkeypatch, capsys):
    checkout = _checkout(tmp_path / 'wiki')
    page = _page(checkout)
    monkeypatch.chdir(checkout)
    monkeypatch.delenv('PODARCIS_ROOT', raising=False)

    from podarcis.tui.launch import dispatch_wiki
    rc = dispatch_wiki(Namespace(
        root=str(checkout), wiki_rest=['context', '--', str(page)], path=None, name=None,
    ))
    assert rc == 0
    rec = json.loads((checkout / 'tmp' / 'tui' / 'current.json').read_text(encoding='utf-8'))
    assert rec['title'] == 'Caffeine'

    rc = dispatch_wiki(Namespace(
        root=str(checkout), wiki_rest=['persona'], path=None, name=None,
    ))
    assert rc == 1
    assert 'persona requires a NAME' in capsys.readouterr().out

    args = Namespace(
        root=str(checkout), wiki_rest=['--', 'wiki/health/caffeine.md'],
        path=None, name=None, dry_run=True, sync=False,
        reset_layout=False, reset_config=False,
    )
    from podarcis.tui import launch as launch_mod
    monkeypatch.setattr(launch_mod, 'get_repo_status', lambda _root=None: [
        {'repo': n, 'status': 'synced', 'branch': 'master', 'changes': 0,
         'ahead': 0, 'behind': 0, 'type': 'git', 'url': 'local'}
        for n in ('sources', 'wiki', 'workspace')
    ])
    bindir = tmp_path / 'bin'
    if not bindir.exists():
        bindir.mkdir()
        _exe(bindir / 'herdr')
        _exe(bindir / 'nvim')
        _exe(bindir / 'opencode')
    monkeypatch.setenv('HERDR_BIN', str(bindir / 'herdr'))
    monkeypatch.setenv('PODARCIS_EDITOR', str(bindir / 'nvim'))
    monkeypatch.setenv('PATH', f'{bindir}{os.pathsep}{os.environ.get("PATH", "")}')
    rc = dispatch_wiki(args)
    assert args.path == 'wiki/health/caffeine.md'
    assert args.path != '--'
    assert rc == 0


def test_stale_pr1_herdr_still_uses_package_flavors(tmp_path, monkeypatch, capsys):
    checkout = _checkout(tmp_path / 'wiki')
    stale = checkout / '.podarcis' / 'herdr'
    stale.mkdir()
    (stale / 'session.toml').write_text('onboarding = false\n', encoding='utf-8')
    (stale / 'layout.py').write_text('# pr1 layout stub\n', encoding='utf-8')
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    herdr = _exe(bindir / 'herdr')
    _exe(bindir / 'nvim')
    _exe(bindir / 'opencode')
    _exe(bindir / 'yazi')
    monkeypatch.setenv('HERDR_BIN', str(herdr))
    monkeypatch.setenv('PODARCIS_EDITOR', str(bindir / 'nvim'))
    monkeypatch.setenv('PATH', f'{bindir}{os.pathsep}{os.environ.get("PATH", "")}')
    monkeypatch.chdir(checkout)

    from podarcis.tui import launch as launch_mod
    monkeypatch.setattr(launch_mod, 'get_repo_status', lambda _root=None: [
        {'repo': n, 'status': 'synced', 'branch': 'master', 'changes': 0,
         'ahead': 0, 'behind': 0, 'type': 'git', 'url': 'local'}
        for n in ('sources', 'wiki', 'workspace')
    ])
    from podarcis.tui.launch import cmd_wiki
    from podarcis.tui.server import package_herdr_dir, resolved_herdr_dir, write_linked_plugin
    assert resolved_herdr_dir(checkout) == package_herdr_dir()
    dest = tmp_path / 'plugin-link'
    out = write_linked_plugin(stale, python=str(tmp_path / 'venv' / 'bin' / 'python'), dest=dest)
    assert (out / 'herdr-plugin.toml').is_file()
    assert 'podarcis.wiki' in (out / 'herdr-plugin.toml').read_text(encoding='utf-8')

    rc = cmd_wiki(Namespace(
        root=str(checkout), dry_run=True, sync=False, reset_layout=False,
        reset_config=False, path=None, wiki_rest=[],
    ))
    assert rc == 0
    printed = capsys.readouterr().out
    assert 'YAZI_CONFIG_HOME=' in printed
    assert 'luafile' in printed
    assert 'wiki.lua' in printed


def test_ensure_checkout_herdr_merges_missing_flavors(tmp_path):
    from podarcis.tui.server import ensure_checkout_herdr, herdr_dir_has_flavors
    checkout = _checkout(tmp_path / 'wiki')
    stale = checkout / '.podarcis' / 'herdr'
    stale.mkdir()
    (stale / 'session.toml').write_text('onboarding = false\n', encoding='utf-8')
    (stale / 'layout.py').write_text('# pr1\n', encoding='utf-8')
    dest = ensure_checkout_herdr(checkout)
    assert dest == stale
    assert herdr_dir_has_flavors(dest)
    assert (dest / 'herdr-plugin.toml').is_file()
    assert (dest / 'session.toml').read_text(encoding='utf-8') == 'onboarding = false\n'
