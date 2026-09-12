'''Dynamic tool, prompt, and resource router for Podarcis Gateway.

Discovers the MCP modules, skills, and agent personas shipped on disk and binds
them onto the FastMCP server instance.

Nothing here holds a list of what ships. The router used to keep two hardcoded
registries — MODULE_PATHS (paths) and DEFAULT_MCP_MODULES (enable flags) —
alongside a third, independent discovery glob in components.discover_components.
Three lists of the same four names drift: a module dropped into .agents/mcp/
appeared in `podarcis status` (globbed) yet bound nowhere (absent from
MODULE_PATHS), and `podarcis config enable` wrote a flag no one read.

The rule that replaces them: whatever is on disk is active unless
.podarcis/config.yaml explicitly disables it.
'''
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

from podarcis.common import load_yaml
from podarcis.gateway.modules import skills_binder, agents_binder

logger = logging.getLogger('podarcis.gateway.router')

# Config sections that gate discovered components, keyed by what they gate.
GATED_SECTIONS = ('mcp_modules', 'skills', 'agents')


def discover_modules(root: Path) -> dict[str, Path]:
    '''Every MCP module shipped under .agents/mcp/, keyed by directory name.

    A module *is* its server.py — the same rule components.discover_components
    applies — so a leftover __pycache__/ or tests/ directory is not a module.
    '''
    return {
        p.parent.name: p
        for p in sorted((root / '.agents' / 'mcp').glob('*/server.py'))
    }


def is_enabled(section: Any, name: str) -> bool:
    '''True unless a config.yaml section explicitly disables `name`.

    Accepts both `{'enabled': bool}` and bare-bool entry forms.
    '''
    if not isinstance(section, dict):
        return True
    val = section.get(name)
    if val is None:
        return True
    return val.get('enabled', True) if isinstance(val, dict) else bool(val)


def load_gateway_config(root: Path, config_path: Path | None = None) -> dict[str, Any]:
    '''Read .podarcis/config.yaml, guaranteeing the three gated sections exist.

    The sections are overrides only — an absent section means "nothing is
    disabled", not "nothing is enabled".
    '''
    cfg = load_yaml(config_path or (root / '.podarcis' / 'config.yaml'))
    return cfg | {s: cfg.get(s) or {} for s in GATED_SECTIONS}


def load_server_mcp(root: Path, path: Path) -> Any | None:
    '''Dynamically load a standalone MCP server entrypoint.'''
    if not path.exists():
        return None

    # Always exec a fresh module. importlib.reload() cannot work here: these
    # names are synthetic and off sys.path, so reload's finder raises "spec not
    # found" and every tool silently unbinds on the watcher's next re-sync.
    module_name = f'podarcis_mod_{path.parent.name}'
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if not spec or not spec.loader:
            return None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod
        spec.loader.exec_module(mod)
        return getattr(mod, 'mcp', None)
    except Exception as e:
        logger.error(f'Error loading MCP module at {path}: {e}')
        return None


def sync_gateway(mcp: Any, root: Path, config_path: Path | None = None) -> dict[str, Any]:
    '''Synchronize FastMCP server tools, resources, and prompts with configuration.'''
    cfg = load_gateway_config(root, config_path)
    mcp_cfgs, skills_cfgs, agents_cfgs = (cfg[s] for s in GATED_SECTIONS)

    state_changed = False
    enabled_modules = []

    # 1. Sync internal capability modules.
    # What is bound is read back off `mcp` rather than tracked in module state:
    # a second gateway in one process starts empty, and cached bookkeeping would
    # tell it everything was already bound, leaving it with no tools at all.
    for name, path in discover_modules(root).items():
        src_mcp = load_server_mcp(root, path)
        if not src_mcp:
            continue

        live_tools = mcp._tool_manager._tools
        live_resources = mcp._resource_manager._resources
        if is_enabled(mcp_cfgs, name):
            enabled_modules.append(name)
            for tname, tool in src_mcp._tool_manager._tools.items():
                if tname not in live_tools:
                    mcp.add_tool(tool.fn, name=tname)
                    state_changed = True
            for uri, res in src_mcp._resource_manager._resources.items():
                if uri not in live_resources:
                    mcp.add_resource(res)
                    state_changed = True
        else:
            for tname in src_mcp._tool_manager._tools:
                if tname in live_tools:
                    mcp.remove_tool(tname)
                    state_changed = True

    # 2 & 3. Sync the skills and agents binders. Each discovers what it ships
    # and consults the matching config section for exclusions.
    bound = {}
    for label, binder, section in (
        ('enabled_skills', skills_binder, skills_cfgs),
        ('enabled_agents', agents_binder, agents_cfgs),
    ):
        try:
            bound[label] = binder.register(mcp, root, section)
        except Exception as e:
            logger.error(f'Failed to sync {label} binder: {e}')
            bound[label] = []

    return {
        'changed': state_changed,
        'enabled_modules': enabled_modules,
        **bound,
    }
