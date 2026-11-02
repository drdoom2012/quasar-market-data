# quasar-market-data
Independent Python market data service for Quasar. Collects, validates, and stores cryptocurrency market data using CCXT/CCXT Pro, with technical indicators powered by TA-Lib and REST APIs via FastAPI.

## Install and run

Two processes, the Collector (the only writer) and the API (read-only), share one SQLite database and one configuration file (`config/quasar.toml`, see "Configuration").

### 1. Locally

Without Docker. Requires [uv](https://docs.astral.sh/uv/); Python 3.12 is pinned in `.python-version`.

```sh
uv sync
uv run quasar-collector run config/quasar.toml   # terminal 1: collects, writes data/quasar.db
uv run quasar-api serve config/quasar.toml       # terminal 2: API on http://127.0.0.1:8000
curl localhost:8000/health
```

With Docker (images from GHCR, `linux/amd64`):

```sh
mkdir -p data/docker                             # database folder, writable for UID 10001
docker compose -f compose.yaml -f compose.local.yaml up -d   # pulls the images; API on 127.0.0.1:8000
```

`compose.local.yaml` is for local use only (see "Local data folder" under Deployment); add `-f compose.build.yaml` before it to build the images from this checkout. Optional page over the API: `uv run python -m tools.symbol_overview`, then open `http://127.0.0.1:8765/`.

### 2. On the VPS

```sh
# copy compose.yaml and config/quasar.compose.toml from the tagged commit (config/ next to compose.yaml)
docker login ghcr.io                             # only if the packages are private
docker compose pull && docker compose up -d
ssh -L 8000:172.31.250.10:8000 <vps>             # from your machine, then: curl localhost:8000/health
```

The API has no published port, hence the tunnel. Upgrade, release rule, backup and health: "Deployment (S21)".

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
| `make bench-indicators` | the worst-case `GET /v1/indicators` request on a generated database, in process and against a served process with 1, 2, 4 and 8 concurrent requests and a `/health` probe (informative, no criterion; not part of `make check`) |
| `make bench-health` | `/health/ingest` read benchmark on a generated database (`PROFILE=smoke`, `medium` or `target`; not part of `make check`; S17 HARD STOP 2) |
| `make live` | live check of `fetch-once` against Binance USDT-M public endpoints (uses the network; not part of `make check`) |
| `make images` | build the `collector` and `api` images for `linux/amd64` with local tags (needs Docker; not part of `make check`) |
| `make compose-test` | `make images`, then the `compose`-marked tests: images, the Compose file, and the isolation test against the running production project (needs a stock Linux Docker Engine, not OrbStack or Docker Desktop; not part of `make check`) |

CI is configured to run `make check` on pull requests and pushes to `main` (not yet observed running). Making it an authoritative gate also needs a required status check on `main`, which is a repository setting not made here. `images.yml` (S21) runs `make compose-test` on pull requests and pushes, and pushes the images to GHCR on pushes only (not yet observed running).

## Local hook

`.claude/settings.json` registers one Claude Code `PreToolUse` hook (matcher `Bash`) that runs `scripts/hooks/pre_commit_check.sh`. When the Bash command contains the text "git commit", it runs `make check` first and blocks the call (exit 2, tail of the output on stderr) if it fails. The hook runs `make check` on the working tree (not the staged content), so it also blocks WIP commits while the tree is red. It matches the text `git commit` anywhere in the command payload, so unrelated commands that mention it cost one `make check`, and forms such as `git -c k=v commit` are not matched. This is a local convenience only. Override the command with `QUASAR_CHECK_CMD` (used by `tests/test_pre_commit_hook.py`).

## Configuration

`config/quasar.toml` is the only source of operational values for `quasar-collector run` and `quasar-api serve`: no switch, no hidden default, no environment variable. Both processes read the same file once at start; a change takes effect after a restart. Every key is required, an unknown key or a wrong type is an error (exit `2`, nothing is opened). Relative `database.path` resolves against the file's directory. Docker uses `config/quasar.compose.toml`, identical except `[api].host`.

| Section | Keys |
|---|---|
| `[database]` | `path` |
| `[rule]` | `publication_delay_ms`, `lag_threshold_ms` |
| `[[venues]]` | `id`, `timeframes`, `symbols` (the universe is the venue, symbol and timeframe triples) |
| `[collector]` | `limit`, `max_concurrency`, `min_request_interval_ms` |
| `[collector.backfill]` | `enabled`, `days` |
| `[api]` | `host`, `port` |

Details are in [`docs/reference.md`](docs/reference.md): `quasar-api serve` and `quasar-collector run` / `fetch-once` (behaviour, exit codes), the API endpoints (instruments, health, indicators), restart reconcile, backfill and the benchmarks.

## Deployment (S21)

Two images, `ghcr.io/drdoom2012/quasar-market-data-collector` and `ghcr.io/drdoom2012/quasar-market-data-api` (`linux/amd64`, user `10001`), run as two services of `compose.yaml`. They share the volume `quasar-data` (mounted at `/app/data`, writable for both, because a WAL reader needs the shared-memory file) and read `config/quasar.compose.toml`, mounted read-only at `/app/config/`. That file must stay identical to `config/quasar.toml` except `[api].host = "0.0.0.0"` (`tests/config/test_compose_config.py`). The API stays read-only at the SQLite connection (INV-014); the Collector is the only writer.

**Network.** The `api` service is attached only to the network `internal` (`internal: true`, subnet `172.31.250.0/24`, fixed address `172.31.250.10`): it has no route out and no published port (a published port does not work on an internal network). Only the VPS itself reaches it; from your machine use an SSH tunnel:

```sh
ssh -L 8000:172.31.250.10:8000 <vps>     # then: curl localhost:8000/health
```

The `collector` is on `internal` and on `egress` (it needs the exchange). `make compose-test` checks this against the running project (INV-069); it proves network isolation of the deployment, not that API handlers never try to fetch exchange data (INV-013).

**Release rule.** `compose.yaml` pins `v<project.version>` (today `v0.1.0`). That tag exists in GHCR only after the git tag `v<project.version>` was pushed and its `images.yml` run succeeded; a push to `main` publishes only `main` and `sha-<commit>`. Before the first `docker compose pull`, check: both `images.yml` jobs green for the tag (`gh run list --workflow images.yml`), `docker buildx imagetools inspect ghcr.io/drdoom2012/quasar-market-data-<name>:v<version>` lists `linux/amd64` for both images, and `git show v<version>:compose.yaml` pins that tag. The H1 network checks on the VPS (S21 design Appendix B.2) also run before the first deployment.

**First start on the VPS.** Copy `compose.yaml` and `config/quasar.compose.toml` from the tagged commit (keeping `config/` next to `compose.yaml`), `docker login ghcr.io` with a read-only token if the packages are private, then `docker compose pull && docker compose up -d`. **Upgrade:** bump `project.version` and both image tags in `compose.yaml` in one commit, push it, tag that commit `v<version>` and push the tag, wait for the green `images.yml` run, then `docker compose pull && docker compose up -d` on the VPS. Nothing is built on the VPS.

**Local data folder.** To keep the database in a host folder instead of the volume `quasar-data`, add `compose.local.yaml`: `docker compose -f compose.yaml -f compose.local.yaml up -d` (add `-f compose.build.yaml` before it for locally built images). It mounts `./data/docker` at `/app/data` in both services (the folder must be writable for UID 10001) and publishes the API on `127.0.0.1:8000`, so no SSH tunnel is needed. To publish a port the API also joins an ordinary bridge network, which gives it a route out that `compose.yaml` withholds: use this file for local work only, never on the VPS.

**Health.** Both services restart unless stopped. The Collector's healthcheck only checks that its main process exists: a dead main process already stops the container (and the restart policy starts it again), Docker does not restart an unhealthy container, and a stuck or idle Collector stays `healthy`. Ingestion shows in `/health/ingest` (series go `due`, then `stale`) and in the Collector's `round` lines (`docker compose logs collector`); neither names the cause (F-16). Logs rotate (`json-file`, 10 MB x 3 per service); memory limits are set in `compose.yaml` with the measurement they come from.

**Backup.** The database is live WAL: copy it with SQLite's backup API, never by copying files while the Collector runs (the main file and its `-wal` file cannot be copied atomically). The images have no `sqlite3` command-line tool:

```bash
docker compose exec collector python -c "import sqlite3; s = sqlite3.connect('/app/data/quasar.db'); d = sqlite3.connect('/app/data/backup.db'); s.backup(d); d.close(); s.close()"
docker compose cp collector:/app/data/backup.db ./quasar-backup.db
docker compose exec collector rm /app/data/backup.db
```

**Schema upgrade (version 2).** The release that ships OI-2 adds the open interest tables (ADR-0003 P-6, P-14). The Collector migrates on start; the API serves only the schema it was built for. Procedure: (1) take a backup as in "Backup" above; (2) `docker compose pull && docker compose up -d`; (3) wait until `curl localhost:8000/health/ready` (through the tunnel) answers `200` with `"schema_version":2` and `docker compose logs collector` shows `migrated to version 2 (from 1)`. Until both have happened the API answers `503 not_ready` (`schema_mismatch`); expected seconds to a minute, not measured. (4) **There is no rollback by redeploy:** a Collector of the previous release refuses the newer database ("newer than the code"). Going back means restoring the backup with the previous release, which loses the candles collected since.

**Open interest write benchmark (ADR-0003 P-9).** Measures the 100-row open interest transaction on the disk that holds the database (criterion: p99 ≤ 50 ms) without installing anything and without touching the production database or its volume. On the VPS: copy `tools/bench_open_interest_write.py` and `src/quasar_market_data/storage/migrations/002_open_interest.sql` from the release commit into one directory; find the volume's filesystem (`docker volume ls` shows its full name, usually prefixed with the Compose project; `docker volume inspect <name>` → `Mountpoint`, then `df <Mountpoint>`); create an empty directory on that filesystem outside the volume; then, right after a Collector `round` line:

```bash
docker run --rm -v <empty dir>:/bench -v "$PWD":/src:ro python:3.12-slim \
  python /src/bench_open_interest_write.py --schema /src/002_open_interest.sql --dir /bench/run --max-p99-ms 50
```

It prints one `oi_write …` line and removes its files. Locally: `make bench-oi-write` (on macOS the number is optimistic: SQLite does not force the write to the medium there).

## Symbol overview (tool)

A local, read-only page that shows which symbols the API serves, where each history starts, how complete the last 30 days are, and whether a symbol is fine or something is wrong with it. It is a separate consumer of the HTTP API (`tools/symbol_overview/`, standard library only, no dependency on `quasar_market_data`); the Collector, the API, the database and the contracts are unchanged. Design: `docs/superpowers/specs/2026-11-01-symbol-overview-design.md`.

```
uv run python -m tools.symbol_overview --api-url http://localhost:8000 --port 8765
```

Open `http://127.0.0.1:8765/`. `--api-url` defaults to `http://localhost:8000` and `--port` to `8765`; the page binds `127.0.0.1` only. To look at the VPS, open the SSH tunnel from the header of `compose.yaml` (`ssh -L 8000:172.31.250.10:8000 <vps>`) and run the same command. The tool is not part of the images or of `compose.yaml`.

It calls only `GET /health/ingest`, `GET /v1/instruments` and `GET /v1/coverage`, and only while a page is open: one refresh at a time, at most four calls at once, the result is reused for 15 s, the page asks again every 60 s. A refresh is 2 calls plus one `/v1/coverage` per symbol with stored data.

States (the first that applies is the headline; all reasons are listed): `no data` (nothing stored), `cannot judge` (an answer failed validation, an API call failed, or the server clock is ahead of the stored data), `stale` (the API reports `stale` slots), `conflict`, `holes` (missing slots after the start of the history, or slots the exchange confirmed as having no data), `behind` (`due` slots), `ok`. If `/health/ingest` or `/v1/instruments` fails, the page shows one error and no states, never an empty or green page. Time thresholds come from the API (its `rule` and clock); the tool defines none.

Limits: the API cannot tell a symbol whose first candle is not yet expected from one that should have been downloaded (both are `no data`); the length of a history is shown but not judged (a symbol with a short history and no holes is `ok`); `ok` is true at the API's time of that answer. The load of `/v1/coverage` for hundreds of symbols has not been measured: the constants (4 calls at once, 10 s per call, 60 s per refresh, 15 s cache) are placeholders until it is. `tools/` is covered by ruff, mypy and pytest, not by `lint-imports`; `tests/tools/symbol_overview/test_boundary.py` keeps it to the standard library.

## Tool scope

| Tool | Covers | Does not cover |
|---|---|---|
| `pytest` | `tests/` only (`testpaths`), `live` marker excluded, `--strict-markers` | `spikes/` |
| `ruff check` / `ruff format --check` | `src/`, `tests/`, `spikes/`, `tools/` | — |
| `mypy` (strict) | `src/`, `tests/`, `tools/` | `spikes/` |
| `lint-imports` | package `quasar_market_data` | `tests/`, `spikes/`, `tools/` (the symbol overview tool is held to the standard library by the AST test `tests/tools/symbol_overview/test_boundary.py`) |
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
