'''Unit tests for Podarcis server, user manager, and container isolation.'''

import json
from pathlib import Path
import pytest

from podarcis.server.user_manager import UserManager


@pytest.fixture
def temp_root(tmp_path: Path) -> Path:
    data_dir = tmp_path / 'data' / 'users'
    data_dir.mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_user_manager_creation_and_registry(temp_root: Path):
    mgr = UserManager(temp_root)

    # Initial registry has admin
    reg = mgr.get_users_registry()
    assert 'admin' in reg

    # Create new user
    uinfo = mgr.create_user('alice', role='user')
    assert uinfo['username'] == 'alice'
    assert uinfo['role'] == 'user'

    ws_path = mgr.get_user_workspace('alice')
    assert ws_path.exists()
    assert (ws_path / 'wiki').exists()
    assert (ws_path / 'workspace').exists()

    # Verify duplicate username raises error
    with pytest.raises(ValueError):
        mgr.create_user('alice')


def test_user_manager_delete_isolation(temp_root: Path):
    mgr = UserManager(temp_root)
    mgr.create_user('bob')

    assert 'bob' in mgr.get_users_registry()
    assert mgr.get_user_workspace('bob').exists()

    # Delete bob
    res = mgr.delete_user('bob')
    assert res is True
    assert 'bob' not in mgr.get_users_registry()
    assert not (temp_root / 'data' / 'users' / 'bob').exists()

    # Admin deletion must fail
    with pytest.raises(ValueError):
        mgr.delete_user('admin')


def test_user_manager_password_authentication(temp_root: Path):
    mgr = UserManager(temp_root)

    # Admin authentication with default password 'admin'
    admin_info = mgr.authenticate_user('admin', 'admin')
    assert admin_info is not None
    assert admin_info['username'] == 'admin'

    # Admin auth fails with wrong password
    assert mgr.authenticate_user('admin', 'wrongpass') is None

    # Create user with password
    mgr.create_user('charlie', password='SecretPassword123!')
    assert mgr.authenticate_user('charlie', 'SecretPassword123!') is not None
    assert mgr.authenticate_user('charlie', 'wrongpass') is None

    # Update password
    mgr.set_user_password('charlie', 'NewPassword456!')
    assert mgr.authenticate_user('charlie', 'SecretPassword123!') is None
    assert mgr.authenticate_user('charlie', 'NewPassword456!') is not None

