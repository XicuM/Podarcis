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

        try:
            mcp.resource(resource_uri)(_make_resource_fn(content, agent_name))
        except Exception:
            pass

        # 2. Register Prompt: agent_<name>
        prompt_name = f'agent_{agent_name.replace("-", "_")}'

        def _make_prompt_fn(text: str, name: str):
            def prompt_fn() -> str:
                return text
            prompt_fn.__doc__ = f"System prompt for subagent {name}"
            return prompt_fn

        try:
            mcp.prompt(name=prompt_name)(_make_prompt_fn(content, agent_name))
        except Exception:
            pass

    # 3. Register Delegation Tool: agent_delegate
    @mcp.tool(name='agent_delegate')
    def delegate_task(
        agent: Annotated[str, "Target subagent persona name (e.g. 'researcher', 'synthesizer', 'protocol-architect', 'auditor')"],
        task: Annotated[str, "Clear, specific task prompt to delegate to the subagent"],
    ) -> str:
        '''Delegate a sub-task to an active Podarcis subagent persona.'''
        if agent not in _REGISTERED_AGENTS:
            available = ", ".join(sorted(_REGISTERED_AGENTS.keys()))
            return f"Error: Agent '{agent}' is not available or disabled. Active agents: {available}"

        persona_prompt = _REGISTERED_AGENTS[agent]
        return (
            f"=== DELEGATED TASK: ADOPT PERSONA [{agent}] ===\n"
            f"This MCP tool cannot spawn an isolated subagent process itself. "
            f"To execute this delegation, adopt the persona below as your operating "
            f"instructions for the remainder of this task, then carry out the task.\n\n"
            f"Task: {task}\n\n"
            f"=== Persona System Prompt ({len(persona_prompt)} chars) ===\n{persona_prompt}\n"
            f"=== End Persona System Prompt ===\n\n"
            f"Note: on Claude Code, prefer the native Agent tool with "
            f"subagent_type: \"{agent}\" instead of this tool — it runs the persona "
            f"in an isolated context rather than folding it into the current one."
        )

def unregister(mcp) -> None:
    '''Unregister agent delegation tools.'''
    _REGISTERED_AGENTS.clear()
    try:
        mcp.remove_tool('agent_delegate')
    except Exception:
        pass
