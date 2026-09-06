# Podarcis MCP Servers

Five domain servers providing tools and resources to Podarcis agents.

They are **not** run standalone. The `podarcis-mcp` gateway (`.podarcis/gateway/`) loads each `server.py`, reads its `_tool_manager`, and rebinds every tool onto one unified `podarcis` MCP server — so agents see a single flat tool surface and the host needs exactly one `mcpServers` entry. `.podarcis/gateway/router.py` decides which modules bind: `DEFAULT_MCP_MODULES` is the git-tracked baseline, and the `mcp_modules:` section of `.podarcis/config.yaml` overrides it per instance.

## Servers Overview

| Server | Description | Entry point |
|---|---|---|
| **wiki-mcp** | Knowledge base search, publishing, and linting | `wiki/server.py` |
| **research-mcp** | Academic literature discovery and ingestion | `research/server.py` |
| **repo-mcp** | Git / Google Drive repository synchronization | `repo/server.py` |
| **menumaker-mcp** | Nutritional optimization and menu pricing | `menumaker/server.py` |
| **diagnostics-mcp** | Platform pain-point logging | `diagnostics/server.py` |

## Configuration

One entry, at the repository root (`.mcp.json`):

```json
{
  "mcpServers": {
    "podarcis": {
      "command": "/absolute/path/to/.venv/bin/podarcis-mcp",
      "args": ["--config", "/absolute/path/to/.podarcis/config.yaml"],
      "env": { "PROJECT_ROOT": "/absolute/path/to/Podarcis" }
    }
  }
}
```

Dependencies install from the project root: `.venv/bin/pip install -e .`

## Components

### wiki-mcp

**Tools**
- `wiki_search` - Consolidated search (keyword grep, semantic vector, hybrid, HyDE passage search, `--explain` score traces). The semantic paths are the reason this tool exists; for literal-string matching, agents should use their native `Grep`.
- `wiki_fetch` - Batch pattern snippet retrieval across wiki, protocols, or sources.
- `wiki_publish` - Atomic synthesis transaction: write page with frontmatter → rebuild index → link audit. The preferred write path for the Synthesizer.
- `wiki_reindex` - Rebuild the qmd semantic index and auto-sync folder context summaries. Only needed for edits made outside `wiki_publish`.
- `wiki_lint` - Audit broken links, YAML frontmatter schemas, directory bloat, and footnotes.

**Resources** — *(none)*. The former `wiki://collections/*` resources were `sorted(dir.rglob("*.md"))` reformatted as markdown lists, which every agent harness already provides as `Glob`.

### research-mcp

**Tools**
- `literature_search` - Search Semantic Scholar, OpenAlex, and PubMed.
- `literature_download` - Fetch metadata → PDF → markitdown → `sources/literature/<domain>/<id>/`.
- `literature_status` - List ingested sources with synthesis status, derived live by checking
  whether each source's id is cited as a `[^id]:` footnote anywhere in `wiki/` or
  `workspace/`. There is no manifest to enqueue/dequeue — status can't drift stale
  because it's recomputed from the citation graph on every call.

**Resources**
- `research://sources/index` - Live contents of the sources catalogue.

### repo-mcp

**Tools**
- `repo_sync` - Clone missing repos, pull remotes, ingest Google Drive deltas, optionally commit and push. Equivalent to `podarcis repo sync`.

### menumaker-mcp

**Tools**
- `intake_targets(age, gender, stage)` - Compute daily nutrient intake targets (RDA + Upper Limits).
- `food_search(query, limit)`, `food_nutrients(food_name)` - Search and retrieve USDA food profiles.
- `menu_optimize(age, gender, stage)` - Linear programming solver for the cheapest menu meeting all requirements.
- `menu_price(items)` - Calculate menu cost from supermarket price data.

### diagnostics-mcp

**Tools**
- `diagnostics_log` - Record a failure, tool error, user correction, or unmet expectation.
- `diagnostics_list` - Read back unresolved pain points.
- `diagnostics_clear` - Mark **all** pain points resolved. Indiscriminate; for a single entry, edit `.podarcis/diagnostics/pain_points.jsonl` directly.

## Development & Conventions

- **Transport**: The gateway speaks `stdio` by default (`--transport http|sse` also available). Individual `server.py` files remain runnable directly for debugging.
- **Stateless**: Servers are stateless. Git commits are always the agent's responsibility.
- **No Fabrication**: `literature_search` returns empty lists if no results are found; never hallucinates papers.
- **Strict Ingestion**: `literature_download` halts on failure and cleans up; agents must provide PDFs manually (`local:<path>`) if open-access fetching fails. It only ever accepts a PDF the server actually served — never a rendered HTML page.
- **Don't duplicate the harness**: A tool or resource earns its place only by doing something the agent's native tools cannot. Directory listings, file reads, and literal-string search do not qualify.
