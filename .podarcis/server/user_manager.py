'''User container management and strict volume isolation for Podarcis multi-user server.'''

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

import hashlib
import hmac
import secrets

USER_NAME_REGEX = re.compile(r'^[a-zA-Z0-9_-]{3,32}$')


class UserManager:
    '''Manages per-user workspaces, password authentication, and Docker container lifecycle.'''

    def __init__(self, root_dir: Path):
        self.root_dir = root_dir.resolve()
        self.data_dir = self.root_dir / 'data' / 'users'
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.registry_file = self.data_dir / 'users.json'
        self._init_registry()

    @staticmethod
    def hash_password(password: str, salt: Optional[str] = None) -> tuple[str, str]:
        '''Hash password using PBKDF2 with SHA-256 and a random salt.'''
        if not salt:
            salt = secrets.token_hex(16)
        pwd_hash = hashlib.pbkdf2_hmac(
            'sha256',
            password.encode('utf-8'),
            salt.encode('utf-8'),
            100_000,
        ).hex()
        return pwd_hash, salt

    @staticmethod
    def verify_password(password: str, password_hash: str, salt: str) -> bool:
        '''Verify password against stored hash and salt in constant time.'''
        expected_hash, _ = UserManager.hash_password(password, salt)
        return hmac.compare_digest(password_hash, expected_hash)

    def _init_registry(self) -> None:
        if not self.registry_file.exists():
            admin_hash, admin_salt = self.hash_password('admin')
            default_data = {
                'admin': {
                    'username': 'admin',
                    'role': 'admin',
                    'created_at': '2026-08-02T00:00:00Z',
                    'password_hash': admin_hash,
                    'password_salt': admin_salt,
                }
            }
            self.registry_file.write_text(json.dumps(default_data, indent=2), encoding='utf-8')

    def get_users_registry(self) -> Dict[str, Any]:
        try:
            return json.loads(self.registry_file.read_text(encoding='utf-8'))
        except Exception:
            return {}

    def save_users_registry(self, registry: Dict[str, Any]) -> None:
        self.registry_file.write_text(json.dumps(registry, indent=2), encoding='utf-8')

    def get_user_workspace(self, username: str) -> Path:
        ws = self.data_dir / username / 'workspace'
        ws.mkdir(parents=True, exist_ok=True)
        return ws

    def list_containers(self) -> List[Dict[str, Any]]:
        '''List running/stopped user containers using docker CLI.'''
        try:
            res = subprocess.run(
                [
                    'docker',
                    'ps',
                    '-a',
                    '--filter',
                    'label=podarcis.user',
                    '--format',
                    '{{.Names}}\t{{.Status}}\t{{.Ports}}\t{{.Labels}}',
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            containers = []
            if res.returncode == 0 and res.stdout.strip():
                for line in res.stdout.strip().split('\n'):
                    parts = line.split('\t')
                    if len(parts) >= 4:
                        name, status, ports, labels_str = parts[0], parts[1], parts[2], parts[3]
                        user = None
                        target_port = None
                        for label in labels_str.split(','):
                            if label.startswith('podarcis.user='):
                                user = label.split('=', 1)[1]
                            elif label.startswith('podarcis.port='):
                                target_port = label.split('=', 1)[1]
                        containers.append({
                            'name': name,
                            'status': status,
                            'ports': ports,
                            'username': user,
                            'port': target_port,
                        })
            return containers
        except Exception:
            return []

    def get_container_for_user(self, username: str) -> Optional[Dict[str, Any]]:
        for container in self.list_containers():
            if container.get('username') == username:
                return container
        return None

    def set_user_password(self, username: str, password: str) -> None:
        '''Set or update a user's password with a fresh hash and salt.'''
        registry = self.get_users_registry()
        if username not in registry:
            raise ValueError(f'User "{username}" does not exist.')
        pwd_hash, salt = self.hash_password(password)
        registry[username]['password_hash'] = pwd_hash
        registry[username]['password_salt'] = salt
        self.save_users_registry(registry)

    def authenticate_user(self, username: str, password: str) -> Optional[Dict[str, Any]]:
        '''Validate user credentials against stored hash or initialize password on first use.'''
        registry = self.get_users_registry()
        user_info = registry.get(username)
        if not user_info:
            return None

        stored_hash = user_info.get('password_hash')
        stored_salt = user_info.get('password_salt')

        if not stored_hash or not stored_salt:
            # First use / unset password: set password to submitted password
            self.set_user_password(username, password)
            return self.get_users_registry().get(username)

        if self.verify_password(password, stored_hash, stored_salt):
            return user_info
        return None

    def create_user(self, username: str, role: str = 'user', password: Optional[str] = None) -> Dict[str, Any]:
        if not USER_NAME_REGEX.match(username):
            raise ValueError('Invalid username. Must be 3-32 alphanumeric characters, hyphens, or underscores.')

        registry = self.get_users_registry()
        if username in registry:
            raise ValueError(f'User "{username}" already exists.')

        # Create isolated workspace directory
        workspace_dir = self.get_user_workspace(username)

        # Seed initial OKF layout
        (workspace_dir / 'wiki').mkdir(exist_ok=True)
        (workspace_dir / 'workspace').mkdir(exist_ok=True)

        user_info = {
            'username': username,
            'role': role,
            'created_at': __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
            'workspace_path': str(workspace_dir),
        }
        if password:
            pwd_hash, salt = self.hash_password(password)
            user_info['password_hash'] = pwd_hash
            user_info['password_salt'] = salt

        registry[username] = user_info
        self.save_users_registry(registry)
        return user_info

    def _allocate_free_port(self) -> int:
        '''Find next available port starting from 9001.'''
        used_ports = set()
        for c in self.list_containers():
            if c.get('port'):
                try:
                    used_ports.add(int(c['port']))
                except ValueError:
                    pass
        port = 9001
        while port in used_ports:
            port += 1
        return port

    def start_user_container(self, username: str) -> Dict[str, Any]:
        registry = self.get_users_registry()
        if username not in registry:
            self.create_user(username)

        existing = self.get_container_for_user(username)
        if existing and 'Up' in existing.get('status', ''):
            return existing

        container_name = f'podarcis-user-{username}'
        workspace_dir = self.get_user_workspace(username)
        port = self._allocate_free_port()

        # Stop old stopped container if exists
        subprocess.run(['docker', 'rm', '-f', container_name], capture_output=True, check=False)

        cmd = [
            'docker',
            'run',
            '-d',
            '--name',
            container_name,
            '--label',
            f'podarcis.user={username}',
            '--label',
            f'podarcis.port={port}',
            '-v',
            f'{workspace_dir}:/home/podarcis/workspace',
            '-e',
            f'PODARCIS_USER={username}',
            '-p',
            f'127.0.0.1:{port}:8000',
            '--restart',
            'unless-stopped',
            'podarcis-user:latest',
        ]

        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode != 0:
            # Fallback for dev / unbuilt image: return virtual container mock status
            return {
                'name': container_name,
                'username': username,
                'status': 'Virtual Mode (Build podarcis-user:latest image for live Docker run)',
                'port': str(port),
                'error': res.stderr.strip(),
            }

        return {
            'name': container_name,
            'username': username,
            'status': 'Up (running)',
            'port': str(port),
        }

    def stop_user_container(self, username: str) -> bool:
        container_name = f'podarcis-user-{username}'
        res = subprocess.run(['docker', 'rm', '-f', container_name], capture_output=True, check=False)
        return res.returncode == 0

    def delete_user(self, username: str) -> bool:
        if username == 'admin':
            raise ValueError('Cannot delete admin user.')

        self.stop_user_container(username)

        # Delete workspace directory
        user_dir = self.data_dir / username
        if user_dir.exists():
            shutil.rmtree(user_dir, ignore_errors=True)

        registry = self.get_users_registry()
        if username in registry:
            del registry[username]
            self.save_users_registry(registry)
            return True
        return False
