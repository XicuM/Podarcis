---
name: self-improvement
description: Log session friction to .podarcis/diagnostics/ and fix platform issues when asked. Never write diagnostics into wiki/ or workspace/.
metadata: { "openclaw": { "emoji": "🌱" } }
---

# Skill: Self-Improvement & Platform Diagnosis

## Done when

- Pain points live only in `.podarcis/diagnostics/pain_points.jsonl` and `.podarcis/diagnostics/sessions/`.
- If platform code or docs changed: tests pass, `[project] version` in `pyproject.toml` is bumped (patch / minor / major as appropriate), and a platform PR (if any) was prepared with `prepare_pr.py`.

## Checkpoints

- Diagnostics MUST NOT land in `wiki/` or `workspace/`.
- Platform PRs MUST NOT touch `workspace/`, `sources/`, `wiki/`, or `.env*`. Use:

```bash
python .agents/skills/self-improvement/scripts/prepare_pr.py --title "..." --body "..."
```

(`prepare_pr.py` also scrubs credentials, emails, and local paths from titles/bodies and can mark pain points resolved.)

## Facts

- Inspect: `podarcis diagnose` / `podarcis diagnose --json`, or `python .agents/skills/self-improvement/scripts/diagnose_session.py --transcript <path>`.
- Categories: `command_failure`, `execution_error`, `user_correction`, `friction`.
- Recurring tool mistakes → this skill or scripts; persona/handoff mistakes → `.apm/agents/`; CLI/installer → `.podarcis/`.
- Tests: `.venv/bin/pytest .podarcis/tests`.
