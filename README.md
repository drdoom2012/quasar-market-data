# quasar-market-data
Independent Python market data service for Quasar. Collects, validates, and stores cryptocurrency market data using CCXT/CCXT Pro, with technical indicators powered by TA-Lib and REST APIs via FastAPI.

## Commands

Requires [uv](https://docs.astral.sh/uv/). Python 3.12 is pinned in `.python-version`.

| Command | Does |
|---|---|
| `uv sync` | Create the environment and install dev dependencies |
| `make test` | `uv run pytest` |
| `make lint` | `ruff check` and `ruff format --check` on `src tests spikes` |
| `make typecheck` | `mypy` (strict) |
| `make imports` | `lint-imports` (module boundaries) |
| `make check` | `lint`, `typecheck`, `imports`, `test` |
| `make live` | live check of `fetch-once` against Binance USDT-M public endpoints (uses the network; not part of `make check`) |

CI is configured to run `make check` on pull requests and pushes to `main` (not yet observed running). Making it an authoritative gate also needs a required status check on `main`, which is a repository setting not made here.

## Local hook

`.claude/settings.json` registers one Claude Code `PreToolUse` hook (matcher `Bash`) that runs `scripts/hooks/pre_commit_check.sh`. When the Bash command contains the text "git commit", it runs `make check` first and blocks the call (exit 2, tail of the output on stderr) if it fails. The hook runs `make check` on the working tree (not the staged content), so it also blocks WIP commits while the tree is red. It matches the text `git commit` anywhere in the command payload, so unrelated commands that mention it cost one `make check`, and forms such as `git -c k=v commit` are not matched. This is a local convenience only. Override the command with `QUASAR_CHECK_CMD` (used by `tests/test_pre_commit_hook.py`).

`storage` holds the SQLite layer shared by both processes: connection factories (`connection.py`), the schema identity and read-side check the API uses (`schema.py`), and the numbered SQL migrations with the runner that only the Collector imports (`migrate.py`). The schema is specified in `docs/adr/0002-sqlite-schema-v1.md`.

`collector` holds `CandleWriter` and `AsyncCandleWriter`: they persist validated candles idempotently and atomically, and record a re-delivered candle that differs from the stored one in `candle_conflicts` (the stored candle is never changed). The writer does not check that a candle is closed; `fetch_once` (`fetch_once.py`) is the path that passes it only confirmed closed candles (ADR-0001), reading from a `KlineSource`. `binance_usdm.py` is the only module that imports CCXT (async client, raw endpoints). The API cannot import any of this.

Console scripts: `quasar-collector` and `quasar-api` (`serve` runs the read-only HTTP API; see below).

### `quasar-api serve`

```
quasar-api serve --db market.db [--host 127.0.0.1] [--port 8000]
                 [--publication-delay-ms N] [--lag-threshold-ms N]
```

Serves `GET /v1/candles` and `GET /v1/coverage` as specified in `docs/contracts/api-v1.md`. It binds to localhost by default. `--publication-delay-ms` and `--lag-threshold-ms` set the expectation rule that `GET /v1/coverage` classifies slots with (defaults 15000 and 60000 ms; at most 300000 and 900000); an out-of-bound or non-integer value is a usage error (exit 2) and the server does not start. The process only reads: it opens the database read-only on every request and never creates, migrates or writes it. `--db` need not exist yet: until the Collector has created and migrated the file every request answers `503 not_ready` (`database_missing`, then `schema_mismatch` for a freshly created or unmigrated file, which carries no `Retry-After`), and the API recovers on its own when the file is fixed. A missing, replaced or unreadable file is noticed on the next request. Stop it with Ctrl-C (SIGINT).

### `quasar-collector fetch-once`

```
quasar-collector fetch-once --db market.db --symbol BTC/USDT:USDT [--limit 100] [--publication-delay-ms 15000]
```

Fetches the most recent 5m klines of one Binance USDT-M perpetual (public endpoints, no credentials), migrates the database on first use (the directory of `--db` must exist), stores only confirmed closed candles and prints three `key=value` lines (`symbol`/`fetched`, `time`, and `inserted`/`duplicates`/`conflicts`/`pending`/`invalid`). The still-forming candle is `pending` and is never stored. Re-running is safe: stored candles are `duplicates`; a revised value is recorded in `candle_conflicts` and never overwrites.

| Exit | Meaning |
|---|---|
| `0` | every eligible candle stored; nothing invalid or conflicting |
| `3` | eligible candles stored, but invalid candles or conflicts were found (see the output) |
| `1` | failed; nothing was written, with certainty (source error, malformed batch, untrusted server time, schema or directory problem) |
| `4` | the write's outcome is unknown (the batch is stored completely or not at all); re-run the same command to reconcile |
| `130` | interrupted; whether the last batch was stored is unknown; re-run to reconcile |
| `2` | usage error |

## Tool scope

| Tool | Covers | Does not cover |
|---|---|---|
| `pytest` | `tests/` only (`testpaths`), `live` marker excluded, `--strict-markers` | `spikes/` |
| `ruff check` / `ruff format --check` | `src/`, `tests/`, `spikes/` | — |
| `mypy` (strict) | `src/`, `tests/` | `spikes/` |
| `lint-imports` | package `quasar_market_data` | `tests/`, `spikes/` |
| `make live` | `tests/live/` (marker `live`), run by hand | `make check` |

## Boundary enforcement

`domain` is pure: it may import only the stdlib modules in `ALLOWED_EXTERNAL_IMPORTS` (`tests/test_domain_purity.py`: `__future__`, `collections`, `dataclasses`, `decimal`, `enum`, `re`, `typing`) and itself. Adding a module means editing that constant, visible in review. `collector` and `api` must never import each other.

| Guard | Catches | Does not catch |
|---|---|---|
| AST allowlist test (`tests/test_domain_purity.py`) | every direct `import` / `from … import` in `domain/` (also inside functions), relative imports with level ≥ 2, calls to `open`, `__import__`, `eval`, `exec`, `compile` | dynamic imports with computed names; transitive chains (direct-import check only) |
| import-linter `forbidden` contract `domain is pure` | `domain` reaching `collector`, `api`, `storage`, `ccxt`, `fastapi`, `talib` or `sqlite3`, directly or through other modules in the graph | what happens inside third-party package internals |
| import-linter `forbidden` contract `storage is a leaf below collector and api` | `storage` reaching `collector`, `api`, `ccxt`, `fastapi` or `talib` | — |
| import-linter `forbidden` contract `only the collector migrates` | `api` reaching `storage.migrate`, directly or through other modules | — |
| import-linter `forbidden` contract `ccxt is imported only by the Binance adapter` | a static `import ccxt` (or `ccxt.async_support`) in any `api` or `collector` module except `collector/binance_usdm.py`; the single exemption is the adapter's own edge to `ccxt` (negative tests in `tests/test_import_contracts_negative.py`) | dynamic imports, other HTTP clients, `tests/`, `spikes/` |
| import-linter `forbidden` contract `api reaches no network client` | a static import of `httpx`, `requests`, `aiohttp`, `websockets`, `socket` or `ssl` in `api` | `urllib.request`, `http.client` and other modules it cannot name (covered by the AST scan in `tests/api/test_read_only.py`), dynamic imports, network access from other packages, the deployment (S21) |
| import-linter `independence` contract | `collector` ↔ `api` imports, direct or via other graph modules | — |

The import-linter contracts are proven by automated negative tests (`tests/test_import_contracts_negative.py`): each case copies the package and `pyproject.toml` into a temp directory, injects a violating module into the copy and asserts that `lint-imports` fails naming the broken contract. The original manual probes (temporary files, since removed):

- **P1** `domain/_probe_direct.py` with `import sqlite3`: both `make imports` (forbidden contract) and the AST test failed.
- **P2** `domain/_probe_indirect.py` importing a helper outside `domain` that imports `collector.cli`: `make imports` reported the chain `domain._probe_indirect -> _probe_helper -> collector.cli`; the AST test failed on the first import (`from quasar_market_data import _probe_helper`).
- **P3** `import quasar_market_data.api.cli` added to `collector/cli.py`: only `make imports` (independence contract) failed. The AST test covers `domain/` only and does not look at `collector`.

All forbidden entries (including stdlib `sqlite3` and not-installed `ccxt`, `fastapi`, `talib`) were accepted by import-linter without errors.
