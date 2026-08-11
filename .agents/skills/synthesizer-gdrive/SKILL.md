---
name: synthesizer-gdrive
description: Synthesizer ingestion behaviour when sources_backend is gdrive. Browse GDrive via google-drive-mcp, cite by HTTPS URL — no local copy committed.
metadata: { "openclaw": { "emoji": "☁️" } }
disable-model-invocation: true
user-invocable: false
disabled: true
---

# Skill: Synthesizer — GDrive Backend

Use this skill when `sources_backend: gdrive` is set in `.podarcis/config.yaml`.

## Ingestion Workflow

### Step 1 — Discover sources

- Use official remote `drive` MCP (`search_files`) to browse the shared GDrive folder(s) and identify relevant documents.
- Use `research-mcp` (`search_literature`) to discover peer-reviewed papers by keyword or topic.

### Step 2 — Read content without copying

- Use official remote `drive` MCP (`read_file_content`) to read the full text of relevant GDrive documents.
- Use `research-mcp` (`search_literature`) to retrieve paper abstracts and metadata.
- **Do not download, copy, or commit any file locally.** GDrive documents stay on GDrive.

### Step 3 — Build `sources[]` in frontmatter

For each source used, add an entry to the wiki concept's `sources:` block:

| Source type | `resource` value |
|---|---|
| GDrive document | `https://drive.google.com/file/d/<file-id>/view` |
| Peer-reviewed paper | `https://doi.org/<doi>` |

Both formats are clickable in Obsidian and skipped by `check_links.py`.

### Step 4 — Write the wiki concept

Write the OKF v0.2 concept page in `wiki/` using only information extracted in Step 2.
Cite sources via `[^source_id]` footnotes matching the `sources[].id` keys.

### Step 5 — Hand off to `@auditor`

Report the written file path to `@auditor` for verification.

## Rules

- Never use `research-mcp`'s `download_paper` tool in this backend — papers are cited by DOI only.
- Never use a `gdrive://` URI scheme — always use the full `https://drive.google.com/...` URL.
- Never write files to `sources/` — that directory is not managed in this backend.
