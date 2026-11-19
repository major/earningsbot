.DEFAULT_GOAL := help
.PHONY: help install test cov lint format format-check fix typecheck check all clean

help: ## Show this help message
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  %-14s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: ## Install dependencies with uv sync
	uv sync

test: ## Run pytest with branch coverage
	uv run pytest

cov: ## Run tests and also write an HTML coverage report to htmlcov/
	uv run pytest --cov-report=html

lint: ## Check code with ruff
	uv run ruff check

format: ## Format code with ruff (applies changes)
	uv run ruff format

format-check: ## Check formatting without changing files
	uv run ruff format --check

fix: ## Auto-fix lint issues, then format code
	uv run ruff check --fix
	uv run ruff format

typecheck: ## Type-check with ty
	uv run ty check

check: format-check lint typecheck test ## Run format-check, lint, typecheck, and test

all: check ## Same as check

clean: ## Remove caches and coverage artifacts
	rm -rf .pytest_cache .ruff_cache htmlcov
	rm -f .coverage .coverage.* coverage.xml
	find . -path ./.venv -prune -o -name __pycache__ -type d -exec rm -rf {} +
