.PHONY: test lint typecheck imports check live simulate bench bench-health bench-health-oi bench-indicators bench-oi-write bench-overview images compose-test

test:
	uv run pytest

lint:
	uv run ruff check src tests spikes tools && uv run ruff format --check src tests spikes tools

typecheck:
	uv run mypy

imports:
	uv run lint-imports

check: lint typecheck imports test

# Long capacity simulations of the funding rate task (ADR-0004 P-10, E-14: b = 150 and 250, 200 rounds); not part of `check`.
simulate:
	uv run pytest -m simulation tests -v -o addopts="--strict-markers"

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

# /health/ingest with 600 candle and 600 open interest series, open interest off and on, in a fresh temp dir; not part of `check`.
bench-health-oi:
	uv run python -m tests.api.bench_health_open_interest

# The largest /v1/indicators request on a generated database in a fresh temp dir; informative, not part of `check`.
bench-indicators:
	uv run python -m tests.api.bench_indicators

# The 100-row open interest transaction on this machine's temp disk (ADR-0003 P-9: p99 <= 50 ms); not part of `check`.
# On the VPS run it in the Dockerfile's runtime image instead (README, "Open interest write benchmark").
bench-oi-write:
	uv run python tools/bench_open_interest_write.py --schema src/quasar_market_data/storage/migrations/002_open_interest.sql --max-p99-ms 50

# One refresh of the symbol overview against a served API on a generated database in a fresh temp dir (OI-6 spec 7); not part of `check`.
# SERIES symbols; OVERVIEW_PROFILE smoke | target (target = 100 000 candle rows per series, the size of bench-health's target:
# at 600 series tens of minutes and several GB of temp disk); OI_MODE proxy (G0: /v1/coverage stands in for the open interest
# call) | real (/v1/open-interest/coverage). ARGS carries the rest, for example ARGS="--stress", ARGS="--l-tunnel-ms 40",
# ARGS="--refreshes 3 --sustained-minutes 0".
SERIES ?= 300
OVERVIEW_PROFILE ?= smoke
OI_MODE ?= real
ARGS ?=
bench-overview:
	uv run python -m tests.api.bench_overview_refresh --series $(SERIES) --profile $(OVERVIEW_PROFILE) --oi-mode $(OI_MODE) $(ARGS)

# Builds the collector and api images (linux/amd64) with local tags; needs Docker, not part of `check`.
images:
	docker build --platform linux/amd64 --target collector -t quasar-market-data-collector:local .
	docker build --platform linux/amd64 --target api -t quasar-market-data-api:local .

# Images, the Compose file and the isolation test against the running production project (S21); not part of `check`.
# The isolation tests need a stock Linux Docker Engine (not OrbStack or Docker Desktop).
compose-test: images
	uv run pytest -m compose tests/deploy -v
