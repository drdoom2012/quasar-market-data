.PHONY: test lint typecheck imports check live bench bench-health bench-indicators

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

# Reconcile read benchmark on a generated database in a fresh temp dir; not part of `check`.
# PROFILE: smoke | medium | target | scaling. medium and target are slow and large; run them by hand.
PROFILE ?= smoke
bench:
	uv run python -m tests.collector.bench_reconcile --profile $(PROFILE)

# /health/ingest read benchmark on a generated database in a fresh temp dir; not part of `check`.
# PROFILE: smoke | medium | target. medium and target are slow and large; run them by hand (S17 HARD STOP 2).
bench-health:
	uv run python -m tests.api.bench_health --profile $(PROFILE)

# The largest /v1/indicators request on a generated database in a fresh temp dir; informative, not part of `check`.
bench-indicators:
	uv run python -m tests.api.bench_indicators
