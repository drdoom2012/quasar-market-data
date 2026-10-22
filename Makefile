.PHONY: test lint typecheck imports check

test:
	uv run pytest

lint:
	uv run ruff check src tests spikes && uv run ruff format --check src tests spikes

typecheck:
	uv run mypy

imports:
	uv run lint-imports

check: lint typecheck imports test
