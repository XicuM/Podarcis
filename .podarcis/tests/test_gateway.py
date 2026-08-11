'''Unit tests for Podarcis Gateway server, router, and backends integration.'''

import pytest
import asyncio
from pathlib import Path
from podarcis.gateway.server import create_gateway
from backends import discover_server_definitions, generate_all

def test_discover_server_definitions():
    root = Path('.').resolve()
    defs = discover_server_definitions(root)
    assert len(defs) == 1
    assert defs[0]['key'] == 'podarcis'
    assert 'podarcis-mcp' in defs[0]['command'][0]

def test_generate_all_backends():
    root = Path('.').resolve()
    results = generate_all(root)
    assert 'opencode' in results
    assert 'claude' in results

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
