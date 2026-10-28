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

`collector` holds `CandleWriter` and `AsyncCandleWriter`: they persist validated candles idempotently and atomically, and record a re-delivered candle that differs from the stored one in `candle_conflicts` (the stored candle is never changed). The writer does not check that a candle is closed; `fetch_once` (`fetch_once.py`) is the path that passes it only confirmed closed candles (ADR-0001), reading from a `KlineSource`. `binance_usdm.py` is the only module that imports CCXT (async client, raw endpoints). The collection loop (`collection_loop.py`) calls `fetch_once` per symbol on the boundaries planned by `domain/schedule.py`; `rate_limiter.py` spaces adapter calls and `clock.py` holds the injected clocks. The API cannot import any of this.

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

### `quasar-collector run`

```
quasar-collector run --db market.db --symbol BTC/USDT:USDT [--symbol ETH/USDT:USDT ...] [--limit 100] \
  [--publication-delay-ms 15000] [--max-concurrency 4] [--min-request-interval-ms 100] \
  [--backfill-days 30 | --no-backfill]
```

Collects continuously until SIGINT or SIGTERM: on every UTC 5m boundary plus the publication delay it calls `fetch-once` logic for each symbol (so only confirmed closed candles are stored), at most `--max-concurrency` symbols at once. A target slot that is not yet confirmable is retried up to 6 times (1, 2, 4, 8, 8 s); after that the symbol is reported `gave_up` and the next boundary round goes on. It prints one `round ...` line per round (`collected`, `conflicted`, `gave_up`, `stopped`, `write_uncertain`) and one indented line per symbol that is not plainly collected (a `None` count after a shutdown timeout is printed as `unknown`). The round collects the newest `--limit` rows; holes in the stored history are backfilled in the idle time between rounds (below).

**Start-up reconcile (S15).** Before any network call and before the writer opens, `run` reads the stored facts of every symbol on a read-only connection (no checkpoint table exists; the stored candles and records are the facts) and prints:

- `reconcile symbol=... window=... all_present=... clock_behind_stored=... history=... missing= due= stale= conflicted= unavailable= wanted_from= unreachable_tail= history_holes= holes_older_than_window= needs_s16= unknown_before= catch_up_limit=`, one line per symbol that is not fully present and hole-free, and `  hole START..END (raw)` lines for its largest holes;
- `reconcile summary symbols= window_gaps= not_all_present= due_slots= history_holes= needs_s16_slots= catch_up=`. `history_holes` and `needs_s16_slots` count slots, not runs. `history_holes` also includes holes inside the window, whose slots are also counted as window `missing`, so the two fields overlap and must not be added.

`window=window_no_gaps` means only that the recent window has no confirmed gap (`missing` or `stale`). `due`, `conflicted` and `unavailable` slots may still exist; `all_present` says whether any do and is `false` whenever one of them is above 0. `history=contiguous_from_oldest` means only "no hole between the oldest and the newest stored candle"; what exists before the oldest stored candle is unknown. No line says that history is complete. A gap older than the window stays reported at every later start (`holes_older_than_window`) for as long as its rows are absent. When the target is out of reach the domain reports `unreachable_tail=0`; the `window` status says so.

The first round sizes its klines request on every attempt from the stored absent slots and the clock, capped at the project's own `MAX_LIMIT` of 1000 rows (Binance's maximum was not verified). The catch-up therefore reaches at most `1000 - 1 - 2 = 997` slots before the forming candle (about 3.5 days of 5m). Slots the request cannot reach are counted as `needs_s16`; they are left to S16 (paging, `unavailable_candles`). A long `--publication-delay-ms` needs a larger `--limit`; a delay no round can serve (30000000 ms needs `--limit >= 104`) is a usage error before anything is opened.

After round one the database is read again and `reconcile-after symbol=... absent_before= history_holes= holes_older_than_window= needs_s16= unknown_before=` lines (plus `  still_missing SYMBOL START..END STATE` lines) and `reconcile-after summary ... clock_lead_slots_max=` state what the database holds. Recovery is only ever shown by this second read; `absent_before` is a comparison value, not a claim. In `reconcile-after`, `clock_behind_stored` can read `true` with a correct clock when round one crosses a slot boundary and later symbols store the next slot, because it is computed against the round's start time; it is diagnostic only, counts and gaps stay correct (follow-up F-14). If that read fails, `reconcile-after status=unverified error=...` is printed on stderr and collection goes on.

Assumption A1: the exchange's forming slot is at most 2 slots ahead of the local clock. It is an operating assumption with an after-the-fact diagnostic (`clock_lead_slots_max`, and a stderr warning when it exceeds 2); nothing watches the drift while running (S17). A violation costs reach (a slot shows as `still_missing` or as a hole), not a false claim.

If the pre-start read fails, `run` exits `1` before any network call. Known limitation (follow-up): in round one only, if the wall clock steps back past the oldest wanted slot (a step of roughly one slot, 5 minutes, or more), the request-size calculation raises `ValueError` and the collector ends with exit `1` (fail closed).

**Cost warning (HARD STOP 2, D13).** The pre-check and the post-check each read one series-wide index scan per symbol. It is awaited inline, cannot be interrupted by a stop signal, and delays shutdown and the start of round two by its duration. Its cost on a long history is **unmeasured**: `make bench PROFILE=medium|target` has not been run. Do not use `quasar-collector run` against a real database with long history until the benchmark numbers have been accepted.

Binance request weights were not read: a 1000-row request is spaced only by `--min-request-interval-ms`.

`--min-request-interval-ms` spaces the *calls to the exchange adapter*; the CCXT client throttles on its own, and Binance's weight limits are not checked, so confirm the setting before running many symbols.

**Backfill (S16).** After a round (and after the S15 post-check in round one) `run` spends the idle time before the next boundary on holes inside the last `--backfill-days` days (1..365, default 30; `--no-backfill` disables it). Holes are re-derived from the stored candles every cycle (the 20 newest interior runs and the stretch before the oldest stored candle), newest first, as paged klines with `startTime`/`endTime`/`limit`. Every page goes through `fetch_once`: it is judged against its request first (rows outside the range, misaligned, repeated or too many reject the whole page and store nothing), then the closed-candle selection decides what is stored. One line per symbol and window:

`backfill symbol=… pages=… | served_all=… served_some=… served_none=… | stored=… duplicates=… conflicts=… invalid=… pending=… | new_in_targets=… recovered=… missing_in_targets=… attribution=… | status=…`

Three statements, never merged: `served_*` counts targets by what the exchange returned; `stored… pending` is the writer's report (not evidence); `new_in_targets`, `recovered` and `missing_in_targets` come from two read-backs of the stored open times around the slice. `recovered` is a number only when it can be attributed to this slice (`attribution=ok`); a foreign write in the targets, a mismatching report, an unknown write outcome, a removed row or a failed read makes it `unverified`. `status` is one of `no_targets` (the reader found no hole inside the horizon; nothing is said about older history), `progress`, `backoff`, `time_untrusted`. No line says "done", "filled", "complete" or "unavailable": **v1 never writes `unavailable_candles`**; a slot the exchange does not serve (empty or short page) stays `missing`, is not asked again for 288 cycles (one day) and is asked again after a restart (follow-up F-15).

Priority: a window ends `IDLE_RESERVE_MS = 15 s` before the next boundary and a page starts only if its budget (`PAGE_BUDGET_MS = 15 s`: two 5 s request timeouts plus a 5 s write reserve) fits. Network calls are cancelled at their timeout, at the deadline or at stop; a write that has started is never cancelled. Round lines gain `lateness_ms` (monotonic start delay), `idle_overrun_ms` (monotonic, how long idle work ran past the due time), `skipped_rounds` (cycles in which no round started, from wall clock) and `clock_step_ms` (wall minus monotonic change between round starts beyond 100 ms: a clock event, not load); a non-zero overrun or skipped round is also warned on stderr. After two consecutive idle overruns the backfiller suspends itself until restart (`backfill suspended reason=idle_overrun consecutive=2 until=restart` on stdout, a warning on stderr). Not guaranteed: that rounds start on their boundary (a blocked SQLite write delays the next round by its duration); that a skipped cycle loses nothing. A request that needs about 4.9 s or more times out (the 5 s timeout includes the limiter wait).

CCXT cancellation was checked only on a local plain-HTTP server (CCXT 4.5.89): cancelling a stalled request returns promptly and the client stays usable, and a cancelled request keeps its CCXT throttle turn (pinned by `tests/collector/test_ccxt_cancellation.py`). **Not verified:** Binance's `startTime`/`endTime`/`limit` semantics, request weights (live probe, plan Task 0 step 3), TLS, many cancels in a row, 418/429 behaviour, and the scan cost at 600 symbols (`make bench`, F-10). The client-side page judgement makes a wrong assumption fail closed, not efficient.

Stopping: no new attempt starts, and attempts already running get a 10 s grace to finish. A symbol whose write ever came back with an unknown outcome, or was cancelled while writing, keeps the `write_uncertain` mark **for the rest of the run** (a later successful write is a different batch and proves nothing about it). Closing the writer has no time limit, so the process can stay alive longer than the grace while a blocked write settles.

| Exit | Meaning |
|---|---|
| `0` | stopped on request; every write is accounted for |
| `4` | a write's outcome was unknown or cancelled at some point (the batch may or may not be stored; start the command again, writes are idempotent), even if later rounds were clean; takes priority over `1` |
| `1` | a fatal error with nothing in doubt, a failure before the loop started (directory, schema, source), or a failing writer close after an otherwise clean stop |
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
