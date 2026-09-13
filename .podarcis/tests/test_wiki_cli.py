'''``podarcis wiki`` and the JSON contracts the Rust front-end depends on.

The front-end reads `podarcis lint --json`, `podarcis wiki search --json` and
`podarcis repo status --json`. If a key here changes, the front-end breaks
silently, so their shapes are pinned.
'''

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from podarcis import audit, wiki
from podarcis.root import WikiRootError, find_wiki_root, is_wiki_root


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    (tmp_path / '.podarcis').mkdir()
    (tmp_path / 'AGENTS.md').write_text('# agents\n', encoding='utf-8')
    (tmp_path / '.podarcis' / 'config.yaml').write_text('engines:\n  qmd: false\n', encoding='utf-8')
    page = tmp_path / 'wiki' / 'health' / 'caffeine.md'
    page.parent.mkdir(parents=True)
    page.write_text(
        '---\ntitle: Caffeine\ntype: concept\ncategory: health\nrationale: r\n---\n'
        '# Caffeine\n\nBlocks adenosine[^lin_2023].\n\n[^lin_2023]: Lin et al.\n',
        encoding='utf-8',
    )
    return tmp_path


# ── argv normalisation ─────────────────────────────────────────────────────

@pytest.mark.parametrize(
    'argv, expected',
    [
        (['wiki'], ['wiki', 'open']),
        (['wiki', 'a.md'], ['wiki', 'open', 'a.md']),
        (['wiki', 'search', 'creatine'], ['wiki', 'search', 'creatine']),
        (['wiki', 'build'], ['wiki', 'build']),
        (['wiki', '--root', '/r'], ['wiki', '--root', '/r', 'open']),
        (['wiki', '--root', '/r', 'a.md'], ['wiki', '--root', '/r', 'open', 'a.md']),
        (['wiki', '--root=/r', 'search', 'x'], ['wiki', '--root=/r', 'search', 'x']),
        (['status'], ['status']),
        (['lint', '--json'], ['lint', '--json']),
    ],
)
def test_normalize_argv_inserts_the_implied_open(argv, expected):
    assert wiki.normalize_argv(argv) == expected


def test_normalize_argv_leaves_help_alone():
    assert wiki.normalize_argv(['wiki', '--help']) == ['wiki', '--help']


def test_normalize_argv_does_not_touch_a_later_wiki_word():
    # `wiki` as an argument to another subcommand is a path, not the subcommand.
    assert wiki.normalize_argv(['lint', 'wiki']) == ['lint', 'wiki']
    assert wiki.normalize_argv(['--root', '/r', 'wiki']) == ['--root', '/r', 'wiki', 'open']


# ── binary resolution ──────────────────────────────────────────────────────

def test_release_build_wins_over_debug(checkout, monkeypatch):
    monkeypatch.delenv('PODARCIS_TUI_BIN', raising=False)
    monkeypatch.setattr(wiki.shutil, 'which', lambda _name: None)
    for profile in ('debug', 'release'):
        target = checkout / 'tui' / 'target' / profile
        target.mkdir(parents=True)
        (target / wiki.BINARY).write_text('#!/bin/sh\n', encoding='utf-8')
    assert wiki.find_binary(checkout).parent.name == 'release'


def test_binary_falls_back_to_path(checkout, monkeypatch, tmp_path):
    monkeypatch.delenv('PODARCIS_TUI_BIN', raising=False)
    fake = tmp_path / 'from-path'
    fake.write_text('', encoding='utf-8')
    monkeypatch.setattr(wiki.shutil, 'which', lambda _name: str(fake))
    assert wiki.find_binary(checkout) == fake


def test_env_override_beats_everything(checkout, monkeypatch, tmp_path):
    target = checkout / 'tui' / 'target' / 'release'
    target.mkdir(parents=True)
    (target / wiki.BINARY).write_text('', encoding='utf-8')
    override = tmp_path / 'override'
    override.write_text('', encoding='utf-8')
    monkeypatch.setenv('PODARCIS_TUI_BIN', str(override))
    assert wiki.find_binary(checkout) == override


def test_a_missing_override_resolves_to_nothing(checkout, monkeypatch):
    monkeypatch.setenv('PODARCIS_TUI_BIN', '/nonexistent/podarcis-tui')
    assert wiki.find_binary(checkout) is None


def test_launch_execs_the_binary_with_the_root(checkout, monkeypatch):
    calls = {}
    monkeypatch.setenv('PODARCIS_TUI_BIN', sys.executable)
    monkeypatch.setattr(wiki.os, 'execv', lambda path, argv: calls.update(path=path, argv=argv))
    wiki.cmd_wiki(argparse.Namespace(root=str(checkout), path='wiki/health/caffeine.md'))
    assert calls['path'] == sys.executable
    assert calls['argv'][1:] == ['--root', str(checkout), 'wiki/health/caffeine.md']


def test_launch_without_a_binary_explains_how_to_get_one(checkout, monkeypatch, capsys):
    monkeypatch.setenv('PODARCIS_TUI_BIN', '/nonexistent/podarcis-tui')
    monkeypatch.setattr(wiki.shutil, 'which', lambda _name: None)
    assert wiki.cmd_wiki(argparse.Namespace(root=str(checkout), path=None)) == 1
    assert 'cargo build' in capsys.readouterr().out


def test_launch_outside_a_checkout_is_an_error(tmp_path, capsys):
    assert wiki.cmd_wiki(argparse.Namespace(root=str(tmp_path), path=None)) == 1
    assert 'not a Podarcis checkout' in capsys.readouterr().out


def test_build_without_cargo_reports_rather_than_raises(checkout, monkeypatch):
    (checkout / 'tui').mkdir()
    (checkout / 'tui' / 'Cargo.toml').write_text('[package]\n', encoding='utf-8')
    monkeypatch.setattr(wiki.shutil, 'which', lambda _name: None)
    assert wiki.build(checkout, quiet=True) is False


def test_build_without_a_crate_is_a_no_op(checkout):
    assert wiki.build(checkout, quiet=True) is False


# ── JSON contracts the front-end reads ─────────────────────────────────────

def test_lint_json_shape(checkout):
    payload = audit.lint(checkout)
    assert set(payload) == {'ok', 'root', 'files'}
    assert payload['ok'] is True
    assert payload['files'] == {}

    (checkout / 'wiki' / 'health' / 'broken.md').write_text(
        '---\ntitle: B\ntype: concept\ncategory: health\nrationale: r\n---\n[gone](nope.md)\n',
        encoding='utf-8',
    )
    payload = audit.lint(checkout)
    assert payload['ok'] is False
    issues = payload['files']['wiki/health/broken.md']
    assert [i['code'] for i in issues] == ['broken_link']
    assert set(issues[0]) == {'code', 'detail'}


def test_lint_codes_are_the_set_the_frontend_knows(checkout):
    '''The Rust front-end renders these nine codes; a new one would be silent.'''
    known = {
        'broken_link', 'missing_footnote', 'unused_footnote', 'unmatched_source',
        'positional_footnote', 'missing_frontmatter', 'yaml_error', 'page_length',
        'bloated_directory',
    }
    emitted = {
        issue['code']
        for issues in _every_issue_shape()
        for issue in issues
    }
    assert emitted <= known, f'unknown lint codes: {emitted - known}'


def _every_issue_shape() -> list[list[dict]]:
    shapes = [
        {'bloated_directory': 16},
        {'word_count': 99999},
        {'yaml_errors': ['bad']},
        {'broken_links': [('a.md', '/r/a.md')]},
        {'missing_footnotes': ['x']},
        {'unused_footnotes': ['x']},
        {'unmatched_sources': ['x']},
        {'positional_footnotes': ['1']},
        {'missing_frontmatter': ['missing']},
    ]
    return [audit.file_issues('wiki/a.md', shape) for shape in shapes]


def test_wiki_search_json_shape(checkout, capsys):
    args = argparse.Namespace(
        root=str(checkout), query=['adenosine'], json=True,
        collection='wiki', method='keyword', limit=5, no_rerank=True,
    )
    assert wiki.cmd_wiki_search(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) >= {'query', 'collection', 'method', 'warning', 'hits'}
    assert payload['hits'], 'the keyword fallback must work without qmd'
    assert set(payload['hits'][0]) >= {'path', 'title', 'score', 'collection', 'snippet'}
    assert payload['hits'][0]['path'] == 'wiki/health/caffeine.md'


def test_wiki_search_outside_a_checkout_is_an_error(tmp_path, capsys):
    args = argparse.Namespace(root=str(tmp_path), query=['x'], json=True)
    assert wiki.cmd_wiki_search(args) == 1
    assert 'not a Podarcis checkout' in capsys.readouterr().out


# ── the pieces the old TUI left behind ─────────────────────────────────────

def test_root_discovery_still_lives_at_the_top_level(checkout):
    assert is_wiki_root(checkout)
    nested = checkout / 'wiki' / 'health'
    assert find_wiki_root(cwd=nested) == checkout.resolve()
    with pytest.raises(WikiRootError):
        find_wiki_root(explicit=str(checkout / 'wiki'))


def test_the_tmux_frontend_is_gone():
    '''The compositor is deleted, not disabled: no module, no package data.'''
    import importlib

    for name in ('podarcis.tui', 'podarcis.tui.launch', 'podarcis.herdr'):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(name)


def test_no_source_file_still_references_the_compositor():
    root = Path(__file__).resolve().parents[2]
    offenders = []
    for path in (root / '.podarcis').rglob('*.py'):
        if '__pycache__' in path.parts or path.name == 'test_wiki_cli.py':
            continue
        text = path.read_text(encoding='utf-8')
        if 'podarcis.tui' in text or 'podarcis.herdr' in text:
            offenders.append(str(path.relative_to(root)))
    assert not offenders


def test_frontend_names_offer_the_tui_and_not_herdr():
    from podarcis import cli

    assert 'tui' in cli.FRONTEND_NAMES
    assert 'herdr' not in cli.FRONTEND_NAMES


def test_pass_through_flags_reach_the_subcommand(monkeypatch, capsys):
    '''`podarcis test -q` must not die at the top-level parser.'''
    from podarcis import cli

    seen = {}

    def fake_test(args):
        seen['args'] = list(args.remaining_args)
        return 0

    monkeypatch.setattr(cli, 'cmd_test', fake_test)
    assert cli.main(['test', '-q', '-x']) == 0
    assert seen['args'] == ['-q', '-x']


def test_an_unknown_flag_on_a_plain_subcommand_still_errors():
    from podarcis import cli

    with pytest.raises(SystemExit):
        cli.main(['status', '--nonsense'])


def test_the_crate_version_matches_the_engine():
    '''Drift between the two halves is detectable via `podarcis --version`.'''
    root = Path(__file__).resolve().parents[2]
    engine = next(
        line.split('=', 1)[1].strip().strip('"\'')
        for line in (root / 'pyproject.toml').read_text(encoding='utf-8').splitlines()
        if line.strip().startswith('version =')
    )
    crate = next(
        line.split('=', 1)[1].strip().strip('"\'')
        for line in (root / 'tui' / 'Cargo.toml').read_text(encoding='utf-8').splitlines()
        if line.strip().startswith('version =')
    )
    assert crate == engine, 'bump tui/Cargo.toml alongside pyproject.toml'
