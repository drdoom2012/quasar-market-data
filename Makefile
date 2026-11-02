.PHONY: test lint typecheck imports check live bench bench-health bench-indicators bench-oi-write images compose-test

test:
	uv run pytest

lint:
	uv run ruff check src tests spikes tools && uv run ruff format --check src tests spikes tools

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

# The 100-row open interest transaction on this machine's temp disk (ADR-0003 P-9: p99 <= 50 ms); not part of `check`.
# On the VPS run tools/bench_open_interest_write.py in python:3.12-slim instead (README, "Schema upgrade (version 2)").
bench-oi-write:
	uv run python tools/bench_open_interest_write.py --schema src/quasar_market_data/storage/migrations/002_open_interest.sql --max-p99-ms 50

# Builds the collector and api images (linux/amd64) with local tags; needs Docker, not part of `check`.
images:
	docker build --platform linux/amd64 --target collector -t quasar-market-data-collector:local .
	docker build --platform linux/amd64 --target api -t quasar-market-data-api:local .

# Images, the Compose file and the isolation test against the running production project (S21); not part of `check`.
# The isolation tests need a stock Linux Docker Engine (not OrbStack or Docker Desktop).
compose-test: images
	uv run pytest -m compose tests/deploy -v
