'''Tests for the lint gate. The linter itself is tested in Rust.

`audit.py` used to hold a copy of the linter; it now holds only the gate, so
what is worth testing here is how the gate behaves when it cannot reach the
binary — the case that decides whether a broken install blocks a commit or
waves it through.
'''

import podarcis
from podarcis.audit import audit_gate, podarcis_bin

import pytest


def test_podarcis_bin_prefers_the_environment(tmp_path, monkeypatch):
    fake = tmp_path / 'podarcis'
    fake.write_text('#!/bin/sh\n', encoding='utf-8')
    monkeypatch.setenv('PODARCIS_BIN', str(fake))
    assert podarcis_bin(tmp_path) == str(fake)


def test_podarcis_bin_ignores_an_environment_pointing_at_nothing(tmp_path, monkeypatch):
    '''A stale PODARCIS_BIN must not shadow a working build.'''
    built = tmp_path / 'tui' / 'target' / 'release'
    built.mkdir(parents=True)
    (built / 'podarcis').write_text('#!/bin/sh\n', encoding='utf-8')
    monkeypatch.setenv('PODARCIS_BIN', str(tmp_path / 'gone'))
    assert podarcis_bin(tmp_path) == str(built / 'podarcis')


def _no_binary_anywhere(tmp_path, monkeypatch):
    monkeypatch.delenv('PODARCIS_BIN', raising=False)
    monkeypatch.setattr(podarcis, 'ROOT_DIR', tmp_path)
    monkeypatch.setattr('shutil.which', lambda _name: None)


def test_podarcis_bin_says_how_to_build_when_there_is_none(tmp_path, monkeypatch):
    _no_binary_anywhere(tmp_path, monkeypatch)
    with pytest.raises(FileNotFoundError, match='podarcis build'):
        podarcis_bin(tmp_path)


def test_the_gate_fails_closed_when_the_linter_cannot_run(tmp_path, monkeypatch):
    '''No checker means no verdict, and no verdict must not mean "commit".'''
    _no_binary_anywhere(tmp_path, monkeypatch)
    ok, detail = audit_gate(tmp_path)
    assert ok is False
    assert 'podarcis build' in detail


def test_the_gate_passes_a_tree_with_nothing_to_report(tmp_path):
    '''Exercises the real binary, so it also proves the wiring.'''
    try:
        podarcis_bin(tmp_path)
    except FileNotFoundError:
        pytest.skip('podarcis binary not built')
    wiki = tmp_path / 'wiki'
    wiki.mkdir()
    (wiki / 'page.md').write_text(
        '---\ntype: concept\ncategory: test\nrationale: r\n---\n\nBody.\n',
        encoding='utf-8',
    )
    ok, detail = audit_gate(tmp_path)
    assert ok is True, detail
    assert detail == 'Audit passed.'


def test_the_gate_reports_the_findings_it_blocks_on(tmp_path):
    '''And blocks on the tree it was given, not on the checkout it runs from.'''
    try:
        podarcis_bin(tmp_path)
    except FileNotFoundError:
        pytest.skip('podarcis binary not built')
    wiki = tmp_path / 'wiki'
    wiki.mkdir()
    (wiki / 'bad.md').write_text(
        '---\ntype: concept\ncategory: test\nrationale: r\n---\n\nSee [gone](./nope.md).\n',
        encoding='utf-8',
    )
    ok, detail = audit_gate(tmp_path)
    assert ok is False
    assert 'broken_link' in detail
    assert 'bad.md' in detail
