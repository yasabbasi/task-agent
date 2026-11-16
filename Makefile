.DEFAULT_GOAL := help
.PHONY: help install check lint test format loc install-skill doctor dispatch dispatch-all \
	watch verify init metrics ready approve send-back

LOC_FILES ?= src/task_agent/*.py
LOC_MAX ?= 2000
REPO ?= yasabbasi/task-agent
EVERY ?= 600
TARGET = --repo $(REPO) $(if $(CLONE),--clone $(CLONE))
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
	uv run pymarkdown scan --recurse --exclude .venv --exclude .worktrees --exclude prompts .
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

# The next five targets use the App's key. Run them in your own terminal: they need Touch ID.

doctor: ## Check that this machine can run agents, with a fix for each failure
	uv run python scripts/doctor.py

dispatch: ## Run one pass for REPO; DRY_RUN=1 only lists what it would take
	uv run task-agent dispatch $(TARGET) $(if $(DRY_RUN),--dry-run)

dispatch-all: ## Run one pass over every configured repo
	uv run task-agent dispatch-all

watch: ## Keep running passes over every configured repo, one every EVERY seconds
	uv run task-agent watch --every $(EVERY)

verify: ## Prove the guardrails hold for REPO; REQUEST=1 asks the running watcher instead
	uv run task-agent verify $(TARGET) $(if $(PR),--pr $(PR)) $(if $(REQUEST),--request)

init: ## Set up REPO for the loop
	uv run task-agent init --repo $(REPO)

metrics: ## Compute the loop's numbers; PUBLISH=1 opens a pull request as the App
	uv run task-agent metrics $(if $(PUBLISH),--publish)

ready: ## Mark issues ready for an agent: make ready ISSUES="1 2"
	for issue in $(ISSUES); do gh issue edit $$issue --repo $(REPO) --add-label status:ready; done

approve: ## Approve and squash-merge a pull request: make approve PR=5
	gh pr review $(PR) --repo $(REPO) --approve
	gh pr merge $(PR) --repo $(REPO) --squash

send-back: ## Send a pull request back with a note: make send-back PR=5 NOTE="..."
	gh pr comment $(PR) --repo $(REPO) --body "$$NOTE"
