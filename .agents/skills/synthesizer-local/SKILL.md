---
name: synthesizer-local
description: Synthesizer ingestion behaviour when sources_backend is local. Scrape relevant GDrive files into sources/, download papers via research-mcp into sources/literature/, commit, then cite by relative path.
metadata: { "openclaw": { "emoji": "🗂️" } }
disable-model-invocation: true
user-invocable: false
disabled: true
---

# Skill: Synthesizer — Local Git Backend

Use this skill when `sources_backend: local` is set in `.podarcis/config.yaml`.

## Ingestion Workflow

### Step 1 — Discover sources

- Use `google-drive-mcp` to browse the shared GDrive folder(s) and identify relevant documents.
- Use `research-mcp` (`search_literature`) to discover peer-reviewed papers by keyword or topic.

### Step 2 — Copy useful files into `sources/`

**For GDrive documents:**
- Read the document content via `google-drive-mcp`.
- Write the relevant content to `sources/<domain>/<slug>/raw.md` (OKF frontmatter + extracted body).
- Write a `sources/<domain>/<slug>/metadata.md` with title, author, GDrive URL, and date.
- Git-add and commit: `git add sources/<domain>/<slug>/ && git commit -m "feat(sources): ingest <slug>"` inside the `sources/` directory.

**For peer-reviewed papers:**
- Use `research-mcp` (`download_paper`) with `domain=<domain>` — this writes to `sources/literature/<domain>/<slug>/` automatically when `sources_backend: local`.
- The tool handles the commit manifest (`sources/state.json`); you only need to git-add and commit the new files.

### Step 3 — Build `sources[]` in frontmatter

Use relative paths from the wiki concept file to the `sources/` directory:

```yaml
sources:
  - id: smith2024
    resource: "../../sources/literature/hpc/smith2024/metadata.md"
    title: "..."
    author: "..."
    last_modified: 2024-03-15
  - id: team_spec_v2
    resource: "../../sources/interconnect/team_spec_v2/metadata.md"
    title: "..."
    author: "Internal Team"
    last_modified: 2025-01-10
```

Relative paths are resolved by `check_links.py` and are clickable in Obsidian.

### Step 4 — Write the wiki concept

Write the OKF v0.2 concept page in `wiki/` using only information from the committed sources.
Cite sources via `[^source_id]` footnotes matching the `sources[].id` keys.

### Step 5 — Hand off to `@auditor`

Report the written file path to `@auditor` for verification.

## Rules

- Always commit source files to `sources/` **before** writing the wiki concept page.
- The `resource` field MUST be a relative path resolvable from the concept file's location.
- Never use HTTPS GDrive URLs as `resource` — use local relative paths.
- Per-user literature (workspace) still uses `workspace/literature/` — do not conflate with `sources/`.
