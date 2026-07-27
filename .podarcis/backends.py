'''Backend-specific MCP config adapters.

Design principle:
  - .agents/mcp_config.json stores ONLY server definitions (command, args, env)
  - Each backend's native config is the source of truth for enable/disable state
  - Podarcis only writes to the ACTIVE backend's config
  - Switching backends never touches other backends' configs

Supported backends:
  - opencode   → opencode.json (project root)
  - claude     → .mcp.json (project root)
  - codex      → .codex/config.toml (project)
  - agy        → .mcp.json (same as Claude Code)
  - hermes     → ~/.hermes/config.yaml
  - openclaw   → ~/.openclaw/openclaw.json
  - none        → no-op (no MCP config written)
'''
from __future__ import annotations

import os
import sys
from pathlib import Path

from common import load_json, save_json, load_yaml, save_yaml


# ── Helpers ──────────────────────────────────────────────────────────────────

def _venv_python(root: Path) -> str:
    return str(root / '.venv' / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python'))


def _server_name_map(root: Path) -> dict[str, str]:
    '''Derive dir_name → mcp_key from mcp_config.json or opencode.json.'''
    mapping: dict[str, str] = {}

    mcp_cfg = load_json(root / '.agents' / 'mcp_config.json')
    for key, cfg in mcp_cfg.get('mcpServers', {}).items():
        args = cfg.get('args', [])
        if args and '/mcp/' in args[0].replace('\\', '/'):
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

    mcp_dir = root / '.agents' / 'mcp'
    if mcp_dir.exists():
        for d in mcp_dir.iterdir():
            if d.is_dir() and d.name not in mapping:
                mapping[d.name] = f'{d.name}-mcp'

    return mapping


def discover_server_definitions(root: Path) -> list[dict]:
    '''Discover MCP server definitions from filesystem.

    Returns server list with command/env info but NO enabled state —
    each backend adapter determines enabled state from its own config.
    '''
    mcp_dir = root / '.agents' / 'mcp'
    smap = _server_name_map(root)
    mcp_cfg_path = root / '.agents' / 'mcp_config.json'
    mcp_cfg_data = load_json(mcp_cfg_path)
    mcp_servers_cfg = mcp_cfg_data.get('mcpServers', {})

    servers = []
    if mcp_dir.exists():
        for d in sorted(mcp_dir.iterdir()):
            if d.is_dir() and (d / 'server.py').exists():
                dir_name = d.name
                key = smap.get(dir_name, f'{dir_name}-mcp')
                server_script = f'.agents/mcp/{dir_name}/server.py'

                env = {'PROJECT_ROOT': str(root)}
                if key in mcp_servers_cfg:
                    cfg_env = mcp_servers_cfg[key].get('env', {})
                    for k, v in cfg_env.items():
                        env[k] = v

                if key == 'research-mcp' and 'SEMANTIC_SCHOLAR_API_KEY' not in env:
                    env['SEMANTIC_SCHOLAR_API_KEY'] = ''

                python_bin = _venv_python(root)
                servers.append({
                    'key': key,
                    'dir_name': dir_name,
                    'command': [python_bin, server_script],
                    'env': env,
                })

    return servers


# ══════════════════════════════════════════════════════════════════════════════
# BACKEND ADAPTERS
# Each adapter implements:
#   read_enabled(root)  → dict[str, bool]    # read state from native config
#   write_enabled(root, key, enabled)        # write state to native config
#   generate(root)      → Path               # regenerate full config from defs + state
# ══════════════════════════════════════════════════════════════════════════════


# ── OpenCode ─────────────────────────────────────────────────────────────────

def _opencode_read_enabled(root: Path) -> dict[str, bool]:
    data = load_json(root / 'opencode.json')
    return {k: v.get('enabled', True) for k, v in data.get('mcp', {}).items()}


def _opencode_write_enabled(root: Path, key: str, enabled: bool) -> None:
    path = root / 'opencode.json'
    data = load_json(path)
    mcp = data.setdefault('mcp', {})
    if key in mcp:
        mcp[key]['enabled'] = enabled
    save_json(path, data)


def _opencode_generate(root: Path) -> Path:
    path = root / 'opencode.json'
    existing = load_json(path)
    if not existing:
        existing = {'$schema': 'https://opencode.ai/config.json'}

    existing_mcp = existing.get('mcp', {})
    enabled_map = _opencode_read_enabled(root)

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


# ── Claude Code / Agy ───────────────────────────────────────────────────────
# Claude Code has NO enabled toggle — server is present or absent.

def _claude_code_config_path(root: Path) -> Path:
    return root / '.mcp.json'


def _claude_code_read_enabled(root: Path) -> dict[str, bool]:
    data = load_json(_claude_code_config_path(root))
    # All listed servers are enabled (Claude has no toggle)
    return {k: True for k in data.get('mcpServers', {})}


def _claude_code_write_enabled(root: Path, key: str, enabled: bool) -> None:
    path = _claude_code_config_path(root)
    data = load_json(path)
    servers = data.setdefault('mcpServers', {})
    if enabled:
        # Restore with defaults — caller should generate() to fill in real command
        if key not in servers:
            servers[key] = {'command': '', 'args': []}
    else:
        servers.pop(key, None)
    save_json(path, data)


def _claude_code_generate(root: Path) -> Path:
    path = _claude_code_config_path(root)
    existing = load_json(path)
    existing_servers = existing.get('mcpServers', {})
    enabled_map = _claude_code_read_enabled(root)

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


# ── Claude Desktop ──────────────────────────────────────────────────────────

def _claude_desktop_config_path() -> Path:
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'Claude' / 'claude_desktop_config.json'
    elif sys.platform == 'win32':
        return Path(os.environ.get('APPDATA', '')) / 'Claude' / 'claude_desktop_config.json'
    else:
        return Path.home() / '.config' / 'Claude' / 'claude_desktop_config.json'


def _claude_desktop_read_enabled(root: Path) -> dict[str, bool]:
    path = _claude_desktop_config_path()
    if not path.exists():
        return {}
    data = load_json(path)
    return {k: True for k in data.get('mcpServers', {})}


def _claude_desktop_write_enabled(root: Path, key: str, enabled: bool) -> None:
    path = _claude_desktop_config_path()
    if not path.exists():
        return
    data = load_json(path)
    servers = data.setdefault('mcpServers', {})
    if enabled:
        if key not in servers:
            servers[key] = {'command': '', 'args': []}
    else:
        servers.pop(key, None)
    save_json(path, data)


def _claude_desktop_generate(root: Path) -> Path | None:
    path = _claude_desktop_config_path()
    if not path.exists():
        return None

    data = load_json(path)
    existing_servers = data.get('mcpServers', {})
    enabled_map = _claude_desktop_read_enabled(root)

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

    data['mcpServers'] = existing_servers
    save_json(path, data)
    return path


# ── Codex ───────────────────────────────────────────────────────────────────

def _toml_escape(s: str) -> str:
    return s.replace('\\', '\\\\').replace('"', '\\"')


def _codex_user_path() -> Path:
    return Path.home() / '.codex' / 'config.toml'


def _codex_project_path(root: Path) -> Path:
    return root / '.codex' / 'config.toml'


def _codex_read_enabled_from_file(path: Path) -> dict[str, bool]:
    if not path.exists():
        return {}
    result = {}
    current_server = None
    for line in path.read_text(encoding='utf-8').splitlines():
        stripped = line.strip()
        if stripped.startswith('[mcp_servers.'):
            # Extract server name: [mcp_servers.foo] → foo
            current_server = stripped[len('[mcp_servers.'):].rstrip(']').split('.')[0]
        elif current_server and stripped.startswith('enabled'):
            val = stripped.split('=', 1)[1].strip().lower()
            result[current_server] = val == 'true'
            current_server = None
    return result


def _codex_read_enabled(root: Path) -> dict[str, bool]:
    merged = {}
    merged.update(_codex_read_enabled_from_file(_codex_user_path()))
    merged.update(_codex_read_enabled_from_file(_codex_project_path(root)))
    return merged


def _codex_write_enabled_to_file(path: Path, key: str, enabled: bool) -> None:
    if not path.exists():
        return
    lines = path.read_text(encoding='utf-8').splitlines()
    in_target = False
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped == f'[mcp_servers.{key}]':
            in_target = True
            new_lines.append(line)
        elif in_target and stripped.startswith('['):
            # Entered next section — inject enabled if missing
            new_lines.append(f'enabled = {"true" if enabled else "false"}')
            in_target = False
            new_lines.append(line)
        elif in_target and stripped.startswith('enabled'):
            new_lines.append(f'enabled = {"true" if enabled else "false"}')
            in_target = False
        else:
            new_lines.append(line)
    if in_target:
        new_lines.append(f'enabled = {"true" if enabled else "false"}')
    path.write_text('\n'.join(new_lines) + '\n', encoding='utf-8')


def _codex_write_enabled(root: Path, key: str, enabled: bool) -> None:
    _codex_write_enabled_to_file(_codex_user_path(), key, enabled)
    _codex_write_enabled_to_file(_codex_project_path(root), key, enabled)


def _write_codex_toml(path: Path, servers: list[dict], enabled_map: dict[str, bool]) -> None:
    existing_content = ''
    if path.exists():
        existing_content = path.read_text(encoding='utf-8')

    # Find MCP section boundaries
    mcp_start = None
    mcp_end = None
    in_mcp = False
    for i, line in enumerate(existing_content.splitlines()):
        stripped = line.strip()
        if stripped.startswith('[mcp_servers'):
            if mcp_start is None:
                mcp_start = i
            in_mcp = True
        elif in_mcp and stripped.startswith('[') and not stripped.startswith('[mcp_servers'):
            mcp_end = i
            in_mcp = False
    if in_mcp:
        mcp_end = len(existing_content.splitlines())

    pre_lines = existing_content.splitlines()[:mcp_start] if mcp_start is not None else []
    post_lines = existing_content.splitlines()[mcp_end:] if mcp_end is not None else []

    with open(path, 'w', encoding='utf-8') as f:
        for line in pre_lines:
            f.write(line + '\n')

        for srv in servers:
            key = srv['key']
            enabled = enabled_map.get(key, True)
            python_bin = srv['command'][0]
            server_script = srv['command'][1]
            f.write(f'[mcp_servers.{key}]\n')
            f.write(f'command = "{_toml_escape(python_bin)}"\n')
            f.write(f'args = ["{_toml_escape(server_script)}"]\n')
            f.write(f'enabled = {"true" if enabled else "false"}\n')
            if srv['env']:
                f.write(f'\n[mcp_servers.{key}.env]\n')
                for k, v in srv['env'].items():
                    f.write(f'{k} = "{_toml_escape(str(v))}"\n')
            f.write('\n')

        for line in post_lines:
            f.write(line + '\n')


def _codex_generate_user(root: Path) -> Path:
    codex_dir = Path.home() / '.codex'
    codex_dir.mkdir(parents=True, exist_ok=True)
    path = codex_dir / 'config.toml'
    _write_codex_toml(path, discover_server_definitions(root), _codex_read_enabled(root))
    return path


def _codex_generate_project(root: Path) -> Path:
    codex_dir = root / '.codex'
    codex_dir.mkdir(parents=True, exist_ok=True)
    path = codex_dir / 'config.toml'
    _write_codex_toml(path, discover_server_definitions(root), _codex_read_enabled(root))
    return path


# ── Hermes ──────────────────────────────────────────────────────────────────

def _hermes_config_path() -> Path:
    return Path.home() / '.hermes' / 'config.yaml'


def _hermes_read_enabled(root: Path) -> dict[str, bool]:
    path = _hermes_config_path()
    if not path.exists():
        return {}
    data = load_yaml(path)
    result = {}
    for name, cfg in data.get('mcp_servers', {}).items():
        if isinstance(cfg, dict):
            result[name] = cfg.get('enabled', True)
        else:
            result[name] = True
    return result


def _hermes_write_enabled(root: Path, key: str, enabled: bool) -> None:
    path = _hermes_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = load_yaml(path) if path.exists() else {}
    mcp = data.setdefault('mcp_servers', {})
    if key in mcp and isinstance(mcp[key], dict):
        mcp[key]['enabled'] = enabled
    save_yaml(path, data)


def _hermes_generate(root: Path) -> Path:
    path = _hermes_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = load_yaml(path) if path.exists() else {}
    mcp_servers = data.get('mcp_servers', {})
    enabled_map = _hermes_read_enabled(root)

    for srv in discover_server_definitions(root):
        key = srv['key']
        enabled = enabled_map.get(key, True)
        python_bin = srv['command'][0]
        server_script = srv['command'][1]
        entry: dict = {
            'command': python_bin,
            'args': [server_script],
            'enabled': enabled,
        }
        if srv['env']:
            entry['env'] = srv['env']
        mcp_servers[key] = entry

    data['mcp_servers'] = mcp_servers
    save_yaml(path, data)
    return path


# ── OpenClaw ────────────────────────────────────────────────────────────────

def _openclaw_config_path() -> Path:
    return Path.home() / '.openclaw' / 'openclaw.json'


def _openclaw_read_enabled(root: Path) -> dict[str, bool]:
    path = _openclaw_config_path()
    if not path.exists():
        return {}
    data = load_json(path)
    result = {}
    for name, cfg in data.get('mcp', {}).get('servers', {}).items():
        if isinstance(cfg, dict):
            result[name] = cfg.get('enabled', True)
        else:
            result[name] = True
    return result


def _openclaw_write_enabled(root: Path, key: str, enabled: bool) -> None:
    path = _openclaw_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = load_json(path) if path.exists() else {}
    mcp = data.setdefault('mcp', {})
    servers = mcp.setdefault('servers', {})
    if key in servers and isinstance(servers[key], dict):
        servers[key]['enabled'] = enabled
    save_json(path, data)


def _openclaw_generate(root: Path) -> Path:
    path = _openclaw_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = load_json(path) if path.exists() else {}
    mcp = data.setdefault('mcp', {})
    servers = mcp.setdefault('servers', {})
    enabled_map = _openclaw_read_enabled(root)

    for srv in discover_server_definitions(root):
        key = srv['key']
        enabled = enabled_map.get(key, True)
        python_bin = srv['command'][0]
        server_script = srv['command'][1]
        entry: dict = {
            'command': python_bin,
            'args': [server_script],
            'enabled': enabled,
        }
        if srv['env']:
            entry['env'] = srv['env']
        servers[key] = entry

    save_json(path, data)
    return path


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ══════════════════════════════════════════════════════════════════════════════

class BackendAdapter:
    '''Unified interface for backend-specific MCP config operations.'''
    def __init__(self, name: str, read_enabled, write_enabled, generate):
        self.name = name
        self._read_enabled = read_enabled
        self._write_enabled = write_enabled
        self._generate = generate

    def read_enabled(self, root: Path) -> dict[str, bool]:
        '''Read current enable/disable state from native config.'''
        return self._read_enabled(root)

    def write_enabled(self, root: Path, key: str, enabled: bool) -> None:
        '''Write enable/disable state to native config.'''
        self._write_enabled(root, key, enabled)

    def generate(self, root: Path) -> Path | None:
        '''Regenerate native config from definitions + current state.'''
        return self._generate(root)


# ── None (no-op) ─────────────────────────────────────────────────────────────

def _none_noop_read(root: Path) -> dict[str, bool]:  # noqa: ARG001
    return {}


def _none_noop_write(root: Path, key: str, enabled: bool) -> None:  # noqa: ARG001
    pass


def _none_noop_generate(root: Path) -> Path | None:  # noqa: ARG001
    return None


ADAPTERS: dict[str, BackendAdapter] = {
    'opencode': BackendAdapter('opencode', _opencode_read_enabled, _opencode_write_enabled, _opencode_generate),
    'claude': BackendAdapter('claude', _claude_code_read_enabled, _claude_code_write_enabled, _claude_code_generate),
    'codex': BackendAdapter('codex', _codex_read_enabled, _codex_write_enabled, lambda r: _codex_generate_user(r)),
    'agy': BackendAdapter('agy', _claude_code_read_enabled, _claude_code_write_enabled, _claude_code_generate),
    'hermes': BackendAdapter('hermes', _hermes_read_enabled, _hermes_write_enabled, _hermes_generate),
    'openclaw': BackendAdapter('openclaw', _openclaw_read_enabled, _openclaw_write_enabled, _openclaw_generate),
    'none': BackendAdapter('none', _none_noop_read, _none_noop_write, _none_noop_generate),
}


def get_adapter(backend: str) -> BackendAdapter | None:
    return ADAPTERS.get(backend)


def read_enabled(root: Path, backend: str) -> dict[str, bool]:
    '''Read enable/disable state from the specified backend's native config.'''
    adapter = ADAPTERS.get(backend)
    if not adapter:
        return {}
    return adapter.read_enabled(root)


def write_enabled(root: Path, backend: str, key: str, enabled: bool) -> None:
    '''Write enable/disable state to the specified backend's native config.'''
    adapter = ADAPTERS.get(backend)
    if adapter:
        adapter.write_enabled(root, key, enabled)


def generate_for_backend(root: Path, backend: str) -> Path | None:
    '''Regenerate native config for the specified backend.'''
    adapter = ADAPTERS.get(backend)
    if not adapter:
        return None
    return adapter.generate(root)


def generate_all(root: Path) -> dict[str, Path | None]:
    '''Regenerate native config for all backends.'''
    return {name: adapter.generate(root) for name, adapter in ADAPTERS.items()}
