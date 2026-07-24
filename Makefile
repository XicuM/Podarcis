.PHONY: help setup config sync test lint clean

PYTHON ?= python3
VENV ?= .venv
VENV_PYTHON ?= $(VENV)/bin/python
VENV_PIP ?= $(VENV)/bin/pip
PYTEST ?= $(VENV)/bin/pytest

help: ## Show available Makefile targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

install: ## Bootstrap virtual environment, dependencies, Git LFS, and credentials
	@$(PYTHON) tui/install.py

config: ## Interactively enable/disable MCP servers, skills, and repositories
	@if [ -x "$(VENV_PYTHON)" ]; then \
		$(VENV_PYTHON) tui/config.py; \
	else \
		$(PYTHON) tui/config.py; \
	fi

sync: ## Sync and clone decoupled workspace repositories (wiki, workspace)
	@if [ -x "$(VENV_PYTHON)" ]; then \
		$(VENV_PYTHON) -c "from tui.repos import sync_repos; sync_repos()"; \
	else \
		$(PYTHON) -c "from tui.repos import sync_repos; sync_repos()"; \
	fi

test: ## Run test suite across all MCP servers and skills
	@if [ -x "$(PYTEST)" ]; then \
		$(PYTEST); \
	else \
		pytest; \
	fi

lint: ## Run link integrity check across wiki markdown files
	@if [ -x "$(VENV_PYTHON)" ]; then \
		$(VENV_PYTHON) .agents/mcp/wiki/check_links.py; \
	else \
		$(PYTHON) .agents/mcp/wiki/check_links.py; \
	fi

clean: ## Clean Python build artifacts and cache files
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	rm -rf .pytest_cache
