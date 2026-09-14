'''`engines.qmd` resolution.

An absent key is not a decision the user made, so it follows the binary rather
than defaulting to off and then reporting a config line nobody wrote. These
also pin the wording, because the Rust front-end mirrors it.
'''
from __future__ import annotations

from pathlib import Path

import pytest

from podarcis import search


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    (tmp_path / '.podarcis').mkdir()
    return tmp_path


def write(root: Path, body: str) -> None:
    (root / '.podarcis' / 'config.yaml').write_text(body, encoding='utf-8')


def test_absent_key_follows_the_binary(root, monkeypatch):
    monkeypatch.setattr(search.shutil, 'which', lambda _: '/usr/bin/qmd')
    assert search.qmd_status(root, environ={}) == ('enabled_ok', '/usr/bin/qmd')


def test_absent_key_without_the_binary_names_the_binary_not_the_config(root, monkeypatch):
    monkeypatch.setattr(search.shutil, 'which', lambda _: None)
    status, info = search.qmd_status(root, environ={})
    assert status == 'disabled'
    assert 'qmd' in info and 'PATH' in info
    assert 'false' not in info, 'no key was set to false; do not say one was'


def test_explicit_false_is_reported_as_the_decision_it_is(root, monkeypatch):
    write(root, 'engines:\n  qmd: false\n')
    monkeypatch.setattr(search.shutil, 'which', lambda _: '/usr/bin/qmd')
    status, info = search.qmd_status(root, environ={})
    assert status == 'disabled'
    assert 'engines.qmd: false' in info


def test_explicit_true_without_the_binary_is_broken_not_absent(root, monkeypatch):
    write(root, 'engines:\n  qmd: true\n')
    monkeypatch.setattr(search.shutil, 'which', lambda _: None)
    status, info = search.qmd_status(root, environ={})
    assert status == 'enabled_broken'
    assert 'not found in PATH' in info


def test_env_flag_still_wins_over_an_absent_key(root, monkeypatch):
    monkeypatch.setattr(search.shutil, 'which', lambda _: None)
    assert search.qmd_status(root, environ={'ENABLE_QMD': 'true'})[0] == 'enabled_broken'
    assert search.qmd_status(root, environ={'ENABLE_QMD': 'false'})[0] == 'disabled'


def test_a_malformed_config_falls_back_to_the_binary(root, monkeypatch):
    write(root, 'engines: [this is not a mapping\n')
    monkeypatch.setattr(search.shutil, 'which', lambda _: '/usr/bin/qmd')
    assert search.qmd_status(root, environ={})[0] == 'enabled_ok'
