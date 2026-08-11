'''Backend-specific MCP config adapters.

Design principle:
  - .agents/mcp_config.json stores ONLY server definitions (command, args, env)
  - Each backend's native config is the source of truth for enable/disable state
  - Podarcis only writes to the ACTIVE backend's config
  - Switching backends never touches other backends' configs

Supported backends:
  - opencode   → opencode.json (project root)
  - claude     → .mcp.json (project root)
  - codex      → .codex/config.toml (project & user)
  - agy        → .mcp.json (same format as Claude Code)
  - hermes     → ~/.hermes/config.yaml
  - openclaw   → ~/.openclaw/openclaw.json
  - none       → no-op (no MCP config written)
'''
from __future__ import annotations

import os
import sys
from abc import ABC, abstractmethod
from pathlib import Path

from common import load_json, load_yaml, save_json, save_yaml


# ── Helpers ──────────────────────────────────────────────────────────────────

def _venv_python(root: Path) -> str:
    py_exec = 'Scripts/python.exe' if sys.platform == 'win32' else 'bin/python'
    return str(root / '.venv' / py_exec)


def _server_name_map(root: Path) -> dict[str, str]:
    '''Derive dir_name → mcp_key from mcp_config.json or opencode.json.'''
    mapping: dict[str, str] = {}

    mcp_cfg = load_json(root / '.agents' / 'mcp_config.json')
    for key, cfg in mcp_cfg.get('mcpServers', {}).items():
        if (args := cfg.get('args', [])) and '/mcp/' in args[0].replace('\\', '/'):
            parts = args[0].replace('\\', '/').split('/')
            try:
                mapping[parts[parts.index('mcp') + 1]] = key
            except (ValueError, IndexError):
                pass

    opencode_cfg = load_json(root / 'opencode.json')
    for key, cfg in opencode_cfg.get('mcp', {}).items():
        cmd = cfg.get('command', [])
        if isinstance(cmd, list) and len(cmd) >= 2 and '/mcp/' in cmd[1]:
            parts = cmd[1].replace('\\', '/').split('/')
            try:
                mapping[parts[parts.index('mcp') + 1]] = key
            except (ValueError, IndexError):
                pass

    if (mcp_dir := root / '.agents' / 'mcp').exists():
        for d in mcp_dir.iterdir():
            if d.is_dir() and d.name not in mapping:
                mapping[d.name] = f'{d.name}-mcp'

    return mapping


def _venv_podarcis_mcp(root: Path) -> str:
    bin_name = 'Scripts/podarcis-mcp.exe' if sys.platform == 'win32' else 'bin/podarcis-mcp'
    venv_bin = root / '.venv' / bin_name
    if venv_bin.exists():
        return str(venv_bin)
    return 'podarcis-mcp'


def discover_server_definitions(root: Path) -> list[dict]:
    '''Discover MCP server definitions. Returns single Podarcis Gateway entry.'''
    mcp_bin = _venv_podarcis_mcp(root)
    cfg_file = str(root / '.podarcis' / 'config.yaml')
    return [{
        'key': 'podarcis',
        'dir_name': 'gateway',
        'command': [mcp_bin, '--config', cfg_file],
        'env': {'PROJECT_ROOT': str(root)},
    }]


# ══════════════════════════════════════════════════════════════════════════════
# ABSTRACT ADAPTER BASE & HIERARCHY
# ══════════════════════════════════════════════════════════════════════════════

class BaseAdapter(ABC):
    '''Abstract base interface for backend-specific MCP config adapters.'''

    def __init__(self, name: str) -> None:
        self.name = name

    @abstractmethod
    def read_enabled(self, root: Path) -> dict[str, bool]:
        '''Read current enable/disable state from native config.'''

    @abstractmethod
    def write_enabled(self, root: Path, key: str, enabled: bool) -> None:
        '''Write enable/disable state to native config.'''

    @abstractmethod
    def generate(self, root: Path) -> Path | None:
        '''Regenerate native config from definitions + current state.'''


# ── OpenCode Adapter ──────────────────────────────────────────────────────────

class OpenCodeAdapter(BaseAdapter):
    '''Adapter for OpenCode (opencode.json).'''

    def __init__(self) -> None:
        super().__init__('opencode')

    def read_enabled(self, root: Path) -> dict[str, bool]:
        data = load_json(root / 'opencode.json')
        return {k: v.get('enabled', True) for k, v in data.get('mcp', {}).items()}

    def write_enabled(self, root: Path, key: str, enabled: bool) -> None:
        path = root / 'opencode.json'
        data = load_json(path)
        mcp = data.setdefault('mcp', {})
        if key in mcp:
            mcp[key]['enabled'] = enabled
        save_json(path, data)

    def generate(self, root: Path) -> Path | None:
        path = root / 'opencode.json'
        existing = load_json(path) or {'$schema': 'https://opencode.ai/config.json'}

        existing_mcp = existing.get('mcp', {})
        enabled_map = self.read_enabled(root)
        LEGACY_KEYS = {'wiki-mcp', 'research-mcp', 'finance-mcp', 'menumaker-mcp', 'diagnostics-mcp', 'zoom2okf-mcp'}
        for k in LEGACY_KEYS:
            existing_mcp.pop(k, None)

        for srv in discover_server_definitions(root):
            key = srv['key']
            existing_mcp[key] = {
                'type': 'local',
                'command': srv['command'],
                'environment': srv['env'],
                'enabled': enabled_map.get(key, True),
            }

        existing['mcp'] = existing_mcp
        save_json(path, existing)
        return path


# ── Claude Code & Derived Adapters ───────────────────────────────────────────

class ClaudeCodeAdapter(BaseAdapter):
    '''Adapter for Claude Code / .mcp.json format.

    Claude Code has no enabled flag; presence in mcpServers indicates enabled.
    '''

    def __init__(self, name: str = 'claude') -> None:
        super().__init__(name)

    def get_config_path(self, root: Path) -> Path | None:
        return root / '.mcp.json'

    def read_enabled(self, root: Path) -> dict[str, bool]:
        if not (path := self.get_config_path(root)) or not path.exists():
            return {}
        data = load_json(path)
        return {k: True for k in data.get('mcpServers', {})}

    def write_enabled(self, root: Path, key: str, enabled: bool) -> None:
        if not (path := self.get_config_path(root)):
            return
        data = load_json(path) if path.exists() else {}
        servers = data.setdefault('mcpServers', {})
        if enabled:
            if key not in servers:
                servers[key] = {'command': '', 'args': []}
        else:
            servers.pop(key, None)
        save_json(path, data)

    def generate(self, root: Path) -> Path | None:
        if not (path := self.get_config_path(root)):
            return None
        existing = load_json(path) if path.exists() else {}
        existing_servers = existing.get('mcpServers', {})
        enabled_map = self.read_enabled(root)

        LEGACY_KEYS = {'wiki-mcp', 'research-mcp', 'finance-mcp', 'menumaker-mcp', 'diagnostics-mcp', 'zoom2okf-mcp'}
        for k in LEGACY_KEYS:
            existing_servers.pop(k, None)

        for srv in discover_server_definitions(root):
            key = srv['key']
            if not enabled_map.get(key, True):
                existing_servers.pop(key, None)
                continue
            existing_servers[key] = {
                'command': srv['command'][0],
                'args': srv['command'][1:],
                'env': srv['env'],
            }

        existing['mcpServers'] = existing_servers
        save_json(path, existing)
        return path


class AgyAdapter(ClaudeCodeAdapter):
    '''Adapter for Antigravity (agy), sharing .mcp.json with Claude Code.'''

    def __init__(self) -> None:
        super().__init__('agy')


class ClaudeDesktopAdapter(ClaudeCodeAdapter):
    '''Adapter for Claude Desktop system configuration.'''

    def __init__(self) -> None:
        super().__init__('claude_desktop')

    def get_config_path(self, root: Path) -> Path | None:  # noqa: ARG002
        if sys.platform == 'darwin':
            return (
                Path.home() / 'Library' / 'Application Support' / 'Claude'
                / 'claude_desktop_config.json'
            )
        elif sys.platform == 'win32':
            appdata = os.environ.get('APPDATA', '')
            return Path(appdata) / 'Claude' / 'claude_desktop_config.json'
        return Path.home() / '.config' / 'Claude' / 'claude_desktop_config.json'

    def generate(self, root: Path) -> Path | None:
        path = self.get_config_path(root)
        if not path or not path.exists():
            return None
        return super().generate(root)


# ── Codex Adapter ─────────────────────────────────────────────────────────────

def _toml_escape(s: str) -> str:
    return s.replace('\\', '\\\\').replace('"', '\\"')


class CodexAdapter(BaseAdapter):
    '''Adapter for Codex (.codex/config.toml).'''

    def __init__(self) -> None:
        super().__init__('codex')

    @property
    def user_config_path(self) -> Path:
        return Path.home() / '.codex' / 'config.toml'

    def project_config_path(self, root: Path) -> Path:
        return root / '.codex' / 'config.toml'

    def _read_file_enabled(self, path: Path) -> dict[str, bool]:
        if not path.exists():
            return {}
        result: dict[str, bool] = {}
        current_server = None
        for line in path.read_text(encoding='utf-8').splitlines():
            stripped = line.strip()
            if stripped.startswith('[mcp_servers.'):
                current_server = stripped[13:].rstrip(']').split('.')[0]
            elif current_server and stripped.startswith('enabled'):
                val = stripped.split('=', 1)[1].strip().lower()
                result[current_server] = val == 'true'
                current_server = None
        return result

    def read_enabled(self, root: Path) -> dict[str, bool]:
        merged = self._read_file_enabled(self.user_config_path)
        merged |= self._read_file_enabled(self.project_config_path(root))
        return merged

    def _write_file_enabled(self, path: Path, key: str, enabled: bool) -> None:
        if not path.exists():
            return
        lines = path.read_text(encoding='utf-8').splitlines()
        in_target = False
        new_lines = []
        val_str = 'true' if enabled else 'false'
        for line in lines:
            stripped = line.strip()
            if stripped == f'[mcp_servers.{key}]':
                in_target = True
                new_lines.append(line)
            elif in_target and stripped.startswith('['):
                new_lines.append(f'enabled = {val_str}')
                in_target = False
                new_lines.append(line)
            elif in_target and stripped.startswith('enabled'):
                new_lines.append(f'enabled = {val_str}')
                in_target = False
            else:
                new_lines.append(line)
        if in_target:
            new_lines.append(f'enabled = {val_str}')
        path.write_text('\n'.join(new_lines) + '\n', encoding='utf-8')

    def write_enabled(self, root: Path, key: str, enabled: bool) -> None:
        self._write_file_enabled(self.user_config_path, key, enabled)
        self._write_file_enabled(self.project_config_path(root), key, enabled)

    def _write_toml(
        self, path: Path, servers: list[dict], enabled_map: dict[str, bool]
    ) -> None:
        existing_content = (
            path.read_text(encoding='utf-8') if path.exists() else ''
        )
        lines = existing_content.splitlines()

        mcp_start = None
        mcp_end = None
        in_mcp = False
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith('[mcp_servers'):
                if mcp_start is None:
                    mcp_start = i
                in_mcp = True
            elif (
                in_mcp
                and stripped.startswith('[')
                and not stripped.startswith('[mcp_servers')
            ):
                mcp_end = i
                in_mcp = False
        if in_mcp:
            mcp_end = len(lines)

        pre_lines = lines[:mcp_start] if mcp_start is not None else []
        post_lines = lines[mcp_end:] if mcp_end is not None else []

        with open(path, 'w', encoding='utf-8') as f:
            for line in pre_lines:
                f.write(line + '\n')

            for srv in servers:
                key = srv['key']
                enabled = enabled_map.get(key, True)
                py_bin = srv['command'][0]
                script = srv['command'][1]
                val_str = 'true' if enabled else 'false'
                f.write(f'[mcp_servers.{key}]\n')
                f.write(f'command = "{_toml_escape(py_bin)}"\n')
                f.write(f'args = ["{_toml_escape(script)}"]\n')
                f.write(f'enabled = {val_str}\n')
                if srv['env']:
                    f.write(f'\n[mcp_servers.{key}.env]\n')
                    for k, v in srv['env'].items():
                        f.write(f'{k} = "{_toml_escape(str(v))}"\n')
                f.write('\n')

            for line in post_lines:
                f.write(line + '\n')

    def generate(self, root: Path) -> Path | None:
        target = self.project_config_path(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._write_toml(
            target,
            discover_server_definitions(root),
            self.read_enabled(root),
        )
        return target


# ── Hermes Adapter ────────────────────────────────────────────────────────────

class HermesAdapter(BaseAdapter):
    '''Adapter for Hermes (~/.hermes/config.yaml).'''

    def __init__(self) -> None:
        super().__init__('hermes')

    @property
    def config_path(self) -> Path:
        return Path.home() / '.hermes' / 'config.yaml'

    def read_enabled(self, root: Path) -> dict[str, bool]:  # noqa: ARG002
        if not self.config_path.exists():
            return {}
        data = load_yaml(self.config_path)
        return {
            k: (v.get('enabled', True) if isinstance(v, dict) else True)
            for k, v in data.get('mcp_servers', {}).items()
        }

    def write_enabled(self, root: Path, key: str, enabled: bool) -> None:  # noqa: ARG002
        path = self.config_path
        if not path.parent.exists():
            return
        data = load_yaml(path) if path.exists() else {}
        mcp = data.setdefault('mcp_servers', {})
        if key in mcp and isinstance(mcp[key], dict):
            mcp[key]['enabled'] = enabled
        save_yaml(path, data)

    def generate(self, root: Path) -> Path | None:
        path = self.config_path
        if not path.parent.exists():
            return None
        data = load_yaml(path) if path.exists() else {}
        mcp_servers = data.get('mcp_servers', {})
        enabled_map = self.read_enabled(root)

        for srv in discover_server_definitions(root):
            key = srv['key']
            entry: dict = {
                'command': srv['command'][0],
                'args': [srv['command'][1]],
                'enabled': enabled_map.get(key, True),
            }
            if srv['env']:
                entry['env'] = srv['env']
            mcp_servers[key] = entry

        data['mcp_servers'] = mcp_servers
        save_yaml(path, data)
        return path


# ── OpenClaw Adapter ──────────────────────────────────────────────────────────

class OpenClawAdapter(BaseAdapter):
    '''Adapter for OpenClaw (~/.openclaw/openclaw.json).'''

    def __init__(self) -> None:
        super().__init__('openclaw')

    @property
    def config_path(self) -> Path:
        return Path.home() / '.openclaw' / 'openclaw.json'

    def read_enabled(self, root: Path) -> dict[str, bool]:  # noqa: ARG002
        if not self.config_path.exists():
            return {}
        data = load_json(self.config_path)
        return {
            k: (v.get('enabled', True) if isinstance(v, dict) else True)
            for k, v in data.get('mcp', {}).get('servers', {}).items()
        }

    def write_enabled(self, root: Path, key: str, enabled: bool) -> None:  # noqa: ARG002
        path = self.config_path
        if not path.parent.exists():
            return
        data = load_json(path) if path.exists() else {}
        mcp = data.setdefault('mcp', {})
        servers = mcp.setdefault('servers', {})
        if key in servers and isinstance(servers[key], dict):
            servers[key]['enabled'] = enabled
        save_json(path, data)

    def generate(self, root: Path) -> Path | None:
        path = self.config_path
        if not path.parent.exists():
            return None
        data = load_json(path) if path.exists() else {}
        mcp = data.setdefault('mcp', {})
        servers = mcp.setdefault('servers', {})
        enabled_map = self.read_enabled(root)

        for srv in discover_server_definitions(root):
            key = srv['key']
            entry: dict = {
                'command': srv['command'][0],
                'args': [srv['command'][1]],
                'enabled': enabled_map.get(key, True),
            }
            if srv['env']:
                entry['env'] = srv['env']
            servers[key] = entry

        save_json(path, data)
        return path


# ── None (No-Op) Adapter ──────────────────────────────────────────────────────

class NoneAdapter(BaseAdapter):
    '''No-op adapter when no native MCP config write is desired.'''

    def __init__(self) -> None:
        super().__init__('none')

    def read_enabled(self, root: Path) -> dict[str, bool]:  # noqa: ARG002
        return {}

    def write_enabled(self, root: Path, key: str, enabled: bool) -> None:  # noqa: ARG002
        pass

    def generate(self, root: Path) -> Path | None:  # noqa: ARG002
        return None


# ══════════════════════════════════════════════════════════════════════════════
# ADAPTER REGISTRY & PUBLIC API
# ══════════════════════════════════════════════════════════════════════════════

ADAPTERS: dict[str, BaseAdapter] = {
    adapter.name: adapter
    for adapter in [
        OpenCodeAdapter(),
        ClaudeCodeAdapter(),
        CodexAdapter(),
        AgyAdapter(),
        HermesAdapter(),
        OpenClawAdapter(),
        NoneAdapter(),
    ]
}


def get_adapter(backend: str) -> BaseAdapter | None:
    '''Retrieve adapter instance for backend name.'''
    return ADAPTERS.get(backend)


def read_enabled(root: Path, backend: str) -> dict[str, bool]:
    '''Read enable/disable state from specified backend's native config.'''
    if adapter := ADAPTERS.get(backend):
        return adapter.read_enabled(root)
    return {}


def write_enabled(root: Path, backend: str, key: str, enabled: bool) -> None:
    '''Write enable/disable state to specified backend's native config.'''
    if adapter := ADAPTERS.get(backend):
        adapter.write_enabled(root, key, enabled)


def generate_for_backend(root: Path, backend: str) -> Path | None:
    '''Regenerate native config for specified backend.'''
    if adapter := ADAPTERS.get(backend):
        return adapter.generate(root)
    return None


def generate_all(root: Path) -> dict[str, Path | None]:
    '''Regenerate native config for all backends.'''
    return {name: adapter.generate(root) for name, adapter in ADAPTERS.items()}
