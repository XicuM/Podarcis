---
name: fact-check
description: Verifies claims against wiki, protocols, and scientific literature.
metadata: { "openclaw": { "emoji": "✅" } }
---

# Role: Fact Checker (`audit-agent`)

Execute as `audit-agent` (multi-agent) or sequentially (single agent).

## Workflow
1. **Claims**: Break user input into atomic, verifiable claims.
2. **Internal Search**: Use `wiki-query` in `.agents/` directory:
   - Precision: `qmd query "<keywords>" --json`
   - Fast keyword lookup: `qmd search "<keywords>"`
3. **External Research**: If internal context is insufficient, use `research` skill:
   - `python .agents/skills/research/scripts/search_papers.py "<keywords>"`
4. **Verdict**: Synthesize evidence as **Supported** (matches wiki/literature), **Contradicted** (refuted by evidence), or **Mixed** (conflicting).
5. **Report**: Propose wiki edits if outdated.
