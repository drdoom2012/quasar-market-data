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
| `make bench-indicators` | the worst-case `GET /v1/indicators` request on a generated database, in process and against a served process with 1, 2, 4 and 8 concurrent requests and a `/health` probe (informative, no criterion; not part of `make check`) |
| `make bench-health` | `/health/ingest` read benchmark on a generated database (`PROFILE=smoke`, `medium` or `target`; not part of `make check`; S17 HARD STOP 2) |
| `make live` | live check of `fetch-once` against Binance USDT-M public endpoints (uses the network; not part of `make check`) |
| `make images` | build the `collector` and `api` images for `linux/amd64` with local tags (needs Docker; not part of `make check`) |
| `make compose-test` | `make images`, then the `compose`-marked tests: images, the Compose file, and the isolation test against the running production project (needs a stock Linux Docker Engine, not OrbStack or Docker Desktop; not part of `make check`) |

CI is configured to run `make check` on pull requests and pushes to `main` (not yet observed running). Making it an authoritative gate also needs a required status check on `main`, which is a repository setting not made here. `images.yml` (S21) runs `make compose-test` on pull requests and pushes, and pushes the images to GHCR on pushes only (not yet observed running).

## Local hook

`.claude/settings.json` registers one Claude Code `PreToolUse` hook (matcher `Bash`) that runs `scripts/hooks/pre_commit_check.sh`. When the Bash command contains the text "git commit", it runs `make check` first and blocks the call (exit 2, tail of the output on stderr) if it fails. The hook runs `make check` on the working tree (not the staged content), so it also blocks WIP commits while the tree is red. It matches the text `git commit` anywhere in the command payload, so unrelated commands that mention it cost one `make check`, and forms such as `git -c k=v commit` are not matched. This is a local convenience only. Override the command with `QUASAR_CHECK_CMD` (used by `tests/test_pre_commit_hook.py`).

`storage` holds the SQLite layer shared by both processes: connection factories (`connection.py`), the schema identity and read-side check the API uses (`schema.py`), and the numbered SQL migrations with the runner that only the Collector imports (`migrate.py`). The schema is specified in `docs/adr/0002-sqlite-schema-v1.md`.

`collector` holds `CandleWriter` and `AsyncCandleWriter`: they persist validated candles idempotently and atomically, and record a re-delivered candle that differs from the stored one in `candle_conflicts` (the stored candle is never changed). The writer does not check that a candle is closed; `fetch_once` (`fetch_once.py`) is the path that passes it only confirmed closed candles (ADR-0001), reading from a `KlineSource`. `binance_usdm.py` is the only module that imports CCXT (async client, raw endpoints). The collection loop (`collection_loop.py`) calls `fetch_once` per symbol on the boundaries planned by `domain/schedule.py`; `rate_limiter.py` spaces adapter calls and `clock.py` holds the injected clocks. The API cannot import any of this.

Console scripts: `quasar-collector` and `quasar-api` (`serve` runs the read-only HTTP API; see below).

## Configuration file (S20)

`config/quasar.toml` is the only source of operational values for `quasar-collector run` and `quasar-api serve` (INV-063): no switch, no hidden default, no environment variable. Both processes read the same committed, versioned file, so the Collector's publication delay and the API's expectation rule are one key.

- **Location.** The optional `CONFIG` argument; default `config/quasar.toml` relative to the working directory. A relative `database.path` is resolved against the directory of the file (symlinks resolved), never the working directory (INV-065); an absolute path stays absolute.
- **Every key is required**; an unknown key, a wrong type or an out-of-range value is an error. There are no defaults in code.
- **Restart to apply.** The file is read once at start. A change takes effect after a restart of each process.
- **Invalid file.** Exit `2`; the Collector opens no database and makes no network call, the API binds nothing (INV-064).
- **Start line.** `config path=… sha256=… database=…` (the API prints it to stderr, the Collector to stdout). Different files for the two processes are only visible by this hash; nothing enforces that they are the same.

| Section | Keys |
|---|---|
| `[database]` | `path` |
| `[rule]` | `publication_delay_ms`, `lag_threshold_ms` |
| `[[venues]]` | `id`, `timeframes`, `symbols` (the universe is the venue, symbol and timeframe triples) |
| `[collector]` | `limit`, `max_concurrency`, `min_request_interval_ms` |
| `[collector.backfill]` | `enabled`, `days` |
| `[api]` | `host`, `port` |

### `quasar-api serve`

```
quasar-api serve [CONFIG]
```

`CONFIG` is the configuration file; it defaults to `config/quasar.toml` relative to the working directory. `quasar-api serve` has no other switch: the database path, the bind address and port, the expectation rule and the universe all come from the file (see "Configuration file (S20)" above). It binds to `127.0.0.1:8000` with the committed file. Serves `GET /v1/candles`, `GET /v1/coverage`, `GET /v1/indicators`, `GET /v1/instruments` and the health endpoints as specified in `docs/contracts/api-v1.md`. `rule.publication_delay_ms` and `rule.lag_threshold_ms` set the expectation rule that `GET /v1/coverage` and `GET /v1/indicators` classify slots with (at most 300000 and 900000 ms). The file is read once at start; a change takes effect after a restart. An invalid or unreadable file is a usage error (exit `2`): nothing is bound and no database is opened. The first line printed, on stderr, is `config path=<resolved path> sha256=<hash of the bytes read> database=<resolved database path>`. The process only reads: it opens the database read-only on every request and never creates, migrates or writes it. The database file need not exist yet: until the Collector has created and migrated it every request answers `503 not_ready` (`database_missing`, then `schema_mismatch` for a freshly created or unmigrated file, which carries no `Retry-After`), and the API recovers on its own when the file is fixed. A missing, replaced or unreadable file is noticed on the next request. Stop it with Ctrl-C (SIGINT).

**Instruments (S20, contract 6.4).** `GET /v1/instruments` takes no parameter and lists the configured universe, ordered by `(venue, symbol, timeframe)`: `{"instruments":[{"venue","symbol","timeframe","newest_open_time_ms"}]}`, with `newest_open_time_ms` `null` when nothing is stored for that instrument. It lists configured instruments only; a stored series that is not in the file keeps its rows and stays readable through the other `/v1` endpoints (INV-067). It reads stored rows only and never contacts an exchange. The contract section 6.4 is still marked PROPOSED, awaiting the user's approval.

**Health (S17, contract 6.2).** Three unversioned paths, each `GET` only and taking no query parameters (any parameter is `400`; an empty query such as `?` is ignored):

- `GET /health` is liveness: `200 {"status":"alive"}`. It reads no database and no clock and stays `200` while every `/v1` request answers `503`.
- `GET /health/ready` is readiness: `200 {"status":"ready","schema_version":1}` when the database opens read-only with the expected schema, otherwise the `503 not_ready` body with its reason. It says nothing about freshness.
- `GET /health/ingest` is information, never a gate: `200` whenever the database is readable, with `status` `current`, `catching_up` (some series `due`, none `stale`), `behind` (some `stale`) or `no_series`, counts, and up to 50 non-current series. **The S17 fields describe only series that exist in the database** (`scope: "stored_series"`, unchanged by S20): `no_series` means only that no series is stored (not that the Collector is healthy or idle), holes behind a series' newest candle are not freshness (use `/v1/coverage`), and a stopped Collector, an exchange outage and a delisted symbol all look `stale`. With a large universe, the last symbols of a Collector round can be written later than `rule.lag_threshold_ms` after their slot, so `behind` can appear for part of every cycle: set `rule.lag_threshold_ms` above the round duration and alert on `behind` that persists for more than one cycle. Since S20 the separate `universe` block lists the configured instruments that have no stored series (`missing`), apart from those fields; the S17 fields are computed over all stored series, configured or not, exactly as before, with `series_count == configured_stored + unconfigured_stored` (INV-066), and a `behind` entry carries `configured` (a retired series stays `behind` and is marked `configured: false`). A configured instrument with zero rows is `missing` and never changes `status` or any HTTP status; it does not say why. `/health/ready` and `/health/ingest` wait up to the SQLite busy timeout (5 s) on a locked database before answering `503`; `/health` never waits. The read does two seeks per series (the next series and its newest candle); `make bench-health` was run on the `medium` and `target` profiles and the numbers were accepted on 2026-10-28 (follow-up F-17); a cold cache and more than 600 series are not measured.

**Cost of the universe (S20, HARD STOP 2 carried over from S17).** The universe is held in memory and compared with the series the S17 read already returns (no new SQL). `make bench-health` on the `target` profile (600 series x 100 000 rows), baseline B = `6b7b6e4` (before the universe in `/health/ingest`) against A = `7c6851c` (after), 3 runs each in the order B A B A B A, median of the run medians; the S17 figures above are the reference (H1 to H3 held in all 6 runs):

| figure | B | A | noise (max-min of B run medians) | ratio A/B | verdict |
|---|---|---|---|---|---|
| handler, quiet (ms) | 5.3 (runs 5.4, 5.2, 5.3) | 5.7 (runs 5.5, 5.7, 5.7) | 0.20 ms (3.8 % of B) | 1.075 | no regression by the 1.10 rule |
| snapshot, quiet (ms) | 3.2 (runs 3.2, 3.2, 3.3) | 3.4 (runs 3.2, 3.4, 3.5) | 0.10 ms (3.1 % of B) | 1.062 | no regression by the 1.10 rule |

Python 3.12.5, SQLite 3.53.4, macOS 26.6.2, Apple M3 Pro, 18 GiB. Read this honestly: the machine was on battery (83 %), not on mains, with load average about 1.5 and other sessions open; A was slower than B in every run; the handler ratio 1.075 is under the 1.10 line but not by a wide margin (the added cost is building the set of configured triples per request). The verdict is "no regression by the 1.10 rule, not by a wide margin". Absolute figures are a few milliseconds against the 250 ms limit of H2. The first request of each run is a cold outlier (15 to 35 ms) in both trees; medians ignore it.


**Indicators (S18, S19, contract 6.3).** `GET /v1/indicators?venue=…&symbol=…&timeframe=5m&start=…[&end=…]&indicators=rsi:period=14;rsi:period=7` computes one to ten indicators on demand from stored closed candles; nothing is cached or written; the body is built and encoded in the thread pool, off the event loop. `indicators` is one parameter, items separated by `;`, each `name:key=value,…` with every parameter required; v1 serves six indicators (`warmup_slots` in brackets): `rsi:period` 2..100 (`10 x period`); `ema:period` 2..200 (`6 x period`); `macd:fast,slow,signal` with `fast` 2..100, `slow` 3..200, `signal` 2..50 and `fast < slow`, otherwise `400` (`6 x (slow + signal)`; outputs `macd`, `signal`, `histogram`); `atr:period` 2..100 (`10 x period`); `adx:period` 2..100 (`13 x period`; outputs `adx`, `plus_di`, `minus_di`); `bbands:period` 2..200 with `stddev` 1..5 (`period - 1`; outputs `upper`, `middle`, `lower`). `rsi`, `ema`, `macd` and `bbands` use the close, `atr` and `adx` also the high and the low of present slots only (a conflicted slot feeds none of them). The window is at most 2016 slots (7 days of 5m), also for a defaulted `end`, which is never shortened (`400` instead). Every slot is a point with `values` or a `reason` (`conflicted`, `unavailable`, `missing`, `not_yet_expected`, `due`, `stale`, `warmup`): no value is computed across a slot that is not `present` or before the warm-up of its segment, and each indicator reads only its own range `[start - warmup_slots x 5m, end)`, so the widest read range is 2016 + 1500 = 3516 slots. `latest` is the window's last point, not the current market value: omit `end` to end the window at the newest expected slot, then compare `latest.open_time_ms` with `as_of_ms`; a stopped series answers `due` or `stale`, never its last value. Values are float64 results as shortest round-trip decimal strings, reproducible only for the same build (TA-Lib, numpy, the TA-Lib C library, the platform; `tests/api/test_indicator_engine.py::test_r3_golden_strings` and `::test_r3_s19_golden_strings` make an upgrade visible); they are not promised to equal a computation over the whole stored history, for any indicator. The warm-up bounds the weight of the seeds, not the difference in output units (design S19 5.2.1). Observed on generated data (largest difference between the anchored value and the whole-history value; not a bound): a random walk and tiny true ranges differ by about 0.001 points (RSI, ADX) or less; a sudden trend reversal by up to 0.005 points (ADX, RSI(100)); after a 1000x volatility drop RSI(100) differs by about 0.5 points, ADX by about 0.13 points and ATR by about 3.6 %; `bbands` depends only on the last `period` closes, so it is exact in principle, but its last digits can depend on where the segment starts (TA-Lib keeps running sums). `tests/api/test_indicator_convergence.py` records these observations. Limits: the flat-market note applies to RSI and equally to `adx`, `plus_di` and `minus_di` (they read exactly `0` only when every bar from the anchor to that slot is flat, and keep a frozen non-zero value when the market turned flat after moving; follow-up F-20). RSI is undefined (0/0) when the average gain and the average loss are both exactly zero. On the locked build (ta-lib 0.8.2, C library 0.8.1; observed, source not consulted) TA-Lib returns exactly `0.0` only then, that is when every close from the anchor to that slot is equal; a market that turns flat after moving keeps its last RSI ratio, so one slot can read `"0"` or about 64 depending on `start`. Do not read `"0"` as a market statement (oversold or otherwise); this contradicts the 'slightly different (observed below 0.01 RSI points)' sentence of contract 6.3.6 for that case (follow-up F-20, a decision for the user). TA-Lib's C library is required: the `ta-lib` 0.8.2 wheel from PyPI bundles it (C library 0.8.1) on this macOS arm64 machine, so `uv sync` installs everything and no Homebrew `ta-lib` is used; another platform without such a wheel needs the C library first (spec D6 plans a Docker build step for it in S21). `make bench-indicators` (50 series x 4000 generated OHLC bars from `generated_bars`, 5 repeats, page cache not dropped, one machine: macOS 26.6.2 arm64, Python 3.12.5, SQLite 3.53.4, TA-Lib 0.8.2 (C 0.8.1), numpy 2.5.4) ran the S19 worst case: 2016 slots, ten items over all six indicators at the largest warm-ups (`rsi:period=100`, `ema:period=200`, `macd:fast=100,slow=200,signal=50`, `atr:period=100`, `adx:period=100`, `bbands:period=200,stddev=5`, `ema:period=2`, `macd:fast=12,slow=26,signal=9`, `adx:period=14`, `bbands:period=20,stddev=2`), read range 3516 slots, body 2 419 350 bytes. In process, one request at a time (median, min to max in ms): database read 2.9 (2.4 to 3.1), computation 41.7 (38.2 to 51.4), body and JSON 60.9 (45.9 to 64.5), whole request 110.5 (104.2 to 120.6). Served by a separate `quasar-api serve` process (one fresh process per level, 5 rounds of K concurrent requests after one unmeasured warm-up request, the body built in the thread pool), request latency median (min to max, ms): K=1 105.0 (102.7 to 109.3), K=2 169.7 (100.9 to 202.4), K=4 336.7 (140.8 to 490.0), K=8 1051.1 (210.2 to 1424.3). `GET /health` probed every 10 ms while the requests ran: median 13.2, 12.6, 11.7 and 1.8 ms (max 28.5, 57.3, 58.6 and 149.1 ms) for K=1, 2, 4, 8, with 0, 0, 0 and 2 probes above 100 ms (of 22, 41, 90 and 313 probes); the probe runs in the same Python client process that receives the 2.4 MB bodies, so part of its latency may be the client's, not the server's. Server CPU (user / system seconds over the process life, start-up included) K=1 0.87 / 0.05, K=2 1.34 / 0.09, K=4 2.48 / 0.46, K=8 5.46 / 7.83; peak RSS of that process over its whole life (not one request) 93 274 112, 99 500 032, 120 258 560 and 137 019 392 bytes. All four servers stopped on SIGINT. These are numbers from this one machine on a warm cache, with no pass/fail criterion; a cold disk, other machines, per-request memory and more than 8 concurrent requests are not measured, and there is no rate limit (follow-up F-21). S18 reference (before J7, `rsi:period=91` to `100`, read range 3016 slots, measured by the S18 benchmark; an orientation, not a baseline of this benchmark): database read median 2.0 ms, computation 33.0 ms, body and JSON 39.1 ms (26.8 to 40.8, on the event loop), the whole request in process 77.9 ms, body 1 659 971 bytes; concurrency was not measured. `--profile s18` reruns that request.

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
quasar-collector run [CONFIG]
```

`CONFIG` defaults to `config/quasar.toml` relative to the working directory. `run` has no other switch (`--db`, `--symbol`, `--limit`, `--publication-delay-ms`, `--max-concurrency`, `--min-request-interval-ms`, `--backfill-days` and `--no-backfill` are gone; `fetch-once` is unchanged and keeps its own switches). Everything comes from the file (see "Configuration file (S20)" above). The first line printed, on stdout, is `config path=<resolved path> sha256=<hash of the bytes read> database=<resolved database path>`; compare the hash with the one the API prints to see that both processes read the same file. An invalid or unreadable file exits `2` before anything is opened or any network call is made.

Collects continuously until SIGINT or SIGTERM: on every UTC 5m boundary plus `rule.publication_delay_ms` it calls `fetch-once` logic for each configured symbol (so only confirmed closed candles are stored), at most `collector.max_concurrency` symbols at once, spaced by `collector.min_request_interval_ms`. A target slot that is not yet confirmable is retried up to 6 times (1, 2, 4, 8, 8 s); after that the symbol is reported `gave_up` and the next boundary round goes on. It prints one `round ...` line per round (`collected`, `conflicted`, `gave_up`, `stopped`, `write_uncertain`) and one indented line per symbol that is not plainly collected (a `None` count after a shutdown timeout is printed as `unknown`). The round collects the newest `collector.limit` rows; holes in the stored history are backfilled in the idle time between rounds (below). Removing a symbol from the file stops collecting it; its rows stay stored (INV-067).

**Start-up reconcile (S15).** Before any network call and before the writer opens, `run` reads the bounds and the recent window of every symbol on a read-only connection (no checkpoint table exists; the stored candles and records are the facts). It does not read the whole history here: that series-wide read is done by the post-check after round one (below), so the start does not wait for it. It prints:

- `reconcile symbol=... window=... all_present=... clock_behind_stored=... history=unknown missing= due= stale= conflicted= unavailable= wanted_from= unreachable_tail= history_holes=- holes_older_than_window=- needs_s16=- unknown_before= catch_up_limit=`, one line per symbol whose window is not fully present; `-` means "not read yet", never 0;
- `reconcile summary symbols= window_gaps= not_all_present= due_slots= history_holes=- needs_s16_slots=- catch_up=`. In the post-check, `history_holes` and `needs_s16_slots` count slots, not runs. `history_holes` also includes holes inside the window, whose slots are also counted as window `missing`, so the two fields overlap and must not be added.

`window=window_no_gaps` means only that the recent window has no confirmed gap (`missing` or `stale`). `due`, `conflicted` and `unavailable` slots may still exist; `all_present` says whether any do and is `false` whenever one of them is above 0. `history=contiguous_from_oldest` means only "no hole between the oldest and the newest stored candle"; what exists before the oldest stored candle is unknown. No line says that history is complete. A gap older than the window stays reported by the post-check of every later start (`holes_older_than_window`, and `  hole START..END (raw)` lines for the largest holes) for as long as its rows are absent. When the target is out of reach the domain reports `unreachable_tail=0`; the `window` status says so.

The first round sizes its klines request on every attempt from the stored absent slots and the clock, capped at the project's own `MAX_LIMIT` of 1000 rows (Binance's maximum was not verified). The catch-up therefore reaches at most `1000 - 1 - 2 = 997` slots before the forming candle (about 3.5 days of 5m). Slots the request cannot reach are counted as `needs_s16`; they are left to S16 (paging, `unavailable_candles`). A long `rule.publication_delay_ms` needs a larger `collector.limit`; a delay no round can serve is a usage error (exit `2`) before anything is opened.

After round one the database is read again, this time with the whole history: `reconcile-after symbol=... absent_before= history_holes= holes_older_than_window= needs_s16= unknown_before=` lines (plus `  still_missing SYMBOL START..END STATE` and `  hole START..END (raw)` lines) and `reconcile-after summary ... clock_lead_slots_max=` state what the database holds. This post-check runs in the background: round two waits only until its read transaction holds a snapshot (milliseconds), so it reports exactly what round one left and never a later write, while the series-wide read runs beside round two and the backfill and its lines appear when it is done. While it reads, SQLite cannot checkpoint the WAL past its snapshot, so the `-wal` file may grow for that time. At shutdown it gets at most 2 s more, then it is interrupted and `reconcile-after status=unverified` is printed; a run stopped before its post-check finished therefore reports no history at all (the backfill still finds the holes). Recovery is only ever shown by this second read; `absent_before` is a comparison value, not a claim. In `reconcile-after`, `clock_behind_stored` can read `true` with a correct clock when round one crosses a slot boundary and later symbols store the next slot, because it is computed against the round's start time; it is diagnostic only, counts and gaps stay correct (follow-up F-14). If that read fails, `reconcile-after status=unverified error=...` is printed on stderr and collection goes on.

Assumption A1: the exchange's forming slot is at most 2 slots ahead of the local clock. It is an operating assumption with an after-the-fact diagnostic: round one reports `clock_lead_slots_max`, and since S17 every round line carries `clock_lead_slots=<n|->` (the largest lead seen in that round) and a stderr warning is written whenever a new run-wide maximum above 2 appears (a sustained lead warns once; the round line shows it every round). It is a diagnosis, not a guard. A violation costs reach (a slot shows as `still_missing` or as a hole), not a false claim.

If the pre-start read fails, `run` exits `1` before any network call. Known limitation (follow-up): in round one only, if the wall clock steps back past the oldest wanted slot (a step of roughly one slot, 5 minutes, or more), the request-size calculation raises `ValueError` and the collector ends with exit `1` (fail closed).

**Read cost (HARD STOP 2, D13).** Measured with `make bench PROFILE=target` (600 series x 105 120 rows) and accepted on 2026-10-28 (follow-up F-10): the start waits about 0.7 s for the window read; the background post-check takes about 35 s. A cold disk, more than 600 series or more than 3 years of history are not measured.

Binance request weights were not read: a 1000-row request is spaced only by `collector.min_request_interval_ms`.

A round observes the exchange server time about once per 5 seconds (never more than half of `max_age_ms`; a short age keeps the clock-rate error of the observation small) and shares that observation across all symbols and retries, instead of asking once per symbol; an observation the time-trust gate rejects is never reused, and a failed refresh falls back to the previous observation only for as long as the gate accepts its age (INV-070). Backfill pages still observe server time once per page.

`collector.min_request_interval_ms` spaces the *calls to the exchange adapter*; the CCXT client throttles on its own, and Binance's weight limits are not checked, so confirm the setting before running many symbols.

**Backfill (S16).** After a round (and after the S15 post-check in round one) `run` spends the idle time before the next boundary on holes inside the last `collector.backfill.days` days (1..365; `collector.backfill.enabled = false` disables it). Holes are re-derived from the stored candles every cycle (the 20 newest interior runs and the stretch before the oldest stored candle), newest first, as paged klines with `startTime`/`endTime`/`limit`. Every page goes through `fetch_once`: it is judged against its request first (rows outside the range, misaligned, repeated or too many reject the whole page and store nothing), then the closed-candle selection decides what is stored. One line per symbol and window:

`backfill symbol=… pages=… | served_all=… served_some=… served_none=… | stored=… duplicates=… conflicts=… invalid=… pending=… | new_in_targets=… recovered=… missing_in_targets=… attribution=… | status=…`

A slice that fails (`status=backoff`) also prints one line on stderr with its cause, `warning: backfill symbol=… status=backoff reason=<ErrorClass: message>` (one line, at most 200 characters); the stdout line above keeps its format. The error class is part of the reason so a bug is not mistaken for a storage or exchange fault.

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

## Deployment (S21)

Two images, `ghcr.io/drdoom2012/quasar-market-data-collector` and `ghcr.io/drdoom2012/quasar-market-data-api` (`linux/amd64`, user `10001`), run as two services of `compose.yaml`. They share the volume `quasar-data` (mounted at `/app/data`, writable for both, because a WAL reader needs the shared-memory file) and read `config/quasar.compose.toml`, mounted read-only at `/app/config/`. That file must stay identical to `config/quasar.toml` except `[api].host = "0.0.0.0"` (`tests/config/test_compose_config.py`). The API stays read-only at the SQLite connection (INV-014); the Collector is the only writer.

**Network.** The `api` service is attached only to the network `internal` (`internal: true`, subnet `172.31.250.0/24`, fixed address `172.31.250.10`): it has no route out and no published port (a published port does not work on an internal network). Only the VPS itself reaches it; from your machine use an SSH tunnel:

```sh
ssh -L 8000:172.31.250.10:8000 <vps>     # then: curl localhost:8000/health
```

The `collector` is on `internal` and on `egress` (it needs the exchange). `make compose-test` checks this against the running project (INV-069); it proves network isolation of the deployment, not that API handlers never try to fetch exchange data (INV-013).

**Release rule.** `compose.yaml` pins `v<project.version>` (today `v0.1.0`). That tag exists in GHCR only after the git tag `v<project.version>` was pushed and its `images.yml` run succeeded; a push to `main` publishes only `main` and `sha-<commit>`. Before the first `docker compose pull`, check: both `images.yml` jobs green for the tag (`gh run list --workflow images.yml`), `docker buildx imagetools inspect ghcr.io/drdoom2012/quasar-market-data-<name>:v<version>` lists `linux/amd64` for both images, and `git show v<version>:compose.yaml` pins that tag. The H1 network checks on the VPS (S21 design Appendix B.2) also run before the first deployment.

**First start on the VPS.** Copy `compose.yaml` and `config/quasar.compose.toml` from the tagged commit (keeping `config/` next to `compose.yaml`), `docker login ghcr.io` with a read-only token if the packages are private, then `docker compose pull && docker compose up -d`. **Upgrade:** bump `project.version` and both image tags in `compose.yaml` in one commit, push it, tag that commit `v<version>` and push the tag, wait for the green `images.yml` run, then `docker compose pull && docker compose up -d` on the VPS. Nothing is built on the VPS.

**Health.** Both services restart unless stopped. The Collector's healthcheck only checks that its main process exists: a dead main process already stops the container (and the restart policy starts it again), Docker does not restart an unhealthy container, and a stuck or idle Collector stays `healthy`. Ingestion shows in `/health/ingest` (series go `due`, then `stale`) and in the Collector's `round` lines (`docker compose logs collector`); neither names the cause (F-16). Logs rotate (`json-file`, 10 MB x 3 per service); memory limits are set in `compose.yaml` with the measurement they come from.

**Backup.** The database is live WAL: copy it with SQLite's backup API, never by copying files while the Collector runs (the main file and its `-wal` file cannot be copied atomically). The images have no `sqlite3` command-line tool:

```bash
docker compose exec collector python -c "import sqlite3; s = sqlite3.connect('/app/data/quasar.db'); d = sqlite3.connect('/app/data/backup.db'); s.backup(d); d.close(); s.close()"
docker compose cp collector:/app/data/backup.db ./quasar-backup.db
docker compose exec collector rm /app/data/backup.db
```

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
