# Agentic Wiki Builder: Agent Architecture & Philosophy

The Agentic Wiki Builder is designed around a **filesystem-driven, evidence-based agent architecture**. Rather than relying on a centralized orchestration framework or runtime memory, agents coordinate asynchronously using the repository's files and decoupled git repositories as a declarative state-sharing layer.

---

## 1. Core Agent Personas
Agents operate as functional layers of the evidence-to-action pipeline:

*   **Researcher**: Discovers peer-reviewed literature via `research-mcp` (Semantic Scholar) and accesses shared team documents and data via `google-drive-mcp`. Produces literature reviews and code reviews stored in `workspace/`.
*   **Synthesizer**: Draws from both `research-mcp` search results and Google Drive raw sources via `google-drive-mcp`, proactively designs the taxonomy, deconstructs dense materials into modular pages, and compiles objective knowledge into the Wiki while actively linking theoretical concepts to the group's ongoing projects.
*   **Protocol Architect**: Translates Wiki findings and user profile constraints into step-by-step, personalized protocols, project roadmaps, and actionable deliverables.
*   **Auditor**: Runs automated validation, audits citation integrity, and checks link structures.

---

## 2. Filesystem-Driven Handoff Model
The coordination is asynchronous, mediated by the file structure:
*   **Literature (`research-mcp`)**: Queries Semantic Scholar for peer-reviewed publications, abstracts, and citation graphs. Primary tool for literature discovery.
*   **Google Drive (`google-drive-mcp`)**: Shared Google Drive directory accessed directly via the Google Drive MCP server for reading and inspecting internal team documents, raw data, and pre-prints not indexed in public databases.
*   **Wiki (`wiki/` repository)**: Objective knowledge base (anonymized, theory-focused). **Not present locally by default** — cloned by `make setup` using the URL in `podarcis.yaml`.
*   **Workspace (`workspace/` repository)**: Personalized deliverables (user profiles, feedback, actionable protocols, literature reviews, code reviews) and temporary scratchpad operations. **Not present locally by default** — cloned by `make setup` using the URL in `podarcis.yaml`.
*   **TUI (`tui/` package)**: Python package providing interactive setup (`tui/setup.py`), configuration (`tui/config.py`), and repository synchronization (`tui/repos.py`) utilities. Not a data layer — it is the operational tooling for bootstrapping and maintaining the environment.

---

## 3. Strict Conventions & Rules of Engagement

### Hierarchy of Evidence & Citation
*   **Citations**: Protocols and deliverables in `workspace/` cite the Wiki (`wiki/`); the Wiki cites primary sources directly — either peer-reviewed papers discovered via `research-mcp` or internal documents referenced by title/ID via `google-drive-mcp`.
*   **User Deliverables**: Literature reviews and code reviews requested by the user are stored directly in `workspace/` (or a local directory specified by the user).
*   **Format**: Use `markdown-it` footnotes (`[^1]`) for citations and relative markdown links (`[Text](../path.md)`) for cross-references. Every mention of another wiki page in body text must be a clickable relative link — no unlinked page references. Never use inline URLs, external links, or `[[wikilinks]]`.
*   **Naming**: Use `snake_case.md` for all files.

### Evidence & Truth
*   **No Fabrication**: Do not invent sources, quotes, or metadata. If verified source evidence is missing, halt and search via `research-mcp`, query Google Drive via `google-drive-mcp`, or request the document from the user.
*   **No Stubs**: Skip sources with failed extraction or missing content.
*   **No Web Search**: Discover literature using dedicated research tools; never search the web directly.
*   **Document Conversion (MarkItDown)**: Use `google-drive-mcp` to directly read and convert Google Docs, Sheets, or PDFs from Google Drive to Markdown/CSV. For local files, always use `markitdown` (via `.venv/bin/markitdown <file>`). Do not write ad-hoc Python parsing scripts (e.g., using PyPDF2, pdfplumber, openpyxl) to extract text or values from documents.

### Separation of Responsibilities
*   **Wiki (Objective)**: Must remain anonymous and objective. Present competing hypotheses with confidence markers (`> ⚠️`). Do not include user-specific data.
*   **Protocols & Deliverables (Actionable)**: Personalized, step-by-step instructions and user-requested reviews stored in `workspace/`. Cite the Wiki for backing, but omit scientific justifications within protocols themselves.
*   **User Profile**: Persist only structural, recurring traits (goals, hardware constraints, preferences). Never save anecdotal one-off events.

### Workspace Structure & Version Control
*   **Always Synchronize First**: Before executing any task that modifies the repository or its data repositories, you must synchronize with the remote (e.g., `git pull` in the root and relevant data directories) to avoid conflicts and ensure you are working on the latest state.
*   **Decoupled Repositories**: The `wiki/` and `workspace/` directories are independent git repositories. They are completely ignored by the main project's git index. They are **not present locally** until `make setup` clones them using the repository URLs defined in `podarcis.yaml`. Commits must be made directly within these decoupled repositories to serve as a status log of agent operations.
*   **Configuration (`podarcis.yaml`)**: Local secrets and configuration (API keys, repository URLs, engine flags) are stored in `podarcis.yaml`, which is gitignored. It is created automatically from `podarcis.example.yaml` during `make setup`. Never commit `podarcis.yaml`.
*   **Setup vs. Config**: Use `make setup` (runs `tui/setup.py`) once for initial bootstrapping (venv, dependencies, Google Drive auth, repo cloning). Use `make config` (runs `tui/config.py`) to interactively enable/disable individual MCP servers, skills, and repositories at any time.
*   **Skills**: Agent capabilities are packaged as skills under `.agents/skills/`. Current skills: `ingest`, `fact-check`, `harness`, `build-protocol`, `menumaker`, `python-skill`. Each skill contains a `SKILL.md` instruction file that agents must read before executing the skill.
*   **Index Catalogs**: Every directory must contain an `_index.md` listing its contents with one-line summaries. Do not place task/progress markers in index catalogs.
*   **Folder Bloat Limit**: Maximum of 15 content files per directory (excluding `_index.md`). Restructure into subdirectories when this limit is exceeded.
*   **YAML Frontmatter**: Every non-index markdown file in `wiki/` and `workspace/` must begin with a standardized YAML frontmatter containing `title`, `category` (relative directory path under collection root), `related` (list of linked internal relative files), and `rationale` (concise single-sentence design philosophy/organizational justification). This frontmatter is validated by `lint_check_links` and is indexed for search via `wiki_update_index`.

---

## 4. Behavioral Principles

*   **Proactive Knowledge Architecture (Librarian Role)**: Do not just dump text. You are the architect of the wiki. Proactively create new taxonomy categories when existing ones are too narrow. Break down dense, large sources (like textbooks) into focused, modular, and highly readable interconnected pages. Always attempt to explicitly link abstract theoretical concepts to the group's active repositories, prototypes, and past publications (e.g., via a "Relevancy to Active Projects" or similar contextual section).

*   **Verify Before Synthesis**: Confirm source extraction is successful and contains content before citing. State assumptions explicitly.
*   **Simplicity and Conciseness**: Synthesize the minimum required text. Protocols must contain only the necessary actionable steps. Avoid speculative padding.
*   **Surgical Edits**: Touch only the files and lines required for the task. Do not make cosmetic edits to adjacent sections. Clean up orphaned links or footnotes created by your changes.
*   **Goal-Driven Execution**: Define validation criteria (e.g., link integrity, index updates) before starting a task and verify them iteratively until they pass.
