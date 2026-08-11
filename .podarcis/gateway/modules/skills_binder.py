'''Skills binder for Podarcis MCP Gateway.

Exposes enabled skills from .agents/skills/ as MCP Prompts, Resources, and script tools.
'''
from __future__ import annotations

import sys
import importlib.util
from pathlib import Path

_REGISTERED_SKILLS: set[str] = set()

def register(mcp, root: Path, enabled_skills: set[str] | None = None) -> None:
    '''Discover and register enabled skills as MCP resources, prompts, and tools.'''
    skills_dir = root / '.agents' / 'skills'
    if not skills_dir.exists():
        return

    from components import is_skill_enabled

    for skill_path in sorted(skills_dir.iterdir()):
        if not skill_path.is_dir():
            continue

        skill_name = skill_path.name
        if enabled_skills is not None and skill_name not in enabled_skills:
            continue

        if not is_skill_enabled(skill_path):
            continue

        skill_file = skill_path / 'SKILL.md'
        if not skill_file.exists():
            continue

        _REGISTERED_SKILLS.add(skill_name)
        content = skill_file.read_text(encoding='utf-8')

        # 1. Register Resource: podarcis://skills/<name>
        resource_uri = f'podarcis://skills/{skill_name}'
        
        def _make_resource_fn(text: str, name: str):
            def resource_fn() -> str:
                return text
            resource_fn.__doc__ = f"Skill documentation for {name}"
            return resource_fn

        mcp.add_resource(_make_resource_fn(content, skill_name), resource_uri)

        # 2. Register Prompt: podarcis_skill_<name>
        prompt_name = f'podarcis_skill_{skill_name.replace("-", "_")}'

        def _make_prompt_fn(text: str, name: str):
            def prompt_fn() -> str:
                return text
            prompt_fn.__doc__ = f"Skill prompt for {name}"
            return prompt_fn

        try:
            mcp.add_prompt(_make_prompt_fn(content, skill_name), prompt_name)
        except Exception:
            pass

        # 3. Register helper scripts as MCP tools if present under scripts/
        scripts_dir = skill_path / 'scripts'
        if scripts_dir.exists():
            for script_file in scripts_dir.glob('*.py'):
                tool_name = f'podarcis_skill_{skill_name.replace("-", "_")}_{script_file.stem}'
                if str(scripts_dir) not in sys.path:
                    sys.path.insert(0, str(scripts_dir))
                try:
                    spec = importlib.util.spec_from_file_location(script_file.stem, script_file)
                    if spec and spec.loader:
                        mod = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(mod)
                        if hasattr(mod, 'run'):
                            mcp.add_tool(mod.run, name=tool_name)
                except Exception:
                    pass

def unregister(mcp) -> None:
    '''Unregister skill prompts and resources.'''
    _REGISTERED_SKILLS.clear()
