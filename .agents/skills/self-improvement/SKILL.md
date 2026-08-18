---
name: self-improvement
description: Analyzes session pain points post-session, logs metrics to .podarcis/diagnostics/, and enables backend agents to inspect and improve the platform when requested.
metadata: { "openclaw": { "emoji": "🌱" } }
---

# Skill: Self-Improvement & Platform Diagnosis

Use this skill to log post-session friction points to `.podarcis/diagnostics/` and resolve platform issues when requested by the user.

---

## Storage & Isolation Policy

> [!IMPORTANT]
> All diagnostic logs and pain points are strictly stored in `.podarcis/diagnostics/pain_points.jsonl` and `.podarcis/diagnostics/sessions/`.
> Diagnostics MUST NOT leak into domain directories (`wiki/` or `workspace/`).

---

## 1. Post-Session Diagnosis Workflow

After completing a session (or when analyzing a conversation transcript):

1. **Run Transcript Analysis**:
   ```bash
   python .agents/skills/self-improvement/scripts/diagnose_session.py --transcript <path_to_transcript.jsonl>
   ```
   Or inspect logged issues directly via CLI:
   ```bash
   podarcis diagnose
   ```

2. **Extracted Pain Point Categories**:
   - `command_failure`: Failed terminal execution or shell errors.
   - `execution_error`: Unhandled Python/code tracebacks.
   - `user_correction`: Explicit user directives correcting agent actions or layout.
   - `friction`: Inefficient or repeated multi-step retry loops.

---

## 2. Platform Improvement Workflow (Backend Execution)

When the user asks the agents to **"improve the platform"** or **"resolve logged pain points"**:

1. **Read Unresolved Issues**:
   Read `.podarcis/diagnostics/pain_points.jsonl` or run:
   ```bash
   podarcis diagnose --json
   ```

2. **Diagnose Systemic Root Causes**:
   Categorize recurring pain points across session logs:
   - Tool/SDK usage errors -> Update `SKILL.md` guidance or scripts in `.agents/skills/`.
   - Subagent prompt or handoff issues -> Update subagent configs in `.agents/agents/`.
   - CLI / installer issues -> Refactor `.podarcis/` CLI or backend modules.

3. **Apply & Verify Fixes**:
   - Make surgical edits to the affected platform code or documentation.
   - Run the test suite to verify stability:
     ```bash
     .venv/bin/pytest .podarcis/tests
     ```

4. **Prepare Fortified Pull Request**:
   - Platform fixes are committed on an isolated branch and proposed as a PR without leaking private user data.
   - Run the PR preparation script (enforces domain isolation and sanitizes descriptions):
     ```bash
     python .agents/skills/self-improvement/scripts/prepare_pr.py --title "Fix: resolve tool parameter parsing in menumaker" --body "Addresses command_failure logged during session execution."
     ```
   - **Enforced PR Guardrails**:
     - PRs **strictly reject** any changes touching `workspace/`, `sources/`, `wiki/`, or `.env*`.
     - PR titles, commit messages, and descriptions are automatically scrubbed for credentials, tokens, emails, and local user paths.
   - Automatically marks resolved issues in `.podarcis/diagnostics/pain_points.jsonl`.
