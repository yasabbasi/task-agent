.DEFAULT_GOAL := help
.PHONY: help install check lint test format loc install-skill

LOC_FILES ?= src/task_agent/*.py
LOC_MAX ?= 1500
YAML_RULES = {document-start: disable, line-length: disable, truthy: {check-keys: false}, \
	comments: {min-spaces-from-content: 1}, indentation: {spaces: 2}}

help: ## List the targets
	@awk -F ':.*## ' '/^[a-z-]+:.*## / {printf "%-14s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: ## Install the locked dependencies
	uv sync --locked

check: lint test loc ## Lint, test and count lines; run it before you finish

lint: ## Check style, types, Markdown, YAML and JSON
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy
	uv run pymarkdown scan --recurse --exclude .venv --exclude .worktrees .
	uv run yamllint --strict -d '{extends: default, rules: $(YAML_RULES)}' .github
	uv run python scripts/lint_json.py

test: ## Run the tests
	uv run pytest

format: ## Fix style and formatting
	uv run ruff format .
	uv run ruff check --fix .

loc: ## Count the lines in src/ against the cap
	@total=$$(cat $(LOC_FILES) | wc -l | tr -d ' '); \
	echo "$$total lines in $(LOC_FILES) (cap $(LOC_MAX))"; \
	test $$total -le $(LOC_MAX)

install-skill: ## Link /task into ~/.claude/skills so it works in any repo
	mkdir -p ~/.claude/skills
	ln -sfn $(CURDIR)/.claude/skills/task ~/.claude/skills/task
