.PHONY: install uninstall clean help

PYTHON ?= python3

help: ## Show available Makefile targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

install: ## Bootstrap virtual environment, dependencies, credentials, and CLI
	@$(PYTHON) .podarcis/install.py

uninstall: ## Remove symlink, venv, and build artefacts created by install
	@$(PYTHON) .podarcis/uninstall.py

clean: ## Clean Python build artifacts and cache files
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	rm -rf .pytest_cache *.egg-info .podarcis/*.egg-info

