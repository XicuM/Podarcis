'''Agents binder for Podarcis MCP Gateway.

Exposes subagent personas from .agents/agents/*.md as MCP Resources.

Personas are delivered to agents by their harness, natively and with real context
isolation — Claude Code reads .claude/agents/, OpenCode reads .opencode/agents/,
and both resolve to this same .agents/agents/ directory. The resource below is a
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

from pathlib import Path


def register(mcp, root: Path, enabled_agents: set[str] | None = None) -> None:
    '''Discover and register enabled subagent personas as MCP resources.'''
    agents_dir = root / '.agents' / 'agents'
    if not agents_dir.exists():
        return

    from components import is_agent_enabled

    for agent_file in sorted(agents_dir.glob('*.md')):
        agent_name = agent_file.stem
        if enabled_agents is not None and agent_name not in enabled_agents:
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
        except Exception:
            pass
