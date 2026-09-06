'''Skills binder for Podarcis MCP Gateway.

Exposes enabled skills from .agents/skills/ as MCP Resources.

Like personas, skills are loaded by the harness natively — Claude Code reads
.claude/skills/, OpenCode reads .opencode/skills/, and both resolve to this same
.agents/skills/ directory. The resource below is a read-only fallback for clients
with no native skill mechanism; it costs nothing until something reads it.

Deliberately NOT registered here:

  * A prompt per skill. It returned byte-identical content to the resource, so it
    was a second transport for the same static file — and a third copy of what the
    harness had already loaded.

  * Tools built from scripts/*.py. That block exec'd every script at gateway
    startup looking for a `run` attribute, under a bare `except: pass`. No shipped
    script defines one, so it registered zero tools while executing arbitrary
    module-level code on every config reload. The scripts are invoked the way they
    were designed to be — as commands, via Bash (see the self-improvement skill).
'''
from __future__ import annotations

from pathlib import Path


def register(mcp, root: Path, enabled_skills: set[str] | None = None) -> None:
    '''Discover and register enabled skills as MCP resources.'''
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

        content = skill_file.read_text(encoding='utf-8')

        def _make_resource_fn(text: str, name: str):
            def resource_fn() -> str:
                return text
            resource_fn.__doc__ = f"Skill documentation for {name}"
            return resource_fn

        try:
            mcp.resource(f'podarcis://skills/{skill_name}')(
                _make_resource_fn(content, skill_name)
            )
        except Exception:
            pass
