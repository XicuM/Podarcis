'''Unit tests for Podarcis Gateway server and router integration.'''

import pytest
import asyncio
from pathlib import Path
from podarcis.gateway.server import create_gateway
from podarcis.gateway.router import load_gateway_config
from common import save_yaml

def test_gateway_dynamic_routing():
    async def run():
        root = Path('.').resolve()
        mcp, watcher = create_gateway(root)
        tools = await mcp.list_tools()
        tool_names = [t.name for t in tools]
        assert 'wiki_search' in tool_names
        assert 'search_literature' in tool_names
        assert 'log_pain_point' in tool_names
        assert 'podarcis_delegate_task' in tool_names

    asyncio.run(run())


def test_default_agents_enabled_without_config(tmp_path):
    '''A fresh instance (config.yaml without agents/skills sections) still
    enables all core personas, skills, and modules from git-tracked defaults.'''
    cfg_dir = tmp_path / '.podarcis'
    cfg_dir.mkdir()
    save_yaml(cfg_dir / 'config.yaml', {
        'repositories': {'wiki': 'git@example.com/wiki.git'},
        'sources_backend': 'local',
    })
    cfg = load_gateway_config(tmp_path)
    assert set(cfg['agents']) == {'researcher', 'synthesizer', 'protocol-architect', 'auditor'}
    assert all(v.get('enabled', True) for v in cfg['agents'].values())
    assert set(cfg['skills']) == {'self-improvement'}
    assert 'wiki' in cfg['mcp_modules']
    # Non-gateway keys are carried through unchanged
    assert cfg['repositories']['wiki'] == 'git@example.com/wiki.git'


def test_config_overrides_default_agents(tmp_path):
    '''An explicit disable in config.yaml wins over the code default.'''
    cfg_dir = tmp_path / '.podarcis'
    cfg_dir.mkdir()
    save_yaml(cfg_dir / 'config.yaml', {
        'agents': {'auditor': {'enabled': False}},
        'skills': {'self-improvement': {'enabled': False}},
    })
    cfg = load_gateway_config(tmp_path)
    assert cfg['agents']['auditor']['enabled'] is False
    assert cfg['agents']['researcher']['enabled'] is True
    assert cfg['skills']['self-improvement']['enabled'] is False

