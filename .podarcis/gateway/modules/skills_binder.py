'''Skills binder for Podarcis MCP Gateway.

Exposes enabled skills from .apm/skills/ as MCP Resources.

Like personas, skills are loaded by the harness natively — Claude Code reads
.claude/skills/, OpenCode reads .opencode/skills/, and `apm install` deploys this
same .apm/skills/ source into both. The resource below is a read-only fallback for clients
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

import logging
from pathlib import Path

logger = logging.getLogger('podarcis.gateway.skills')


def register(mcp, root: Path, config_section: dict | None = None) -> list[str]:
    '''Register every shipped skill as an MCP resource; return what was bound.

    `config_section` is the `skills:` block of config.yaml, an exclusion list —
    a skill absent from it is enabled.
    '''
    skills_dir = root / '.apm' / 'skills'
    if not skills_dir.exists():
        return []

    from podarcis.components import is_skill_enabled
    from podarcis.gateway.router import is_enabled

    bound = []
    for skill_path in sorted(skills_dir.iterdir()):
        if not skill_path.is_dir():
            continue

        skill_name = skill_path.name
        if not is_enabled(config_section, skill_name):
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
            bound.append(skill_name)
        except Exception as exc:
            # Never silently: a skill that fails to bind is invisible to every
            # client with no native skill mechanism.
            logger.error('Failed to bind skill %s: %s', skill_name, exc)

    return bound
