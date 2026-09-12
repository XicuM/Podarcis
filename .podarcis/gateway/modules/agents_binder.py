'''Agents binder for Podarcis MCP Gateway.

Exposes subagent personas from .apm/agents/*.agent.md as MCP Resources.

Personas are delivered to agents by their harness, natively and with real context
isolation — Claude Code reads .claude/agents/, OpenCode reads .opencode/agents/,
and `apm install` deploys this same .apm/agents/ source into both. The resource below is a
read-only fallback for clients that have no native subagent mechanism; it is not
the primary path and costs nothing until something reads it.

Deliberately NOT registered here:

  * A prompt per persona. It returned byte-identical content to the resource, so
    it was a second transport for the same static file.

  * A delegation tool. An MCP tool cannot spawn an isolated subagent process, so
    the old agent_delegate could only return the persona text with instructions
    to adopt it for the rest of the current task — folding a 4KB system prompt
    into the caller's context, which is the opposite of what delegation is for.
    Its own response told callers to use the native Agent tool instead.

Per-instance workflow differences (notably sources_backend: gdrive vs local) are
NOT decided here and never were. Each persona reads .podarcis/config.yaml at
runtime and selects the matching skill — see the "Active Skill Check" table in
synthesizer.md. This binder serves the same static markdown to every instance.
'''
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger('podarcis.gateway.agents')


def register(mcp, root: Path, config_section: dict | None = None) -> list[str]:
    '''Register every shipped persona as an MCP resource; return what was bound.

    `config_section` is the `agents:` block of config.yaml, an exclusion list —
    a persona absent from it is enabled.
    '''
    agents_dir = root / '.apm' / 'agents'
    if not agents_dir.exists():
        return []

    from podarcis.components import is_agent_enabled
    from podarcis.gateway.router import is_enabled

    bound = []
    for agent_file in sorted(agents_dir.glob('*.agent.md')):
        # APM's primitive suffix: researcher.agent.md is the persona `researcher`,
        # and `apm install` strips .agent on the way into .claude/agents/.
        agent_name = agent_file.name.removesuffix('.agent.md')
        if not is_enabled(config_section, agent_name):
            continue

        if not is_agent_enabled(agent_file):
            continue

        content = agent_file.read_text(encoding='utf-8')

        def _make_resource_fn(text: str, name: str):
            def resource_fn() -> str:
                return text
            resource_fn.__doc__ = f"Subagent persona definition for {name}"
            return resource_fn

        try:
            mcp.resource(f'podarcis://agents/{agent_name}.md')(
                _make_resource_fn(content, agent_name)
            )
            bound.append(agent_name)
        except Exception as exc:
            # Never silently: a persona that fails to bind is invisible to every
            # client with no native subagent mechanism.
            logger.error('Failed to bind persona %s: %s', agent_name, exc)

    return bound
