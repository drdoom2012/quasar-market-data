.PHONY: test lint typecheck imports check live

test:
	uv run pytest

lint:
	uv run ruff check src tests spikes && uv run ruff format --check src tests spikes

typecheck:
	uv run mypy

imports:
	uv run lint-imports

check: lint typecheck imports test

# Hits Binance public endpoints; not part of `check`.
live:
	uv run pytest -m live tests/live -v
