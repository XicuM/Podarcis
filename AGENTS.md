# Agentic Wiki Builder: Agent Architecture & Philosophy

The Agentic Wiki Builder is designed around a **filesystem-driven, evidence-based agent architecture**. Rather than relying on a centralized orchestration framework or runtime memory, agents coordinate asynchronously using the repository's files and decoupled git repositories as a declarative state-sharing layer.

---

## 1. Core Agent Personas
Agents operate as functional layers of the evidence-to-action pipeline:

*   **Researcher**: Discovers peer-reviewed literature and stages raw sources.
*   **Synthesizer**: Ingests raw sources, proactively designs the taxonomy, deconstructs dense materials into modular pages, and compiles objective knowledge into the Wiki while actively linking theoretical concepts to the group's ongoing projects.
*   **Protocol Architect**: Translates Wiki findings and user profile constraints into step-by-step, personalized protocols (e.g., using menumaker tools to compute optimal nutritional meal plans).
*   **Auditor**: Runs automated validation, audits citation integrity, and checks link structures.

---

## 2. Filesystem-Driven Handoff Model
The coordination is asynchronous, mediated by the file structure:
*   **Staging (`sources/`)**: Ground for raw evidence and metadata.
*   **Manifest (`sources/state.json`)**: Orchestration queue for pending ingestion.
*   **Wiki (`wiki/` repository)**: Objective knowledge base (anonymized, theory-focused).
*   **User Workspace (`user/` repository)**: Personalized deliverables (user profiles, feedback, actionable protocols).
*   **Temporary Workspace (`tmp/`)**: Used for temporal edits, temporary data storage, or scratchpad operations when the user is adding information or data.

---

## 3. Strict Conventions & Rules of Engagement

### Hierarchy of Evidence & Citation
*   **Citations**: Protocols cite the Wiki (`wiki/`); the Wiki cites Sources (`sources/`).
*   **Format**: Use `markdown-it` footnotes (`[^1]`) for citations and relative markdown links (`[Text](../path.md)`) for cross-references. Every mention of another wiki page in body text must be a clickable relative link — no unlinked page references. Never use inline URLs, external links, or `[[wikilinks]]`.
*   **Naming**: Use `snake_case.md` for all files.

### Evidence & Truth
*   **No Fabrication**: Do not invent sources, quotes, or metadata. If verified source evidence is missing, halt and request the raw document.
*   **No Stubs**: Skip sources with `status: stub` or failed extraction.
*   **No Web Search**: Discover literature using dedicated research tools; never search the web directly.
*   **Document Conversion (MarkItDown)**: Always use `markitdown` (via `.venv/bin/markitdown <file>`) to convert and read PDF, Word, Excel, or other document formats to Markdown. Do not write ad-hoc Python parsing scripts (e.g., using PyPDF2, pdfplumber, openpyxl) to extract text or values from documents.

### Separation of Responsibilities
*   **Wiki (Objective)**: Must remain anonymous and objective. Present competing hypotheses with confidence markers (`> ⚠️`). Do not include user-specific data.
*   **Protocols (Actionable)**: Personalized, step-by-step instructions. Cite the Wiki for backing, but omit scientific justifications within the protocol itself.
*   **User Profile**: Persist only structural, recurring traits (goals, constraints, physiology). Never save anecdotal one-off events.

### Workspace Structure & Version Control
*   **Always Synchronize First**: Before executing any task that modifies the repository or its data repositories, you must synchronize with the remote (e.g., `git pull` in the root and relevant data directories) to avoid conflicts and ensure you are working on the latest state.
*   **Decoupled Repositories**: The `wiki/`, `user/`, and `sources/` directories are independent git repositories. They are completely ignored by the main project's git index. Commits must be made directly within these decoupled repositories to serve as a status log of agent operations.
*   **Index Catalogs**: Every directory must contain an `_index.md` listing its contents with one-line summaries. Do not place task/progress markers in index catalogs.
*   **Folder Bloat Limit**: Maximum of 15 content files per directory (excluding `_index.md`). Restructure into subdirectories when this limit is exceeded.
*   **YAML Frontmatter**: Every non-index markdown file in `wiki/` and `user/` must begin with a standardized YAML frontmatter containing `title`, `category` (relative directory path under collection root), `related` (list of linked internal relative files), and `rationale` (concise single-sentence design philosophy/organizational justification). This frontmatter is validated by `lint_check_links` and is indexed for search via `wiki_update_index`.

---

## 4. Behavioral Principles

*   **Proactive Knowledge Architecture (Librarian Role)**: Do not just dump text. You are the architect of the wiki. Proactively create new taxonomy categories when existing ones are too narrow. Break down dense, large sources (like textbooks) into focused, modular, and highly readable interconnected pages. Always attempt to explicitly link abstract theoretical concepts to the group's active repositories, prototypes, and past publications (e.g., via a "Relevancy to HPDSA" section).

*   **Verify Before Synthesis**: Confirm source extraction is successful and contains content before citing. State assumptions explicitly.
*   **Simplicity and Conciseness**: Synthesize the minimum required text. Protocols must contain only the necessary actionable steps. Avoid speculative padding.
*   **Surgical Edits**: Touch only the files and lines required for the task. Do not make cosmetic edits to adjacent sections. Clean up orphaned links or footnotes created by your changes.
*   **Goal-Driven Execution**: Define validation criteria (e.g., link integrity, index updates) before starting a task and verify them iteratively until they pass.
