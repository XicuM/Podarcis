# Agentic Wiki Builder MCP Servers

A collection of four MCP servers that provide domain-specific tools and resources for the Agentic Wiki Builder over `stdio`.

## Servers Overview

| Server | Description | Entry point |
|---|---|---|
| **finance-mcp** | Investment calculations and expense tracking | `finance/server.py` |
| **wiki-mcp** | Knowledge base query and linting | `wiki/server.py` |
| **research-mcp** | Academic literature discovery and ingestion | `research/server.py` |
| **menumaker-mcp** | Nutritional optimization and menu pricing | `menumaker/server.py` |

## Installation

```bash
# From project root — installs into the shared venv
.venv/bin/pip install -r requirements.txt
```

## Configuration

Add the following to your MCP host configuration (e.g., `~/.config/claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "finance-mcp": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["/absolute/path/to/.agents/mcp/finance/server.py"],
      "env": {
        "PROJECT_ROOT": "/absolute/path/to/agentic-wiki-builder"
      }
    },
    "wiki-mcp": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["/absolute/path/to/.agents/mcp/wiki/server.py"],
      "env": {
        "PROJECT_ROOT": "/absolute/path/to/agentic-wiki-builder"
      }
    },
    "research-mcp": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["/absolute/path/to/.agents/mcp/research/server.py"],
      "env": {
        "PROJECT_ROOT": "/absolute/path/to/agentic-wiki-builder",
        "SEMANTIC_SCHOLAR_API_KEY": "optional-api-key"
      }
    },
    "menumaker-mcp": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["/absolute/path/to/.agents/mcp/menumaker/server.py"]
    }
  }
}
```
*Note: Replace `/absolute/path/to/` with the actual absolute path to your repository.*

## Components

### finance-mcp

**Tools**
- `calc_cagr`, `calc_fv`, `calc_dca`, `calc_weights` - Investment math and projections.
- `stock_price`, `stock_news`, `stock_backtest` - Market data and backtesting.
- `expense_parse`, `expense_monthly`, `expense_range`, `expense_category`, `expense_uncategorized`, `expense_summary`, `expense_trading`, `expense_transfers`, `expense_top`, `expense_export_monthly` - Expense tracking and CSV ingestion.

**Resources**
- `finance://transactions` - Live contents of the canonical transactions CSV.
- `finance://category-rules` - Category rules and patterns.

### wiki-mcp

**Tools**
- `wiki_search` - Consolidated search (hybrid, semantic vector, keyword BM25, HyDE passage search, `--explain` score traces).
- `wiki_get` - Retrieve document content by path with optional line slicing (`start_line`, `num_lines`).
- `wiki_multi_get` - Batch pattern snippet retrieval across wiki, protocols, or sources.
- `wiki_update_index` - Rebuild QMD semantic index and auto-sync folder context summaries.
- `complete_source_synthesis` - Atomic synthesis transaction (frontmatter format, queue done status, index rebuild, link audit).
- `lint_check_links` - Audit broken links, YAML frontmatter schemas, directory bloat, and footnotes.

**Resources**
- `wiki://collections/wiki` - Directory listing of the wiki.
- `wiki://collections/protocols` - Directory listing of protocols.
- `wiki://collections/sources` - Directory listing of sources.

### research-mcp

**Tools**
- `search_literature` - Search 19+ providers via academic-mcp.
- `download_paper` - Fetch metadata → PDF → markitdown → `sources/state.json` queue.
- `queue_list`, `queue_enqueue`, `queue_dequeue` - Manage the ingestion queue.

**Resources**
- `research://state` - Live contents of the ingestion queue.
- `research://sources/index` - Live contents of the sources catalogue.

### menumaker-mcp

**Tools**
- `get_intake_targets` - Compute daily nutrient intake targets.
- `search_foods`, `get_food_nutrients` - Search and retrieve USDA food profiles.
- `optimize_menu` - Linear programming solver for cheapest optimal nutritional menu.
- `price_menu` - Calculate menu prices via supermarket data.

**Resources**
- *(No resources exported)*

## Development & Conventions

- **Transport**: All servers communicate via standard `stdio`.
- **Stateless**: Servers are stateless. Git commits are always the agent's responsibility.
- **No Fabrication**: `search_literature` returns empty lists if no results are found; never hallucinates papers.
- **Strict Ingestion**: `download_paper` halts on failure and cleans up; agents must provide PDFs manually (`local:<path>`) if open-access fetching fails.
