'''Agents binder for Podarcis MCP Gateway.

Exposes subagent personas from .agents/agents/*.md as MCP Prompts, Resources, and Delegation tools.
'''
from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

_REGISTERED_AGENTS: dict[str, str] = {}

def register(mcp, root: Path, enabled_agents: set[str] | None = None) -> None:
    '''Discover and register enabled subagent personas as MCP prompts, resources, and delegation tools.'''
    agents_dir = root / '.agents' / 'agents'
    if not agents_dir.exists():
        return

    from components import is_agent_enabled, get_agent_desc

    _REGISTERED_AGENTS.clear()

    for agent_file in sorted(agents_dir.glob('*.md')):
        agent_name = agent_file.stem
        if enabled_agents is not None and agent_name not in enabled_agents:
            continue

        if not is_agent_enabled(agent_file):
            continue

        content = agent_file.read_text(encoding='utf-8')
        _REGISTERED_AGENTS[agent_name] = content

        # 1. Register Resource: podarcis://agents/<name>.md
        resource_uri = f'podarcis://agents/{agent_name}.md'

        def _make_resource_fn(text: str, name: str):
            def resource_fn() -> str:
                return text
            resource_fn.__doc__ = f"Subagent persona definition for {name}"
            return resource_fn

        mcp.add_resource(_make_resource_fn(content, agent_name), resource_uri)

        # 2. Register Prompt: podarcis_agent_<name>
        prompt_name = f'podarcis_agent_{agent_name.replace("-", "_")}'

        def _make_prompt_fn(text: str, name: str):
            def prompt_fn() -> str:
                return text
            prompt_fn.__doc__ = f"System prompt for subagent {name}"
            return prompt_fn

        try:
            mcp.add_prompt(_make_prompt_fn(content, agent_name), prompt_name)
        except Exception:
            pass

    # 3. Register Delegation Tool: podarcis_delegate_task
    @mcp.tool(name='podarcis_delegate_task')
    def delegate_task(
        agent: Annotated[str, "Target subagent persona name (e.g. 'researcher', 'synthesizer', 'protocol_architect', 'auditor')"],
        task: Annotated[str, "Clear, specific task prompt to delegate to the subagent"],
    ) -> str:
        '''Delegate a sub-task to an active Podarcis subagent persona.'''
        if agent not in _REGISTERED_AGENTS:
            available = ", ".join(sorted(_REGISTERED_AGENTS.keys()))
            return f"Error: Agent '{agent}' is not available or disabled. Active agents: {available}"

        persona_prompt = _REGISTERED_AGENTS[agent]
        return (
            f"=== DELEGATED TASK TO AGENT [{agent}] ===\n"
            f"Persona Context Loaded ({len(persona_prompt)} chars).\n"
            f"Task: {task}\n\n"
            f"System Prompt:\n{persona_prompt[:500]}...\n"
        )

def unregister(mcp) -> None:
    '''Unregister agent delegation tools.'''
    _REGISTERED_AGENTS.clear()
    try:
        mcp.remove_tool('podarcis_delegate_task')
    except Exception:
        pass
