'''Unit tests for harness MCP configuration adapters.'''

import json
from pathlib import Path
from harnesses import (
    ADAPTERS,
    HARNESS_ADAPTERS,
    BaseAdapter,
    OpenCodeAdapter,
    ClaudeCodeAdapter,
    AgyAdapter,
    CodexAdapter,
    HermesAdapter,
    OpenClawAdapter,
    NoneAdapter,
    get_adapter,
    read_enabled,
    write_enabled,
    generate_for_harness,
    generate_all,
)


def test_harness_adapter_registration():
    '''Verify all expected harnesses are registered in ADAPTERS dictionary.'''
    expected = {'opencode', 'claude', 'codex', 'agy', 'hermes', 'openclaw', 'none'}
    assert set(ADAPTERS.keys()) == expected
    assert set(HARNESS_ADAPTERS.keys()) == expected

    for name in expected:
        adapter = get_adapter(name)
        assert adapter is not None
        assert isinstance(adapter, BaseAdapter)


def test_opencode_adapter(tmp_path):
    '''Test OpenCodeAdapter read, write, and generation capabilities.'''
    adapter = OpenCodeAdapter()

    # Pre-create opencode.json
    opencode_path = tmp_path / 'opencode.json'
    opencode_path.write_text(
        json.dumps({'mcp': {'test-mcp': {'enabled': True}}}), encoding='utf-8'
    )

    assert adapter.read_enabled(tmp_path) == {'test-mcp': True}

    adapter.write_enabled(tmp_path, 'test-mcp', False)
    assert adapter.read_enabled(tmp_path) == {'test-mcp': False}


def test_claude_code_adapter(tmp_path):
    '''Test ClaudeCodeAdapter read, write, and generate logic.'''
    adapter = ClaudeCodeAdapter()
    mcp_path = tmp_path / '.mcp.json'
    mcp_path.write_text(
        json.dumps({'mcpServers': {'server-a': {'command': 'py', 'args': []}}}),
        encoding='utf-8',
    )

    assert adapter.read_enabled(tmp_path) == {'server-a': True}

    adapter.write_enabled(tmp_path, 'server-a', False)
    assert adapter.read_enabled(tmp_path) == {}

    adapter.write_enabled(tmp_path, 'server-b', True)
    assert adapter.read_enabled(tmp_path) == {'server-b': True}


def test_agy_adapter_inheritance():
    '''Verify AgyAdapter inherits behavior from ClaudeCodeAdapter.'''
    adapter = AgyAdapter()
    assert isinstance(adapter, ClaudeCodeAdapter)
    assert adapter.name == 'agy'


def test_none_adapter(tmp_path):
    '''Verify NoneAdapter no-op behavior.'''
    adapter = NoneAdapter()
    assert adapter.read_enabled(tmp_path) == {}
    assert adapter.generate(tmp_path) is None

    # Ensure write_enabled performs no side effects
    adapter.write_enabled(tmp_path, 'key', True)


def test_global_helper_functions(tmp_path):
    '''Test top-level helper functions read_enabled, write_enabled, generate_all.'''
    # Non-existent harness returns empty dict / None
    assert read_enabled(tmp_path, 'invalid_harness') == {}
    assert generate_for_harness(tmp_path, 'invalid_harness') is None

    res = generate_all(tmp_path)
    assert 'opencode' in res
    assert 'claude' in res
    assert 'none' in res
    assert res['none'] is None
