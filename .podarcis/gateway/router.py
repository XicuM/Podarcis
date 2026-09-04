'''Dynamic tool, prompt, and resource router for Podarcis Gateway.

Reads .podarcis/config.yaml and dynamically binds enabled internal modules,
skills, and agent personas onto the FastMCP server instance.
'''
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

from common import load_yaml
from podarcis.gateway.modules import skills_binder, agents_binder

logger = logging.getLogger('podarcis.gateway.router')

MODULE_PATHS = {
    'wiki': '.agents/mcp/wiki/server.py',
    'research': '.agents/mcp/research/server.py',
    'menumaker': '.agents/mcp/menumaker/server.py',
    'finance': '.agents/mcp/finance/server.py',
    'diagnostics': '.agents/mcp/diagnostics/server.py',
}

_CURRENT_BOUND_TOOLS: dict[str, set[str]] = {}
_CURRENT_BOUND_RESOURCES: dict[str, set[str]] = {}
_CURRENT_BOUND_PROMPTS: dict[str, set[str]] = {}

def load_server_mcp(root: Path, rel_path: str) -> Any | None:
    '''Dynamically load standalone MCP server entrypoint.'''
    p = (root / rel_path).resolve()
    if not p.exists():
        return None

    module_name = f'podarcis_mod_{p.parent.name}'
    try:
        if module_name in sys.modules:
            mod = sys.modules[module_name]
            importlib.reload(mod)
            return getattr(mod, 'mcp', None)

        spec = importlib.util.spec_from_file_location(module_name, p)
        if not spec or not spec.loader:
            return None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod
        spec.loader.exec_module(mod)
        return getattr(mod, 'mcp', None)
    except Exception as e:
        logger.error(f"Error loading MCP module at {rel_path}: {e}")
        return None

# ── Default gateway configuration ────────────────────────────────────────────
# Defaults live here in git-tracked code so every instance gets the same
# baseline. The (git-ignored) .podarcis/config.yaml only *overrides* these
# defaults — e.g. disabling a specific persona — and is never the source of
# truth for which modules/skills/agents are active.

DEFAULT_MCP_MODULES = {
    'wiki': {'enabled': True},
    'research': {'enabled': True},
    'diagnostics': {'enabled': True},
}

DEFAULT_SKILLS = {
    'self-improvement': {'enabled': True},
}

DEFAULT_AGENTS = {
    'researcher': {'enabled': True},
    'synthesizer': {'enabled': True},
    'protocol-architect': {'enabled': True},
    'auditor': {'enabled': True},
}


def _merge_section(defaults: dict, on_disk: Any) -> dict:
    '''Overlay an on-disk config section onto its git-tracked defaults.'''
    section = dict(defaults)
    if isinstance(on_disk, dict):
        section.update(on_disk)
    return section


def load_gateway_config(root: Path, config_path: Path | None = None) -> dict[str, Any]:
    '''Merge .podarcis/config.yaml over git-tracked defaults.

    The three gateway sections (mcp_modules, skills, agents) are always
    returned with defaults filled in, so a fresh instance whose config.yaml
    only holds repositories/engines/etc. still binds every core module, skill,
    and persona. Any other top-level keys (repositories, engines, …)
    are carried through unchanged.
    '''
    path = config_path or (root / '.podarcis' / 'config.yaml')
    on_disk = load_yaml(path)

    merged = {
        'mcp_modules': _merge_section(DEFAULT_MCP_MODULES, on_disk.get('mcp_modules')),
        'skills': _merge_section(DEFAULT_SKILLS, on_disk.get('skills')),
        'agents': _merge_section(DEFAULT_AGENTS, on_disk.get('agents')),
    }
    for key, value in on_disk.items():
        if key not in merged:
            merged[key] = value
    return merged

def sync_gateway(mcp: Any, root: Path, config_path: Path | None = None) -> dict[str, Any]:
    '''Synchronize FastMCP server tools, resources, and prompts with configuration.'''
    cfg = load_gateway_config(root, config_path)
    mcp_cfgs = cfg.get('mcp_modules', {})
    skills_cfgs = cfg.get('skills', {})
    agents_cfgs = cfg.get('agents', {})

    state_changed = False

    # 1. Sync internal capability modules
    for name, rel_path in MODULE_PATHS.items():
        is_enabled = False
        if name in mcp_cfgs:
            mod_val = mcp_cfgs[name]
            if isinstance(mod_val, dict):
                is_enabled = mod_val.get('enabled', True)
            elif isinstance(mod_val, bool):
                is_enabled = mod_val
        elif name in ('wiki', 'research', 'diagnostics'):
            is_enabled = True

        src_mcp = load_server_mcp(root, rel_path)
        if not src_mcp:
            continue

        if is_enabled:
            # Bind tools
            bound_tools = _CURRENT_BOUND_TOOLS.setdefault(name, set())
            for tname, tool in src_mcp._tool_manager._tools.items():
                if tname not in bound_tools:
                    mcp.add_tool(tool.fn, name=tname)
                    bound_tools.add(tname)
                    state_changed = True

            # Bind resources
            bound_res = _CURRENT_BOUND_RESOURCES.setdefault(name, set())
            for uri, res in src_mcp._resource_manager._resources.items():
                if uri not in bound_res:
                    try:
                        mcp.add_resource(res)
                        bound_res.add(uri)
                        state_changed = True
                    except Exception:
                        pass
        else:
            # Unbind tools
            if name in _CURRENT_BOUND_TOOLS:
                for tname in list(_CURRENT_BOUND_TOOLS[name]):
                    try:
                        mcp.remove_tool(tname)
                        state_changed = True
                    except Exception:
                        pass
                del _CURRENT_BOUND_TOOLS[name]

            if name in _CURRENT_BOUND_RESOURCES:
                del _CURRENT_BOUND_RESOURCES[name]

    # 2. Sync skills binder
    enabled_skills = {
        k for k, v in skills_cfgs.items()
        if (v.get('enabled', True) if isinstance(v, dict) else bool(v))
    }
    try:
        skills_binder.register(mcp, root, enabled_skills)
    except Exception as e:
        logger.error(f"Failed to sync skills binder: {e}")

    # 3. Sync agents binder
    enabled_agents = {
        k for k, v in agents_cfgs.items()
        if (v.get('enabled', True) if isinstance(v, dict) else bool(v))
    }
    try:
        agents_binder.register(mcp, root, enabled_agents)
    except Exception as e:
        logger.error(f"Failed to sync agents binder: {e}")

    return {
        'changed': state_changed,
        'enabled_modules': [k for k in _CURRENT_BOUND_TOOLS.keys()],
        'enabled_skills': list(enabled_skills),
        'enabled_agents': list(enabled_agents),
    }
