---
name: synthesizer-local
description: Synthesizer citation rules when sources_backend is local. Stage files under sources/, commit them, cite by relative path.
metadata: { "openclaw": { "emoji": "🗂️" } }
---

# Skill: Synthesizer — Local Git Backend

Applies when `sources_backend: local` in `.podarcis/config.yaml`.

## Done when

- The wiki page cites only files that exist under `sources/` (including `sources/literature/`).
- Every `sources[].id` is a `[^id]` footnote; every `resource` is a relative path from the concept file (one the linter can resolve — check with `wiki_lint`).
- New source directories are committed in the `sources/` repo before they are cited.
- `@auditor` has the written wiki path.

## Checkpoints

- Commit `sources/<domain>/<slug>/` **before** writing the wiki page.
- Never put a Drive HTTPS URL in `resource`.
- Per-user literature stays in `workspace/literature/` — do not mix with `sources/`.

## Backend facts

- GDrive documents: write `sources/<domain>/<slug>/raw.md` (OKF frontmatter + body) and `metadata.md` (title, author, GDrive URL, date), then commit inside `sources/`.
- Papers: `literature_download` with `domain=<domain>` (writes `sources/literature/<domain>/<slug>/` and the domain `_index.md`).
- Unsynthesized sources are whatever `literature_status` still reports as pending — there is no ingest queue to maintain.

```yaml
sources:
  - id: smith2024
    resource: "../../sources/literature/hpc/smith2024/metadata.md"
    title: "..."
    author: "..."
    last_modified: 2024-03-15
```
