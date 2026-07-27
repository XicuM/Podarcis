'''Unit tests for uninstall.py removal routines.'''

import os
import sys
from pathlib import Path
import pytest
import uninstall


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def fake_root(tmp_path, monkeypatch):
    '''Redirect uninstall.root to a temporary directory.'''
    monkeypatch.setattr(uninstall, 'root', tmp_path)
    return tmp_path


# ── _find_global_symlinks ─────────────────────────────────────────────────────

def test_find_symlinks_finds_matching(tmp_path, monkeypatch):
    '''Symlinks pointing at <root>/podarcis are returned.'''
    podarcis_script = tmp_path / 'podarcis'
    podarcis_script.write_text('#!/bin/sh\nexec python .podarcis/cli.py "$@"\n')
    monkeypatch.setattr(uninstall, 'root', tmp_path)

    local_bin = tmp_path / 'fake_local_bin'
    local_bin.mkdir()
    good_link = local_bin / 'podarcis'
    good_link.symlink_to(podarcis_script)
    unrelated_link = local_bin / 'other'
    unrelated_link.symlink_to(tmp_path / 'other_script')  # not the podarcis script

    # Patch Path.home() so we scan our fake local_bin
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path / 'home'))
    (tmp_path / 'home' / '.local' / 'bin').mkdir(parents=True)
    real_link = tmp_path / 'home' / '.local' / 'bin' / 'podarcis'
    real_link.symlink_to(podarcis_script)

    found = uninstall._find_global_symlinks()
    assert real_link in found
    assert len(found) == 1


def test_find_symlinks_empty_when_none(tmp_path, monkeypatch):
    '''No symlinks returned when ~/.local/bin does not exist.'''
    monkeypatch.setattr(uninstall, 'root', tmp_path)
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path / 'home'))
    # ~/.local/bin is intentionally not created

    found = uninstall._find_global_symlinks()
    assert found == []


# ── _remove_global_symlinks ───────────────────────────────────────────────────

def test_remove_symlinks_dry_run(tmp_path, monkeypatch, capsys):
    '''Dry-run removes nothing.'''
    link = tmp_path / 'mylink'
    link.symlink_to(tmp_path / 'podarcis')

    uninstall._remove_global_symlinks([link], dry_run=True)

    assert link.exists() or link.is_symlink()  # symlink still present


def test_remove_symlinks_live(tmp_path, monkeypatch):
    '''Live run removes the symlink.'''
    target = tmp_path / 'podarcis'
    target.write_text('#!/bin/sh\n')
    link = tmp_path / 'podarcis_link'
    link.symlink_to(target)

    count = uninstall._remove_global_symlinks([link], dry_run=False)

    assert count == 1
    assert not link.exists()
    assert not link.is_symlink()


# ── _remove_venv ──────────────────────────────────────────────────────────────

def test_remove_venv_absent(fake_root, capsys):
    '''.venv absent → returns False without error.'''
    result = uninstall._remove_venv(dry_run=False)
    assert result is False


def test_remove_venv_dry_run(fake_root):
    '''Dry-run keeps .venv intact.'''
    venv = fake_root / '.venv'
    venv.mkdir()
    (venv / 'pyvenv.cfg').write_text('home = /usr/bin\n')

    result = uninstall._remove_venv(dry_run=True)

    assert result is False
    assert venv.exists()


def test_remove_venv_live(fake_root):
    '''Live run deletes .venv and returns True.'''
    venv = fake_root / '.venv'
    venv.mkdir()
    (venv / 'pyvenv.cfg').write_text('home = /usr/bin\n')

    result = uninstall._remove_venv(dry_run=False)

    assert result is True
    assert not venv.exists()


# ── _remove_build_artefacts ───────────────────────────────────────────────────

def test_remove_build_artefacts_dry_run(fake_root):
    '''Dry-run leaves egg-info and __pycache__ intact.'''
    egg_info = fake_root / 'podarcis.egg-info'
    egg_info.mkdir()
    cache = fake_root / 'somepkg' / '__pycache__'
    cache.mkdir(parents=True)

    removed = uninstall._remove_build_artefacts(dry_run=True)

    assert removed == []
    assert egg_info.exists()
    assert cache.exists()


def test_remove_build_artefacts_live(fake_root):
    '''Live run removes egg-info, __pycache__, and .pytest_cache.'''
    egg_info = fake_root / 'podarcis.egg-info'
    egg_info.mkdir()
    cache = fake_root / 'somepkg' / '__pycache__'
    cache.mkdir(parents=True)
    (cache / 'mod.cpython-311.pyc').write_bytes(b'')
    pytest_cache = fake_root / '.pytest_cache'
    pytest_cache.mkdir()

    removed = uninstall._remove_build_artefacts(dry_run=False)

    assert not egg_info.exists()
    assert not cache.exists()
    assert not pytest_cache.exists()
    assert len(removed) >= 3


# ── _remove_config ────────────────────────────────────────────────────────────

def test_remove_config_absent(fake_root):
    '''Missing config.yaml → returns False without error.'''
    (fake_root / '.podarcis').mkdir(parents=True)
    result = uninstall._remove_config(dry_run=False)
    assert result is False


def test_remove_config_dry_run(fake_root):
    '''Dry-run keeps config.yaml.'''
    pod_dir = fake_root / '.podarcis'
    pod_dir.mkdir(parents=True)
    cfg = pod_dir / 'config.yaml'
    cfg.write_text('backend: opencode\n')

    result = uninstall._remove_config(dry_run=True)

    assert result is False
    assert cfg.exists()


def test_remove_config_live(fake_root):
    '''Live run removes config.yaml and returns True.'''
    pod_dir = fake_root / '.podarcis'
    pod_dir.mkdir(parents=True)
    cfg = pod_dir / 'config.yaml'
    cfg.write_text('backend: opencode\n')

    result = uninstall._remove_config(dry_run=False)

    assert result is True
    assert not cfg.exists()


def test_default_does_not_remove_config(fake_root):
    '''Without --purge, config.yaml is left untouched by the artefact cleaner.'''
    pod_dir = fake_root / '.podarcis'
    pod_dir.mkdir(parents=True)
    cfg = pod_dir / 'config.yaml'
    cfg.write_text('backend: opencode\n')

    # _remove_build_artefacts should NOT touch config.yaml
    uninstall._remove_build_artefacts(dry_run=False)

    assert cfg.exists(), 'config.yaml must not be removed by the build artefact cleaner'
