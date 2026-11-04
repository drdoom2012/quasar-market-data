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
| `make bench-overview` | one refresh of the symbol overview against a served API on a generated database (`SERIES`, `OVERVIEW_PROFILE=smoke` or `target`, `OI_MODE=proxy` or `real`, `ARGS`; not part of `make check`; see "Overview refresh benchmark") |
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

## Open interest requests (ADR-0003)

`quasar-collector fetch-oi-once --db market.db --symbol BTC/USDT:USDT [--periods 3]` fetches the newest 5m open interest rows of one symbol (Binance USDT-M, `GET /futures/data/openInterestHist`, public, no credentials) and stores the eligible ones; details and exit codes are in [`docs/reference.md`](docs/reference.md). `quasar-collector run` collects open interest continuously when the configuration has an enabled `[open_interest]` table (below); without it nothing else uses the endpoint.

**Operational requirement: nothing else on the IP may call `/futures/data/*` more than 600 times per 5 minutes.** Binance documents "IP rate limit 1000 requests/5min" for the endpoint and does not say whether all `/futures/data/*` callers share it. The open interest adapter counts its own request starts (history and server time together) in a sliding 300 s window, at most `budget_per_5min` (400 by default, 40 % of 1000), and has at most 2 requests in flight. A local limiter cannot see other consumers, so the remaining 60 % is reserved for anything else on the IP: do not run a second Collector with open interest enabled, and know what else calls `/futures/data/*` from that address.

**Continuous collection (OI-4).** Add to `config/quasar.toml` (absent or `enabled = false` leaves `run` exactly as before; every key is required, unknown keys are an error):

```toml
[open_interest]
enabled = true
period = "5m"
publication_delay_ms = 15000   # a row is stored only when its period end + this is behind the server-time lower bound
round_offset_ms = 200000       # 60000..280000: the round for boundary B wakes at B + this
budget_per_5min = 400          # 100..400 request starts per 300 s (history and server time together)
```

`run` starts one extra task after the first candle round and its post-check. Each symbol is asked every `c` rounds (`c` is derived from the number of symbols and the budget: 10 symbols give `c = 1`, 150 give 2 at 400, 600 give 5 at 400 and 9 at 200; a configuration that needs `c > 12` is refused at start) with a range that overlaps the previous request, so a row published late is picked up by the next request without a retry. Between rounds the task fills holes of the stored history (at most 29 days back) in pages of up to 500 period ends, oldest first, only while the next round keeps its budget; a slot Binance answers without its row is asked at most three times per run (after 1 h and 6 h) and never recorded as unavailable. The task never changes the exit status of `run` and never delays candles: an error in it prints one `oi-error` line and ends only the task (restart the Collector to restart it); a `418` ends it too.

Log lines (stdout unless noted): `oi-reconcile` (per series and a total, once at start: `stored`, `holes`, `no_longer_recoverable` = unstored slots between the first stored row and the 29-day horizon, `oldest_hole_leaves_in_ms`), `oi-round` (per round), `oi-catchup` (per idle window that did work), `oi-conflict` (a re-delivered row with a different value, once per new conflict), `oi-measurement` (hourly), and on stderr `oi-warning` and `oi-error`. **Soak (ADR-0003 amendment A-1):** read the hourly `oi-measurement` line: `oi100_p99_ms` (p99 of a transaction of exactly 100 rows) must be at most 50 ms, the candle write p99 (`candle_p99_ms`) with open interest on at most 100 ms above the value without it, and the candle `lateness_ms` p99 at most 1 000 ms above the value without it. Enabling it on the VPS is a configuration change plus a release; nothing in the repository turns it on.

**Reading it (OI-5, contract 6.5).** `GET /v1/open-interest?venue=binance-usdm&symbol=BTC/USDT:USDT&period=5m&start=<ms>[&end=<ms>][&limit=<n>]` returns the stored 5m rows with the paging and limits of `/v1/candles`. **`time_ms` is the END of the period**: the row with `time_ms = T` belongs to the candle that opened at `T - 5m`, so the candle opened at `O` joins at `start = O + 5m`; never join by equal timestamps. Values are the decimal strings Binance sent (units are inferred, not documented: `open_interest` in the base asset, `open_interest_value` in USDT). Every slot of the page has one of seven states: `present`, `conflicted` (the row is returned with its first stored values, flagged), `not_yet_expected` (`T + c x 5 min + round_offset_ms` is after `as_of_ms`), `no_longer_recoverable` (older than 29 days, never requested), `due` / `stale` (no row, after the newest stored one; `rule.lag_threshold_ms` splits them) and `missing`; `rule` in the body states what they were computed with, derived from the same `config/quasar.toml` as the Collector (compare the `sha256` both print at start). With a defaulted `end` and a `start` at or before the newest stored row the window stops at that row, so a stopped series still answers `gap_free: true`: read freshness in `/health/ingest` or pass an explicit `end` (a `start` after the newest row gives one slot judged by the clock). An unaligned stored `time_ms` (impossible through the Collector) is a `500`, never a wrong count. Without an enabled `[open_interest]` table the endpoint answers `404 open_interest_disabled` and the health object is `null`. `GET /health/ingest` gains the last key `open_interest` (`status` `no_series`, `degraded` (a conflict in a slot of the last 24 h, or an unjudged series), `behind`, `catching_up`, `current`; counts; `conflicted_series`; `behind` list). `make bench-health-oi` times it with 600 series (on the development machine, 8 352 rows per series: median 4.6 ms with open interest off, 7.6 ms on; not a cold-disk or VPS figure).

**Reading funding rates (FR-5, contract 6.7).** `GET /v1/funding-rates?venue=binance-usdm&symbol=BTC/USDT:USDT&start=<ms>[&end=<ms>][&limit=<n>]` returns the stored settlements. **`funding_rate` is the rate of one settlement, not per day or per year, and the interval it applies to is not part of the row** (a symbol settles every 8, 4 or 1 hour and this has changed over time): the spacing of two rows is an observation, wrong across a gap; this API computes no cost or average. `funding_time_ms` is an instant, a few milliseconds after a whole hour in all measured data; never join it to a candle by equal timestamps. `mark_price` is `null` for settlements before 2023-10-31. The window is the closed interval `[start, end]` of unaligned instants (at most 3 650 days; a defaulted `end` is now, capped and echoed). The `coverage` block answers "how much of **my window** did the Collector verify?": `window_state` is `verified` when the whole window lies inside the verified span, `partly_verified` or `unverified` otherwise, and `complete` is true only for a verified window with no conflicted event. It never says a settlement is missing or that none occurred, and a window with a defaulted `end` is at best `partly_verified`: ask with an explicit `end <= coverage.verified_through_ms` for a complete answer. The first page describes your window; a continuation with `start = next` describes the shorter window `[next, end]`. Without an enabled `[funding_rate]` table the endpoint answers `404 funding_rate_disabled` and the health object is `null`; `GET /health/ingest` gains the last key `funding_rate` (`behind`, `catching_up`, `quiet_series` (no event for 48 h, or no row), `conflicted_series`). With both extension datasets enabled one `ExtensionPause` stops both after a `429` or `418` seen by either; each keeps its own budget.

**Gaps as runs (OI-6a, contract 6.6).** `GET /v1/open-interest/coverage?venue=binance-usdm&symbol=BTC/USDT:USDT&period=5m&start=<ms>&end=<ms>` (all five parameters are required, at most 10 000 slots) returns the counts of the seven slot states of the window and the maximal runs of consecutive slots that are not `present`, each with `state`, `start`, `end` (exclusive; values are `time_ms`, so the **period end**) and `slots`. It reads stored times only, never values. The counts and the runs describe the same slots, so they always agree, and for a window that fits one page they equal the `coverage` counts of `/v1/open-interest`. It cannot say when collection began (the oldest stored row is not that). A conflict record without a stored row (impossible through the Collector) is reported as `conflicted`. Errors and their order are those of `/v1/open-interest`, including `404 open_interest_disabled`.

**429 and 418.** A `429` pauses every open interest request of the process for `max(Retry-After, 300 s)`, doubling on consecutive pauses up to 1 hour (a larger `Retry-After` is honoured). A `418` stops open interest until the process restarts and logs an error. Candle collection has its own client and limiter and is not paused by either. Whether a `418` caused by open interest would also ban klines for the IP is not known; the budget exists to make it unlikely.

## Funding rate requests (ADR-0004)

`quasar-collector fetch-funding-once --db market.db --symbol BTC/USDT:USDT [--hours 24]` fetches one page of funding rate history of one symbol (Binance USDT-M, `GET /fapi/v1/fundingRate`, public, no credentials) and stores the eligible rows. It **stores rows and claims no coverage**: the verified span is written only by the funding task (FR-4). Output lines and exit codes are in [`docs/reference.md`](docs/reference.md).

**Operational requirement: nothing else on the IP may call `fundingRate` or `fundingInfo` more than `500 - b` times per 5 minutes** (default `b = 150`, so 350). Binance documents "500/5min/IP" for both endpoints together and sends no used-weight header. The funding source counts its own request starts (history and server time together) in a window of 305 s (the documented 300 s plus a 5 s guard), at most `b` of them (50..250), split into two shares that are never lent: rounds and seeds at most `floor(b / 2)`, catch-up and history together the rest. It has at most 2 requests in flight and never retries a failed request itself. A local limiter cannot see other consumers: do not run a second Collector with funding enabled, and know what else calls these endpoints from that address.

**429 and 418.** A `429` pauses every funding request of the process for `max(Retry-After, 300 s)`, doubling on consecutive pauses up to 1 hour (a larger `Retry-After` is honoured). A `418` stops funding until the process restarts. This is one in-process `ExtensionPause` that funding sets and honours. Candle collection has its own client and limiter and neither reads nor is paused by it. Until amendment A-3 to ADR-0003 is applied, open interest keeps its own gate, so a `429` on funding does not stop open interest.

**Continuous collection (FR-4).** Add to `config/quasar.toml` (absent or `enabled = false` leaves `run` exactly as before and creates nothing; every key is required, unknown keys are an error):

```toml
[funding_rate]
enabled = true
publication_delay_ms = 30000   # 0..595000: a row is stored only when its funding time + this is behind the server-time lower bound
round_offset_ms = 60000        # 60000..600000, at least publication_delay_ms + 5000: the round for hour B wakes at B + this
budget_per_5min = 150          # 50..250 request starts per 305 s window (history and server time together)
backfill_days = 365            # 1..3650: how far back the verified span grows
recheck_ms = 86400000          # 3600000..604800000: how far back an hourly check looks, to pick up a late-published row
```

`run` starts one more task after the first candle round and its post-check. Every hour, at `B + round_offset_ms`, one round asks every series once, the oldest verified first (an unseeded series is seeded first, and a series the previous round did not reach comes first because it is the oldest); a round ends 15 s before the next hour and a series it did not reach is first in the next round and is never marked failed. A series that is behind after a restart (its verified span ends more than 2 h before the bound) is caught up with continuation requests from `through + 1`; after an outage without a restart the next hourly request covers `recheck_ms` before the span end up to the bound and continues page by page when the answer is truncated; the history behind `from` is filled backwards in chunks of at most 90 days up to `backfill_days`. The two shares of the budget are never lent, so catch-up and history cannot starve the hourly rounds and the reverse. A failed request is retried after 1 s and 2 s; three consecutive failures of a series suspend it for 6 h, then 12 h, then 24 h, with one probe request when a suspension ends; a restart clears the suspensions. A row that appears inside the verified span is stored, counted and logged as a late event, never ignored. Capacity: the sustainable number of series per round at the default offset of 60 s is 274 at `b = 50`, 824 at 150 and 1 374 at 250; `run` refuses a larger enabled configuration at start and warns above 90 % of it. Enabling funding together with open interest is a deployment decision (schema version 3); `run` then prints one `funding-budget funding=<b> open_interest=<b or off>` line so the operator sees both budgets, and both stay below what the IP may send.

Log lines (stdout unless noted): `funding-reconcile` (per series and a total, once at start), `funding-round` (per round: `asked`, `failed`, `skipped`, `suspended`, `skipped_rounds`, `late_ms`), `funding-probe`, `funding-conflict` (a re-delivered row with a different value), `funding-suspended`, `funding-measurement` (hourly and once at stop), and on stderr `funding-warning` and `funding-error` (a `418`, an unusable writer or a failing start-up read ends only this task: restart the Collector to restart it). The task never changes the exit status of `run` and never delays candles. **Soak reading rule:** in the hourly `funding-measurement` line `funding100_p99_ms` (a transaction of exactly 100 rows) must be at most 50 ms, the candle write p99 with funding on at most 100 ms above the value without it, and `lateness_ms` of the candle rounds at most 1 000 ms above the value without it. Capacity under injected failures is checked by simulations: the cases for `b = 50` run in `make check`, the larger budgets and the 200-round run only under `make simulate` (marker `simulation`); they model instant writes (evidence E-14). Enabling it on the VPS is a configuration change plus a release; nothing in the repository turns it on.

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

**Schema upgrade (versions 2 and 3).** The release that ships open interest and funding rate adds the open interest tables (ADR-0003 P-6, P-14) and the funding rate tables (ADR-0004 P-6, P-14). Open interest is released only together with funding, so a production database goes from version 1 to version 3 in one start. The Collector migrates on start; the API serves only the schema it was built for. Procedure: (1) take a backup as in "Backup" above; (2) `docker compose pull && docker compose up -d`; (3) wait until `curl localhost:8000/health/ready` (through the tunnel) answers `200` with `"schema_version":3` and `docker compose logs collector` shows `migrated to version 3 (from 1)` (a database that was already moved to version 2 shows `(from 2)`). Until both have happened the API answers `503 not_ready` (`schema_mismatch`); expected seconds to a minute, not measured. (4) **There is no rollback by redeploy:** a Collector of the previous release refuses the newer database ("newer than the code"). Going back means restoring the backup with the previous release, which loses the candles collected since.

**Open interest write benchmark (optional).** Measures the 100-row open interest transaction on the disk that holds the database without installing anything and without touching the production database or its volume. The ADR-0003 P-9 criterion (p99 ≤ 50 ms) is checked from the Collector's hourly measurement line once open interest collection runs (OI-4, amendment A-1); this benchmark is a quick check before that. On the VPS: copy `tools/bench_open_interest_write.py` and `src/quasar_market_data/storage/migrations/002_open_interest.sql` from the release commit into one directory; find the volume's filesystem (`docker volume ls` shows its full name, usually prefixed with the Compose project; `docker volume inspect <name>` → `Mountpoint`, then `df <Mountpoint>`); create an empty directory on that filesystem outside the volume; then, right after a Collector `round` line:

```bash
docker run --rm -v <empty dir>:/bench -v "$PWD":/src:ro python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337 \
  python /src/bench_open_interest_write.py --schema /src/002_open_interest.sql --dir /bench/run --max-p99-ms 50
```

The image is the Dockerfile's pinned runtime base, so SQLite is the production library. It prints one `oi_write …` line and removes its files. Locally: `make bench-oi-write` (on macOS the number is optimistic: SQLite does not force the write to the medium there).

**Overview refresh benchmark (optional).** `make bench-overview SERIES=300 OVERVIEW_PROFILE=target OI_MODE=proxy` times what one refresh of the symbol overview costs the API (OI-6 design, section 7): it generates a database (never production data) in a fresh temp dir, starts `quasar-api serve` on 127.0.0.1 with open interest enabled, and reproduces the overview tool's call order (`/health/ingest`, `/v1/instruments`, a candle phase of one `/v1/coverage` call per symbol, then an open interest phase), 4 workers, a new connection per call, the 60 s deadline checked before every call. It prints one JSON line: per-call latency per endpoint, the wall time of each refresh, the acceptance criteria AC1 to AC7 and, unless `ARGS="--sustained-minutes 0"`, the sustained run (refreshes every 15 s for 10 minutes with a concurrent writer and a `/health/ready` probe; AC4, AC5). `OI_MODE=proxy` asks `/v1/coverage` for the open interest slot and is the G0 stand-in used before `/v1/open-interest/coverage` exists. `ARGS="--l-tunnel-ms <n>"` takes the one measurement only you can make: the median of at least 20 trivial calls (for example `/health/ready`) through your real SSH tunnel; it turns the loopback numbers into the prediction AC7. `target` at 600 symbols builds about 60 million candle rows (tens of minutes, several GB of temp disk). One machine, a warm file cache, a modelled writer: not a VPS figure.

## Symbol overview (tool)

A local, read-only page that shows which symbols the API serves, where each history starts, how complete the last 30 days are, and whether a symbol is fine or something is wrong with it. It is a separate consumer of the HTTP API (`tools/symbol_overview/`, standard library only, no dependency on `quasar_market_data`); the Collector, the API, the database and the contracts are unchanged. Design: `docs/superpowers/specs/2026-11-01-symbol-overview-design.md`.

```
uv run python -m tools.symbol_overview --api-url http://localhost:8000 --port 8765
```

Open `http://127.0.0.1:8765/`. `--api-url` defaults to `http://localhost:8000` and `--port` to `8765`; the page binds `127.0.0.1` only. To look at the VPS, open the SSH tunnel from the header of `compose.yaml` (`ssh -L 8000:172.31.250.10:8000 <vps>`) and run the same command. The tool is not part of the images or of `compose.yaml`.

It calls only `GET /health/ingest`, `GET /v1/instruments`, `GET /v1/coverage` and `GET /v1/open-interest/coverage`, and only while a page is open: one refresh at a time, at most four calls at once, the result is reused for 15 s, the page asks again every 60 s. A refresh is 2 calls plus one `/v1/coverage` per symbol with stored data, then (when open interest is enabled) one `/v1/open-interest/coverage` per symbol; one 60 s deadline covers both parts and no call starts after it.

States (the first that applies is the headline; all reasons are listed): `no data` (nothing stored), `cannot judge` (an answer failed validation, an API call failed, or the server clock is ahead of the stored data), `stale` (the API reports `stale` slots), `conflict`, `holes` (missing slots after the start of the history, or slots the exchange confirmed as having no data), `behind` (`due` slots), `ok`. If `/health/ingest` or `/v1/instruments` fails, the page shows one error and no states, never an empty or green page. Time thresholds come from the API (its `rule` and clock); the tool defines none.

**Open interest (OI-6b, design `docs/superpowers/specs/2026-11-03-oi-6-open-interest-overview-design.md`).** When `/health/ingest` reports an `open_interest` object, every symbol gets a second strip and the summary a second row. The second part of a refresh asks `/v1/open-interest/coverage` for the same 30 days shifted by one period (an open interest slot is the **end** of its period, so bucket `k` is the same hour in both strips) and starts only after every candle answer is final: whatever open interest costs, the candle states, reasons, strips and tiles are those of a candle-only refresh. The states are the candle ones with these differences. `_` on the open interest strip means "before the first stored row", **not** "before collection started": the API cannot tell a late start from a catch-up that did not reach back far enough, and slots without a row before the first row are never problems. `r` (no longer recoverable) marks slots inside the stored history that have no row and are older than the retrieval horizon (29 days), so they can no longer be fetched; it is information, not a problem: a symbol with only that finding stays `ok`. `ok` does not say the history is long enough, and a hole is a hole in the API's data (Binance omits periods without trades; the API cannot tell that from a lost row). The problem list shows the candle state and the open interest state of a symbol and prefixes each reason with `candles:` or `open interest:`; a symbol is listed when either is a problem, and the `ok` group holds the symbols where both are `ok`. Without an enabled `[open_interest]` the page says so and makes no open interest call; if the API reports it in a form the tool cannot use, open interest is `cannot judge` for every symbol and never green. Symbols the 60 s deadline did not reach are `cannot judge` for open interest only, and the page names the counts (`refresh.candle_not_asked`, `oi_deadline_hit`, `oi_not_asked`). When the candle part alone uses the whole 60 s, no open interest call is made at all: every symbol is then `cannot judge` for open interest and listed as a problem, with a line saying so, because nothing says it is fine. `r` can also appear on the strip after a symbol's newest row (a series that stopped more than 29 days ago): it is shown for those slots but is not a reason; the reason is `stale`. The overview object only gains keys.

**Checking the page.** `tests/tools/symbol_overview/` covers the validation, the judging, the refresh and the page's decision functions (which run under Node.js when `node` is installed; the test is skipped otherwise). Nothing automatic covers layout, colors and tooltips: open the page once against a database with a symbol with a partial history, one with a hole, one with a hole older than 29 days, one never stored, and once with open interest disabled (`tests/tools/symbol_overview/test_oi_against_real_api.py` seeds exactly those five cases).

Limits: the API cannot tell a symbol whose first candle is not yet expected from one that should have been downloaded (both are `no data`); the length of a history is shown but not judged (a symbol with a short history and no holes is `ok`); `ok` is true at the API's time of that answer. The load of one refresh for 300 and 600 symbols was measured on loopback only (`make bench-overview`, design section 14: the tool's refresh took 7 to 30 s, its memory 28 to 44 MiB, and the overview object the page downloads is about 1.2 MB at 600 symbols); the prediction for an SSH tunnel is open until the per-call latency of the tunnel is measured, and the constants (4 calls at once, 10 s per call, 60 s per refresh, 15 s cache) are unchanged. `tools/` is covered by ruff, mypy and pytest, not by `lint-imports`; `tests/tools/symbol_overview/test_boundary.py` keeps it to the standard library.

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
