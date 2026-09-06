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
        tool_names = [t.name for t in await mcp.list_tools()]
        assert 'wiki_search' in tool_names
        assert 'literature_search' in tool_names
        assert 'diagnostics_log' in tool_names

    asyncio.run(run())


def test_personas_exposed_as_resources_only():
    '''Personas reach agents through their harness's native subagent mechanism
    (.claude/agents, .opencode/agents — both this same directory), which gives
    real context isolation. The MCP resource is a read-only fallback for clients
    without one. No delegation tool: MCP cannot spawn an isolated process, so
    such a tool could only inline the persona into the caller's own context.'''
    async def run():
        root = Path('.').resolve()
        mcp, watcher = create_gateway(root)

        tool_names = [t.name for t in await mcp.list_tools()]
        assert 'agent_delegate' not in tool_names

        prompt_names = [p.name for p in await mcp.list_prompts()]
        assert not [p for p in prompt_names if p.startswith('agent_')]

        uris = {str(r.uri) for r in await mcp.list_resources()}
        assert 'podarcis://agents/synthesizer.md' in uris
        assert 'podarcis://agents/researcher.md' in uris

    asyncio.run(run())


def test_persona_content_is_backend_agnostic():
    '''sources_backend (gdrive vs local) must NOT be resolved at bind time. Each
    persona reads .podarcis/config.yaml at runtime and picks the matching skill,
    so one static persona serves every instance. If the binder ever specialised
    per backend, a gdrive instance would silently receive local instructions.'''
    persona = (Path('.').resolve() / '.agents' / 'agents' / 'synthesizer.md').read_text()
    assert 'sources_backend' in persona
    assert 'synthesizer-gdrive' in persona
    assert 'synthesizer-local' in persona


@pytest.mark.parametrize('backend_skill', ['synthesizer-local', 'synthesizer-gdrive'])
def test_both_synthesizer_backends_are_reachable(backend_skill):
    '''Naming a skill in the persona is not enough — it must actually resolve.

    The gdrive skill previously carried disable-model-invocation/disabled flags
    while the persona called gdrive the default backend, so a gdrive instance was
    routed to a skill both the harness and the gateway refused to serve. Assert
    the file exists, is not frontmatter-gated, and is enabled by default.
    '''
    from components import is_skill_enabled
    from podarcis.gateway.router import DEFAULT_SKILLS

    skill_path = Path('.').resolve() / '.agents' / 'skills' / backend_skill
    assert (skill_path / 'SKILL.md').exists()
    assert is_skill_enabled(skill_path), f'{backend_skill} is gated by frontmatter'
    assert DEFAULT_SKILLS.get(backend_skill, {}).get('enabled') is True


def test_skills_exposed_as_resources_only():
    '''Skills reach agents through the harness (.claude/skills, .opencode/skills —
    both this same directory). The MCP resource is a read-only fallback. No prompt:
    it returned byte-identical content, making it a third copy of one static file.'''
    async def run():
        mcp, watcher = create_gateway(Path('.').resolve())

        prompt_names = [p.name for p in await mcp.list_prompts()]
        assert not [p for p in prompt_names if p.startswith('skill_')]

        uris = {str(r.uri) for r in await mcp.list_resources()}
        assert 'podarcis://skills/synthesizer-gdrive' in uris

    asyncio.run(run())


def test_no_tool_duplicates_a_native_harness_capability():
    '''Directory listings and file reads are Glob/Read. Guard the regression:
    wiki://collections/* was rglob("*.md") reformatted as a markdown list.'''
    async def run():
        mcp, watcher = create_gateway(Path('.').resolve())
        uris = {str(r.uri) for r in await mcp.list_resources()}
        assert not [u for u in uris if u.startswith('wiki://collections/')]

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
    # Every skill shipped on disk must be enabled by default. Asserted against the
    # directory rather than a hardcoded set, so adding a skill without registering
    # it in DEFAULT_SKILLS fails here instead of silently never binding.
    on_disk = {d.name for d in (Path('.').resolve() / '.agents' / 'skills').iterdir()
               if (d / 'SKILL.md').exists()}
    assert set(cfg['skills']) == on_disk
    assert all(v.get('enabled', True) for v in cfg['skills'].values())
    assert set(cfg['mcp_modules']) == {'wiki', 'research', 'repo', 'menumaker', 'diagnostics'}
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

