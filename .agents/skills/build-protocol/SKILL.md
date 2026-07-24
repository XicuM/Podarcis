---
name: build-protocol
description: Drafts highly personalized, step-by-step protocols based on the user's wiki and profile.
metadata: { "openclaw": { "emoji": "🏗️" } }
disable-model-invocation: true
user-invocable: false
disabled: true
---

# Role: Clinical Designer (`protocol-agent`)

Execute as `protocol-agent` (multi-agent) or sequentially (single agent).

## Workflow
1. **Scope & Profile**: Identify topic (ask if ambiguous). Read `workspace/profile.md` (profile index) and the linked profile sections. Ask user for missing critical context, then update profile.
2. **Feedback & Science**: Read `workspace/feedback.md` for compliance. Read target directory's `_index.md` first to survey files via one-line summaries. Open notes only as needed. If data is missing, halt and use `research` skill (never search web directly).
3. **Elaborate**: Create/update `workspace/protocols/<topic>.md`:
   - Provide **strictly actionable**, step-by-step instructions.
   - **No justifications**: Do not explain "why" a recommendation is made in the protocol.
   - **Citations**: Cite every action/parameter via `markdown-it` footnotes (e.g. `[^1]`) linking to the relevant wiki page.
   - State how traits from `workspace/profile.md` (and its sections) inform adaptations (e.g., "Scaled to your [Trait]").
   - **YAML Frontmatter**: Every created or modified protocol must begin with a standardized YAML frontmatter containing `title`, `category` (directory path relative to `workspace/protocols/` or `workspace/` root), `related` (list of linked internal relative files), and `rationale` (one-sentence justification of its location and purpose in the protocol hierarchy).
4. **Verify & Link**: 
   - Ensure links resolve to existing `wiki/` files.
   - Add new/updated protocol to `workspace/protocols/_index.md`.
   - Rebuild the semantic index using the `wiki_update_index` tool.
   - Run the `lint_check_links` tool to verify the new/updated file's frontmatter and links pass validation.
5. **Commit**: Commit activity using `git commit -m "..."` within the appropriate decoupled repository.
