'''Unit tests for Podarcis Gateway server and router integration.'''

import sys
import pytest
import asyncio
from pathlib import Path
from podarcis.gateway.server import create_gateway
from podarcis.gateway.router import (
    discover_modules, load_gateway_config, load_server_mcp,
)
from podarcis.common import save_yaml

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
    (.claude/agents, .opencode/agents — both deployed from .apm/agents), which gives
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
    persona = (Path('.').resolve() / '.apm' / 'agents' / 'synthesizer.agent.md').read_text()
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
    from podarcis.components import is_skill_enabled
    from podarcis.gateway.router import is_enabled

    skill_path = Path('.').resolve() / '.apm' / 'skills' / backend_skill
    assert (skill_path / 'SKILL.md').exists()
    assert is_skill_enabled(skill_path), f'{backend_skill} is gated by frontmatter'
    assert is_enabled({}, backend_skill), f'{backend_skill} is disabled by default' 


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


def test_everything_shipped_binds_without_config(tmp_path):
    '''A config.yaml with no gating sections must still bind everything on disk.

    Discovery is the only registry: there is no DEFAULT_* dict to add a new
    module, skill, or persona to, so "shipped but never bound" cannot happen.
    '''
    root = Path('.').resolve()
    cfg_dir = tmp_path / '.podarcis'
    cfg_dir.mkdir()
    save_yaml(cfg_dir / 'config.yaml', {
        'repositories': {'wiki': 'git@example.com/wiki.git'},
        'sources_backend': 'local',
    })
    cfg = load_gateway_config(tmp_path)
    assert all(cfg[s] == {} for s in ('mcp_modules', 'skills', 'agents'))
    # Non-gateway keys are carried through unchanged.
    assert cfg['repositories']['wiki'] == 'git@example.com/wiki.git'

    async def run():
        mcp, watcher = create_gateway(root, cfg_dir / 'config.yaml')
        uris = {str(r.uri) for r in await mcp.list_resources()}

        # .apm/ holds what this repo authors. APM deploys third-party skills into
        # .agents/skills/ and .claude/skills/ alongside them; the gateway binds only
        # what is first-party, so read the source tree, not a deploy target.
        on_disk_skills = {d.name for d in (root / '.apm' / 'skills').iterdir()
                          if (d / 'SKILL.md').exists()}
        assert {f'podarcis://skills/{n}' for n in on_disk_skills} <= uris

        on_disk_agents = {f.name.removesuffix('.agent.md')
                          for f in (root / '.apm' / 'agents').glob('*.agent.md')}
        assert {f'podarcis://agents/{n}.md' for n in on_disk_agents} <= uris

        # Every tool every shipped module defines is bound.
        bound = {t.name for t in await mcp.list_tools()}
        for name, path in discover_modules(root).items():
            own = set(load_server_mcp(root, path)._tool_manager._tools)
            assert own, f'{name} defines no tools'
            assert own <= bound, f'{name} bound none of {sorted(own - bound)}'

    asyncio.run(run())


def test_discovery_is_the_only_module_registry():
    '''`podarcis status` and the gateway must agree on what exists.

    They previously disagreed: the router bound a hardcoded MODULE_PATHS while
    components globbed the directory, so a new module listed as "disabled"
    forever.
    '''
    from podarcis.components import discover_components, get_enabled_mcp_servers

    root = Path('.').resolve()
    globbed = {f'{n}-mcp' for n in discover_modules(root)}
    listed, _, _ = discover_components(root)
    assert set(listed) == globbed
    assert globbed <= get_enabled_mcp_servers(root)


def test_explicit_disable_overrides_discovery(tmp_path):
    '''An explicit disable in config.yaml wins over "present on disk".'''
    from podarcis.gateway.router import is_enabled

    cfg_dir = tmp_path / '.podarcis'
    cfg_dir.mkdir()
    save_yaml(cfg_dir / 'config.yaml', {
        'agents': {'auditor': {'enabled': False}},
        'skills': {'self-improvement': False},
    })
    cfg = load_gateway_config(tmp_path)
    assert is_enabled(cfg['agents'], 'auditor') is False
    assert is_enabled(cfg['agents'], 'researcher') is True
    assert is_enabled(cfg['skills'], 'self-improvement') is False

    async def run():
        mcp, watcher = create_gateway(Path('.').resolve(), cfg_dir / 'config.yaml')
        uris = {str(r.uri) for r in await mcp.list_resources()}
        assert 'podarcis://agents/auditor.md' not in uris
        assert 'podarcis://agents/researcher.md' in uris
        assert 'podarcis://skills/self-improvement' not in uris

    asyncio.run(run())


# Rules that live in AGENTS.md §3-4 and were previously restated verbatim inside
# personas. AGENTS.md is symlinked as CLAUDE.md and loads into subagent context
# automatically (verified empirically), so a persona copy is dead weight that
# drifts — `agent_delegate` and `gemini-3.6-flash` both survived in persona
# bodies long after the thing they described was gone.
_SHARED_RULES = [
    'diagnostics_log',
    'No Manual Line Wrapping',
    'Surgical Edits',
    'Snake_case filenames',
    'No Web Search',
]


@pytest.mark.parametrize('persona', sorted(
    p.name for p in (Path('.').resolve() / '.apm' / 'agents').glob('*.agent.md')))
def test_personas_do_not_restate_shared_conventions(persona):
    body = (Path('.').resolve() / '.apm' / 'agents' / persona).read_text()
    assert 'Shared conventions' in body, (
        f'{persona} must point at AGENTS.md rather than restating it')
    for rule in _SHARED_RULES:
        assert rule not in body, (
            f'{persona} restates the AGENTS.md rule {rule!r}; keep it in one place')


def test_snake_case_convention_has_exactly_one_home():
    '''It lived only in two persona bodies (one spelling it "Filnaming") and was
    absent from AGENTS.md, so the shared rulebook was missing a real convention.'''
    assert 'Snake_case Filenames' in (Path('.').resolve() / 'AGENTS.md').read_text()


def test_mcp_surface_is_exactly_the_bash_less_job_surface():
    '''Every bound tool must be callable by an agent job.

    Jobs are denied Bash, so a tool they cannot reach is one a human could
    have run as `podarcis ...` instead — and an unreachable MCP copy rots
    silently, as repo_sync did (it imported a helper deleted in 0868a00 and
    would have raised ImportError on every pull).
    '''
    from podarcis.jobs.agent import READ_TOOLS, WRITE_TOOLS

    async def run():
        mcp, watcher = create_gateway(Path('.').resolve())
        bound = {t.name for t in await mcp.list_tools()}
        reachable = {
            t.removeprefix('mcp__podarcis__')
            for t in READ_TOOLS + WRITE_TOOLS if t.startswith('mcp__podarcis__')
        }
        assert bound == reachable, (
            f'bound but unreachable by any job: {sorted(bound - reachable)}; '
            f'allow-listed but not bound: {sorted(reachable - bound)}'
        )

    asyncio.run(run())
