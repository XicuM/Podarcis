'''PR3: structured lint/search JSON, overlay popups, key merge, lint-gated commit.'''

from __future__ import annotations

import inspect
import json
import os
import stat
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest

OKF = (
    '---\n'
    'title: Caffeine\n'
    'type: concept\n'
    'category: test\n'
    'rationale: test\n'
    '---\n'
)


def _checkout(root: Path, *, frontend: str = 'none') -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / 'AGENTS.md').write_text('# Podarcis\n', encoding='utf-8')
    pod = root / '.podarcis'
    pod.mkdir(exist_ok=True)
    (pod / 'config.yaml').write_text(f'frontend: {frontend}\nengines:\n  qmd: false\n', encoding='utf-8')
    return root


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for args in (
        ['init', '-b', 'master'],
        ['config', 'user.email', 't@t'],
        ['config', 'user.name', 't'],
    ):
        subprocess.run(['git', *args], cwd=path, capture_output=True, check=True)
    return path


def _commit_seed(repo: Path, rel: str, body: str) -> None:
    dest = repo / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(body, encoding='utf-8')
    subprocess.run(['git', 'add', '-A'], cwd=repo, capture_output=True, check=True)
    subprocess.run(['git', 'commit', '-m', 'seed'], cwd=repo, capture_output=True, check=True)


def test_search_json_schema(tmp_path, monkeypatch):
    checkout = _checkout(tmp_path / 'proj')
    page = checkout / 'wiki' / 'health' / 'caffeine.md'
    page.parent.mkdir(parents=True)
    page.write_text(OKF + 'Caffeine acts as a nonselective antagonist of adenosine receptors.\n', encoding='utf-8')
    monkeypatch.delenv('ENABLE_QMD', raising=False)

    from podarcis.tui.search import search
    result = search(checkout, 'adenosine', collection='wiki', method='keyword')
    assert result['query'] == 'adenosine'
    assert result['collection'] == 'wiki'
    assert result['method'] == 'keyword'
    assert 'warning' in result
    assert isinstance(result['hits'], list)
    assert result['hits'], result
    hit = result['hits'][0]
    assert set(hit) >= {'path', 'title', 'score', 'collection', 'snippet'}
    assert hit['path'].endswith('caffeine.md')
    assert hit['title'] == 'Caffeine'
    assert hit['collection'] == 'wiki'
    assert hit['score'] is None
    assert 'adenosine' in hit['snippet'].lower()


def test_wiki_search_cli_json(tmp_path, monkeypatch, capsys):
    checkout = _checkout(tmp_path / 'proj')
    page = checkout / 'wiki' / 'caffeine.md'
    page.parent.mkdir(parents=True)
    page.write_text(OKF + 'Caffeine adenosine\n', encoding='utf-8')
    monkeypatch.chdir(checkout)
    monkeypatch.delenv('PODARCIS_ROOT', raising=False)
    monkeypatch.delenv('ENABLE_QMD', raising=False)

    from podarcis import cli
    monkeypatch.setattr('sys.argv', ['podarcis', 'wiki', 'search', 'caffeine', '--json'])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    data = json.loads(capsys.readouterr().out)
    assert data['collection'] == 'wiki'
    assert data['query'] == 'caffeine'
    assert isinstance(data['hits'], list)
    assert data['hits']
    assert set(data['hits'][0]) >= {'path', 'title', 'score', 'collection', 'snippet'}


def test_lint_json_schema_and_cache(tmp_path, monkeypatch, capsys):
    checkout = _checkout(tmp_path / 'proj')
    page = checkout / 'wiki' / 'bad.md'
    page.parent.mkdir(parents=True)
    page.write_text(OKF + '[gone](../nope.md)\nClaim.[^1]\n\n[^1]: x\n', encoding='utf-8')
    monkeypatch.chdir(checkout)
    monkeypatch.delenv('PODARCIS_ROOT', raising=False)

    from podarcis.audit import lint
    payload = lint(checkout)
    assert payload['ok'] is False
    assert payload['root'] == str(checkout.resolve())
    assert isinstance(payload['files'], dict)
    page_key = next(k for k in payload['files'] if k.endswith('bad.md'))
    issues = payload['files'][page_key]
    codes = {i['code'] for i in issues}
    assert 'broken_link' in codes
    assert 'positional_footnote' in codes
    for issue in issues:
        assert set(issue) >= {'code', 'detail'}
    cache = checkout / 'tmp' / 'tui' / 'lint.json'
    assert cache.is_file()
    cached = json.loads(cache.read_text(encoding='utf-8'))
    assert cached['files'] == payload['files']

    from podarcis import cli
    monkeypatch.setattr('sys.argv', ['podarcis', 'lint', '--json', str(page)])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    data = json.loads(capsys.readouterr().out)
    assert data['ok'] is False
    assert any(
        iss['code'] == 'broken_link'
        for rec in data['files'].values()
        for iss in rec
    )


def test_check_links_script_json(tmp_path):
    from podarcis.audit import check_links_path, python_bin
    page = tmp_path / 'wiki' / 'n.md'
    page.parent.mkdir(parents=True)
    page.write_text(OKF + '[x](missing.md)\n', encoding='utf-8')
    proc = subprocess.run(
        [python_bin(), str(check_links_path()), '--json', str(page)],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 1
    data = json.loads(proc.stdout)
    assert data['ok'] is False
    assert any(i['code'] == 'broken_link' for rec in data['files'].values() for i in rec)


def test_overlay_modules_are_popup_commands():
    from podarcis.tui.keys import overlay_key_blocks
    blocks = overlay_key_blocks(plugin_linked=False, python_bin='python3')
    by_key = {b['key']: b for b in blocks}
    assert set(by_key) == {'prefix+/', 'prefix+shift+l', 'prefix+shift+s', 'prefix+shift+c'}
    assert 'prefix+c' not in by_key
    assert by_key['prefix+/']['type'] == 'popup'
    assert by_key['prefix+/']['command'] == 'python3 -m podarcis.tui.actions.search_overlay'
    assert by_key['prefix+shift+l']['type'] == 'popup'
    assert by_key['prefix+shift+l']['command'] == 'python3 -m podarcis.tui.actions.lint_overlay'
    assert by_key['prefix+shift+c']['type'] == 'popup'
    assert by_key['prefix+shift+c']['command'] == 'python3 -m podarcis.tui.actions.commit'
    assert by_key['prefix+shift+s']['type'] == 'popup'
    assert by_key['prefix+shift+s']['command'] == 'python3 -m podarcis.tui.actions.sync'
    linked = overlay_key_blocks(plugin_linked=True, python_bin='python3')
    sync = next(b for b in linked if b['key'] == 'prefix+shift+s')
    assert sync['type'] == 'plugin_action'
    assert sync['command'] == 'podarcis.wiki.sync'


def test_merge_overlay_keys_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    dest = tmp_path / '.config' / 'herdr' / 'sessions' / 'podarcis' / 'config.toml'
    dest.parent.mkdir(parents=True)
    dest.write_text('onboarding = false\n\n[keys]\nprefix = "ctrl+space"\n', encoding='utf-8')
    from podarcis.tui.keys import merge_overlay_keys, present_keys
    added = merge_overlay_keys(dest, python_bin='python3', plugin_linked=False)
    assert added == ['prefix+/', 'prefix+shift+l', 'prefix+shift+s', 'prefix+shift+c']
    text = dest.read_text(encoding='utf-8')
    assert text.count('key = "prefix+/"') == 1
    assert 'key = "prefix+c"' not in text
    assert merge_overlay_keys(dest, python_bin='python3', plugin_linked=False) == []
    assert dest.read_text(encoding='utf-8') == text
    assert present_keys(text) >= {'prefix+/', 'prefix+shift+l', 'prefix+shift+s', 'prefix+shift+c'}
    live = Path.home() / '.config' / 'herdr' / 'sessions' / 'podarcis' / 'config.toml'
    assert live == dest


def test_merge_skips_existing_key(tmp_path):
    dest = tmp_path / 'config.toml'
    dest.write_text(
        '[[keys.command]]\nkey = "prefix+/"\ntype = "popup"\ncommand = "custom"\n',
        encoding='utf-8',
    )
    from podarcis.tui.keys import merge_overlay_keys
    added = merge_overlay_keys(dest, python_bin='python3', plugin_linked=False)
    assert 'prefix+/' not in added
    assert dest.read_text(encoding='utf-8').count('key = "prefix+/"') == 1
    assert 'command = "custom"' in dest.read_text(encoding='utf-8')


def test_overlays_noop_outside_wiki_session(monkeypatch, tmp_path):
    monkeypatch.delenv('HERDR_SESSION', raising=False)
    monkeypatch.delenv('HERDR_SOCKET_PATH', raising=False)
    monkeypatch.chdir(tmp_path)
    from podarcis.tui.actions import commit, lint_overlay, search_overlay, sync
    assert search_overlay.main(['caffeine']) == 0
    assert lint_overlay.main([]) == 0
    assert commit.main([]) == 0
    assert sync.main([]) == 0


def test_audit_gate_uses_sys_executable(tmp_path, monkeypatch):
    captured: list[list[str]] = []
    real_run = subprocess.run

    def fake_run(cmd, *a, **kw):
        joined = ' '.join(str(c) for c in (cmd or []))
        if 'check_links.py' in joined:
            captured.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, stdout='Audit passed.', stderr='')
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr('podarcis.audit.subprocess.run', fake_run)
    from podarcis.audit import audit_gate, python_bin
    ok, msg = audit_gate(tmp_path)
    assert ok and msg == 'Audit passed.'
    assert captured
    assert captured[0][0] == python_bin() == sys.executable
    assert '.venv' not in captured[0][0]


def test_audit_refuses_dirty_wiki_with_broken_links(tmp_path):
    checkout = _checkout(tmp_path / 'proj')
    wiki = _git_repo(checkout / 'wiki')
    _commit_seed(wiki, 'ok.md', OKF + 'clean page\n')
    (wiki / 'broken.md').write_text(OKF + '[x](../nope.md)\n', encoding='utf-8')
    from podarcis.audit import audit_and_commit
    result = audit_and_commit(checkout, 'chore: should not land')
    assert result['ok'] is False
    assert result['committed'] == []
    porcelain = subprocess.run(
        ['git', 'status', '--porcelain'], cwd=wiki, capture_output=True, text=True, check=True,
    ).stdout
    assert 'broken.md' in porcelain
    log = subprocess.run(
        ['git', 'log', '--oneline'], cwd=wiki, capture_output=True, text=True, check=True,
    ).stdout
    assert 'should not land' not in log


def test_commit_action_never_calls_auto_commit():
    from podarcis.tui.actions import commit as commit_mod
    src = inspect.getsource(commit_mod.main)
    assert 'auto_commit' not in src
    assert 'push_repos' not in src
    assert 'audit_and_commit' in inspect.getsource(commit_mod)


def test_commit_overlay_uses_audit_and_commit(tmp_path, monkeypatch):
    checkout = _checkout(tmp_path / 'proj')
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('HERDR_SESSION', 'podarcis')
    monkeypatch.chdir(checkout)
    monkeypatch.delenv('PODARCIS_ROOT', raising=False)
    called: list = []
    pushes: list = []
    monkeypatch.setattr(
        'podarcis.tui.actions.commit.dirty_repos',
        lambda root: [checkout / 'wiki'],
    )
    monkeypatch.setattr(
        'podarcis.tui.actions.commit.audit_and_commit',
        lambda root, msg: called.append((Path(root), msg)) or {'ok': True, 'committed': ['wiki']},
    )
    monkeypatch.setattr('podarcis.repos.push_repos', lambda *a, **k: pushes.append(k) or {})
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr('builtins.input', lambda *a, **k: 'y')
    from podarcis.tui.actions.commit import main
    assert main([]) == 0
    assert called and called[0][0] == checkout.resolve()
    assert pushes == []


def test_push_repos_audit_true_does_not_use_unguarded_auto_commit(tmp_path, monkeypatch):
    checkout = _checkout(tmp_path / 'proj')
    called: list = []
    monkeypatch.setattr(
        'podarcis.audit.audit_and_commit',
        lambda root, msg: called.append(msg) or {'ok': False, 'message': 'blocked'},
    )
    from podarcis.repos import push_repos
    res = push_repos(checkout, auto_commit=True, audit=True, message='x')
    assert called == ['x']
    assert all(v.get('status') == 'error' for v in res.values())


def test_repo_commit_cli(tmp_path, monkeypatch, capsys):
    checkout = _checkout(tmp_path / 'proj')
    monkeypatch.chdir(checkout)
    monkeypatch.delenv('PODARCIS_ROOT', raising=False)
    called: list = []
    monkeypatch.setattr(
        'podarcis.audit.audit_and_commit',
        lambda root, msg: called.append(msg) or {'ok': True, 'committed': ['wiki']},
    )
    from podarcis.cli import cmd_repo_commit
    rc = cmd_repo_commit(Namespace(message='chore: test', root=None))
    assert rc == 0
    assert called == ['chore: test']
    assert 'wiki' in capsys.readouterr().out


def test_metadata_never_applies_layout():
    from podarcis.tui import metadata
    src = inspect.getsource(metadata.main) + inspect.getsource(metadata.report_repo_metadata)
    assert 'layout.apply' not in src
    assert 'apply_socket_layout' not in src
    assert 'report-metadata' in inspect.getsource(metadata.report_repo_metadata)


def test_metadata_reports_tokens(tmp_path, monkeypatch):
    checkout = _checkout(tmp_path / 'proj')
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('HERDR_SESSION', 'podarcis')
    monkeypatch.chdir(checkout)
    monkeypatch.delenv('PODARCIS_ROOT', raising=False)
    captured: list = []

    class _Session:
        def cli(self, *args):
            captured.append(args)
            if args[:2] == ('workspace', 'list'):
                return {'workspaces': [{'workspace_id': 'w1', 'label': 'podarcis'}]}
            return {}

    monkeypatch.setattr('podarcis.tui.metadata.HerdrSession', lambda *a, **k: _Session())
    monkeypatch.setattr('podarcis.tui.metadata._herdr_bin', lambda: '/bin/herdr')
    monkeypatch.setattr(
        'podarcis.tui.metadata.get_repo_status',
        lambda root: [{'repo': 'wiki', 'status': 'modified'}],
    )
    from podarcis.tui.metadata import main
    assert main() == 0
    assert any(a[:2] == ('workspace', 'report-metadata') for a in captured)
    meta = next(a for a in captured if a[:2] == ('workspace', 'report-metadata'))
    assert 'w1' in meta
    assert '--source' in meta and 'podarcis.wiki' in meta
    assert '--token' in meta and 'wiki=modified' in meta
    assert not any('layout' in str(a) for a in captured)


def test_lint_json_flag_not_eaten_by_remainder(tmp_path, monkeypatch):
    checkout = _checkout(tmp_path / 'proj')
    (checkout / 'wiki').mkdir()
    (checkout / 'wiki' / 'ok.md').write_text(OKF + 'fine\n', encoding='utf-8')
    monkeypatch.chdir(checkout)
    monkeypatch.delenv('PODARCIS_ROOT', raising=False)
    seen: list = []

    def fake_lint(args):
        seen.append(args)
        return 0

    from podarcis import cli
    monkeypatch.setattr(cli, 'cmd_lint', fake_lint)
    monkeypatch.setattr('sys.argv', ['podarcis', 'lint', '--json'])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert seen and seen[0].json is True


def test_dry_run_does_not_merge_keys(tmp_path, monkeypatch):
    checkout = _checkout(tmp_path / 'proj')
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    herdr = bindir / 'herdr'
    herdr.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
    herdr.chmod(herdr.stat().st_mode | stat.S_IXUSR)
    nvim = bindir / 'nvim'
    nvim.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
    nvim.chmod(nvim.stat().st_mode | stat.S_IXUSR)
    opencode = bindir / 'opencode'
    opencode.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
    opencode.chmod(opencode.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv('HERDR_BIN', str(herdr))
    monkeypatch.setenv('PODARCIS_EDITOR', str(nvim))
    monkeypatch.setenv('PATH', f'{bindir}{os.pathsep}{os.environ.get("PATH", "")}')
    monkeypatch.chdir(checkout)
    from podarcis.tui import launch as launch_mod
    monkeypatch.setattr(launch_mod, 'get_repo_status', lambda root=None: [
        {'repo': n, 'status': 'synced', 'branch': 'master', 'changes': 0,
         'ahead': 0, 'behind': 0, 'type': 'git', 'url': 'local'}
        for n in ('sources', 'wiki', 'workspace')
    ])
    from podarcis.tui.launch import cmd_wiki
    rc = cmd_wiki(Namespace(
        root=str(checkout), dry_run=True, sync=False, reset_layout=False,
        reset_config=False, path=None,
    ))
    assert rc == 0
    live = tmp_path / 'home' / '.config' / 'herdr' / 'sessions' / 'podarcis' / 'config.toml'
    assert not live.exists()


def test_in_wiki_session_socket_path():
    from podarcis.tui.session import in_wiki_session
    assert in_wiki_session(environ={'HERDR_SESSION': 'podarcis'}) is True
    assert in_wiki_session(environ={'HERDR_SESSION': 'vscode'}) is False
    assert in_wiki_session(environ={
        'HERDR_SOCKET_PATH': '/home/u/.config/herdr/sessions/podarcis/herdr.sock',
    }) is True
    assert in_wiki_session(environ={
        'HERDR_SOCKET_PATH': '/home/u/.config/herdr/sessions/vscode/herdr.sock',
    }) is False
