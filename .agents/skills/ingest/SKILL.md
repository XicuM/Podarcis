---
name: ingest
description: Synthesizes raw text from sources/ into the wiki.
metadata: { "openclaw": { "emoji": "📥" } }
---
# Role: Knowledge Synthesizer (`synthesis-agent`)

Execute as `synthesis-agent` (multi-agent) or sequentially (single agent).

## Workflow
1. **Manifest & Read**: Read `sources/state.json` in the workspace root to identify pending items in the `ingestion_queue` (where `"status": "pending"`). Parse raw source files and target directory `_index.md` files first.
2. **Synthesize**: Update or create a relevant note in `wiki/`.
   - **Content Rules**: Document findings, context/limitations, and conflicting evidence. Use callouts (`> ⚠️`) for confidence markers (**Strong consensus**, **Moderate evidence**, or **Preliminary/Contested**), limitations, or if the page relies on a single source (`> ⚠️ This page relies on a single source.`). Always attempt to explicitly connect abstract theoretical concepts to the group's active projects (e.g., via a "Relevancy to HPDSA" section).
   - **Sole Responsibility (Librarian Role)**: Your job is to consume fully extracted Markdown documents from the `sources/state.json` queue, proactively design the taxonomy (creating new categories if needed), deconstruct dense materials into modular, focused pages, read related wiki articles, and update the wiki accordingly. You are NOT responsible for finding PDFs or converting them to Markdown (that is the `research-agent`'s job, which must always be done using the `markitdown` tool rather than ad-hoc extraction scripts).
    - **Authentic Sources Only**: Only ingest and synthesize from sources that have a verified `original.pdf` or equivalent original raw text file in the repository. If a source file or summary under `sources/` lacks a verified original document and was generated from model memory/data, it must be deleted immediately. Never synthesize wiki entries from unverified sources.
    - **ANONYMIZATION:** Never include user-specific data in `wiki/` or `sources/`. All raw source summaries, metadata, and wiki pages must be completely objective. Never refer to "the user", their specific goals, profile, habits, or private metaphors (e.g., "pills"). Use general, objective conditional logic (e.g., "In individuals with [Trait]...") instead.
    - **CITATIONS:** Footnote every statement using `markdown-it` footnotes pointing to `sources/`. Place definitions at the bottom.
    - **YAML Frontmatter**: Every created or modified wiki page must begin with a standardized YAML frontmatter containing `title`, `category` (directory path relative to `wiki/`), `related` (list of linked internal relative files), and `rationale` (one-sentence justification of its location and purpose in the taxonomy).
     - **Links**: Interconnect wiki pages using relative markdown links (no `[[wikilinks]]`). Every time a wiki page is mentioned in body text, wrap it in a relative link — like Wikipedia. No unlinked page references.
3. **Audit Bloat**: Check if the target directory has > 15 content files (excluding `_index.md`). If so, reorganize per `AGENTS.md`.
4. **Indices, Indexing & Clean**: 
   - Update target `_index.md` files (ensure links have one-line summaries and do not contain checkmarks).
   - Rebuild the semantic index using the `wiki_update_index` tool.
   - Run the `lint_check_links` tool to verify the new/updated file's frontmatter and links pass validation.
   - Remove the ingested item from `sources/state.json`.
5. **Commit**: Commit activity using `git commit -m "..."` within the appropriate decoupled repository.
