.PHONY: install-dev test lint format-check typecheck agents-check check ci

install-dev:
	uv pip install -e '.[dev,git]'

test:
	uv run --no-sync pytest -q

lint:
	uv run --no-sync ruff check src/ tests/

format-check:
	uv run --no-sync ruff format --check src/ tests/

typecheck:
	uv run --no-sync mypy --strict src/
	uv run --no-sync pyright src/

# CLAUDE.md mirrors AGENTS.md except for its three header lines.
agents-check:
	@sed -e '1s/# AGENTS.md/# CLAUDE.md/' -e '3s/Guidance for Codex working/Guidance for Claude Code working/' -e 's/├── AGENTS.md                 # This file/├── CLAUDE.md                 # This file/' AGENTS.md | diff -u - CLAUDE.md && echo "AGENTS.md and CLAUDE.md in sync"

check: agents-check lint format-check typecheck test

ci: install-dev check
