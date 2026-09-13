---
name: synthesizer-gdrive
description: Synthesizer citation rules when sources_backend is gdrive. Cite Drive HTTPS URLs and DOIs — no local sources/ copy.
metadata: { "openclaw": { "emoji": "☁️" } }
---

# Skill: Synthesizer — GDrive Backend

Applies when `sources_backend: gdrive` in `.podarcis/config.yaml`.

## Done when

- The wiki page cites GDrive files as `https://drive.google.com/file/d/<file-id>/view` and papers as `https://doi.org/<doi>`.
- Every `sources[].id` is a `[^id]` footnote.
- `sources/` was not written to.
- `@auditor` has the written wiki path.

## Checkpoints

- Do not use `literature_download`.
- Do not use a `gdrive://` URI — full `https://drive.google.com/...` only.
- Do not copy, download, or commit files into `sources/`.

## Backend facts

- Read Drive via the official `drive` MCP (`search_files`, `read_file_content`). Discover papers with `literature_search` (metadata/abstracts only).
- `check_links.py` skips HTTPS `resource` values; they must still be real, clickable URLs.
