# S17 Health and Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The API answers three operational questions separately: is the process alive (`GET /health`), can it serve data (`GET /health/ready`), and is ingestion keeping up (`GET /health/ingest`). The Collector watches the A1 server-clock lead in every round, not only in round one (F-12).

**Architecture:** A pure domain function judges one series' freshness from its newest stored open time, the clock and the S11 expectation rule. A read-only API reader discovers the stored series and their newest open time in one read transaction. Three handlers in `api/app.py` render the results with the existing error body. The Collector change is one hook widened from round one to all rounds. No schema, ADR or Collector-to-API channel is added.

**Tech Stack:** Python 3.12, FastAPI, stdlib `sqlite3`; pytest, mypy strict, ruff, import-linter. No new dependency.

**Spec:** `docs/superpowers/specs/2026-10-20-market-data-v1-design.md` (§8 S17, §11), `docs/architecture.md` (§6 item 7, §8, §9), `docs/contracts/api-v1.md` (§2 `503 not_ready`, §6 "Health", §9 Q7 and Q8), `docs/invariants.md` (INV-028, INV-051, INV-052), `docs/follow-ups.md` (F-12). Earlier plans: `2026-10-25-s12-get-v1-coverage.md`, `2026-10-27-s15-checkpoints-and-restart-reconcile.md` (D12).

## Facts checked (2026-10-28, read in the code)

- `GET /health` is named in `docs/architecture.md` §8; the contract (§6, Q8) leaves path and body to S17 and fixes only the readiness vocabulary (`503 not_ready`, reasons `database_missing`, `schema_mismatch`, `database_unreadable`).
- `api/database.py::_with_reader` already maps every storage failure to those reasons and closes the connection; it takes a `CandlesRequest | CoverageRequest` only to build `UnknownSeries`. `verify_schema` runs inside the read transaction in both readers.
- `api/app.py::coverage` reads the host clock through `now_ms` and answers `500` for a clock outside the valid range, after parameter validation and before the database is opened (contract 6.1.5). The health handlers follow the same order.
- `domain/schedule.py::newest_expected_open_ms(now_ms, timeframe, publication_delay_ms)` and `expected_at_ms` already give the S11/S13 expectation instant; `ExpectationRule` carries the publication delay and the lag threshold (`api/rule.py` bounds them).
- The Collector's A1 diagnostic: `collect_symbol(..., observe_forming)` is wired only `if first and observe_forming_slot is not None` (`collector/collection_loop.py`, `observe`), the CLI keeps `lead_max` for round one and `_warn_clock_lead` prints a stderr warning. `RoundReport` has no lead field.
- `candles` primary key is `(venue, symbol, timeframe, open_time_ms)`. A `GROUP BY` over it reads the whole index; the fast shapes are one seek per series (`MAX(open_time_ms) … WHERE venue=? AND symbol=? AND timeframe=?`) and a skip scan for discovering the series. **Measured in the review of this plan (SQLite 3.53.4, 600 series x 300 rows, scratch database):** `GROUP BY venue, symbol, timeframe` with `MAX` took 17 ms and scans the index (cost grows with total rows); the usual recursive-CTE skip scan with `(venue,symbol,timeframe) > (…)` subqueries plans as a venue-prefix search and took 11 s (quadratic, rejected); a Python-side loop of one `MAX` seek plus one full-primary-key row-value seek per series took about 2 ms with plan `SEARCH … USING PRIMARY KEY ((venue,symbol,timeframe)>(?,?,?))` and no table scan. Task 2 uses the loop. The large-database cost stays F-17 / S23.
- **Bounds of the rule (read in `api/rule.py`, `domain/timeframe.py`):** `publication_delay_ms` is `0..300 000`, `lag_threshold_ms` is `0..900 000`; the domain supports more than one timeframe (`5m`, `15m`, …, `1m` is `60 000` ms), v1 serves `5m` (`300 000` ms). So `lag_threshold_ms / timeframe_ms` can be `0` to `3` for `5m` and larger for a shorter timeframe: it is **not** always below one slot.
- **Stored series is defined by the `candles` table only** (contract §5.4 `unknown_series`: "No candle of this series has ever been stored"). v1 never writes `unavailable_candles` (S16 D3) and a conflict needs a stored candle, so a row in either table does not create a series.
- Next invariant IDs: INV-056 onward (`docs/invariants.md` ends at INV-055).

## Global Constraints

- Collector and API stay independent; `domain` imports nothing from `collector`, `api`, `storage`, ccxt, fastapi, talib or `sqlite3`.
- The API never writes, never contacts an exchange and opens the database only through `open_reader`, per request (INV-014, INV-028, INV-044).
- Every error the application produces uses the contract §2 body; health paths follow contract §1 (`GET` only, no trailing slash, no redirects, `Cache-Control: no-store`, UTF-8 JSON, unknown query parameters rejected).
- No database schema change, no new heartbeat or status table, no change to an accepted ADR (the user decides each; none is proposed).
- Never fabricate a status: a value that could not be read is `unverified` or an error, never `ok`.
- Time is integer epoch milliseconds, UTC. No float.

## Review Focus

1. **A database with the schema but no candle** → `/health/ingest` is `200`, `status: "no_series"`, `series_count: 0`; never `current`.
2. **A host clock behind the newest stored candle** (the Collector's clock ahead) → the series is `current`, `lag_slots` is `0`, never negative (the S12 `stored_ahead_of_as_of` case).
3. **Liveness survives everything the readiness reports**: database missing, empty, foreign, newer schema, corrupt, locked → `GET /health` stays `200`; `GET /health/ready` answers `503` with the matching reason, checked on every call including after the file is replaced.
4. **Long, uneven history** (one series with 200 000 rows, hundreds with 1 row, several with 300, mixed venues and timeframes, symbols that are prefixes of each other) → the response stays bounded (non-current series capped, `truncated: true`), the reader's work depends on the number of series and not on the number of rows per series (VM-step test), the snapshot is consistent while a writer commits mid-read, and a measurable gate (D11) stands before production use.
5. **Wrong method, trailing slash, `HEAD`, any query parameter** on the three paths → the contract §2 body (`405`, `404`, `400`), not the framework's.
6. **A frozen Collector and an exchange outage look the same** to `/health/ingest`; the contract text must say so (stated, tested as a documented behaviour: a series whose newest candle is old is `stale`, whatever the cause).
7. **A configured symbol that never produced a candle is invisible** to `/health/ingest` (it is not a stored series): the endpoint says `current` or `no_series` about what is stored, never about what should be stored (D10).

## Decisions (proposed; the contract section is HARD STOP 1)

1. **Paths.** `GET /health` (liveness), `GET /health/ready` (readiness), `GET /health/ingest` (ingestion freshness). They are unversioned operational endpoints, outside `/v1` (a probe must not break on `/v2`). `GET /health` is exactly the path in architecture §8, so no architecture text changes. Alternative not proposed: `/health/live` + `/health/ready` and an architecture edit; `/health` would then be a 404 for probes that assume it.
2. **Liveness.** `200 {"status":"alive"}`. It touches no database and no clock. It says only that the event loop answers.
3. **Readiness.** `200 {"status":"ready","schema_version":1}` when `open_reader` succeeds and `verify_schema` passes inside one read transaction; otherwise the existing `503 not_ready` body with its reason and headers (`Retry-After: 5` except for `schema_mismatch`). It says nothing about freshness: an idle Collector must not make the API unready, because readiness gates whether the API can answer correctly, not whether data is new (Q7).
4. **Ingestion freshness is information, not a gate.** `/health/ingest` is `200` whenever the database is readable (and `503 not_ready` when it is not, because the answer would be unfounded). A lagging Collector never changes an HTTP status, so a liveness or readiness probe cannot restart the API because of the Collector. The body:
   ```json
   {"as_of_ms": 0, "rule": {"publication_delay_ms": 15000, "lag_threshold_ms": 60000},
    "scope": "stored_series", "status": "current|catching_up|behind|no_series", "series_count": 0, "current": 0, "due": 0, "stale": 0,
    "behind": [{"venue": "", "symbol": "", "timeframe": "", "newest_open_time_ms": 0, "lag_slots": 0, "state": "due|stale"}],
    "truncated": false}
   ```
   `behind` lists non-current series, stalest first, at most `HEALTH_BEHIND_MAX = 50`; `truncated` is true when more exist. `status` is `no_series` **only** when the snapshot found zero stored series (D10); `behind` when `stale > 0`; `catching_up` when `due > 0` and `stale == 0`; `current` otherwise. `due` is the normal window after each boundary (the server lower bound deliberately lags the wall clock), so it must not read as `behind`. `scope` is the constant `"stored_series"`: every count and every status describes the series that exist in the database, nothing else (D10). A retired or delisted symbol stays in the table forever and keeps the status `behind` (the contract text says so; no retirement mechanism exists in v1).
5. **Freshness rule (pure), with its arithmetic.** Let `tf` be the timeframe in ms, `d` the publication delay, `L` the lag threshold, `n` the newest stored open time, `e = newest_expected_open_ms(as_of, tf, d)` (the newest slot with `expected_at <= as_of`, `None` before the first). Slot `n + j*tf` is expected at `x + (j-1)*tf`, where `x = expected_at_ms(n + tf, tf, d)`.
   - `e is None` or `n >= e` → `current`, `lag_slots = 0` (a host clock behind the stored data is `current`, never negative).
   - Otherwise `n < e`, which holds exactly when `as_of >= x`, and `lag_slots = (e - n) // tf = 1 + (as_of - x) // tf`.
   - `as_of <= x + L` → `due`, else `stale` (the S11 decision-1 boundaries, strict `>` for stale).
   - **Consequences, proved from the two lines above (the earlier claim "`due` implies `lag_slots == 1`" is false and is withdrawn):** `due` means `x <= as_of <= x + L`, so `1 <= lag_slots <= 1 + L // tf`; `due` with `lag_slots == 1` for every `L < tf` (the default `L = 60 000` against `tf = 300 000`), but `L = 900 000` and `tf = 300 000` allow `due` with `lag_slots` up to `4`, and `tf = 60 000` up to `16`. `stale` means `as_of > x + L`, so `lag_slots >= 1` and unbounded above. `lag_slots` is a count of expected-but-absent slots after the newest stored one, not a state.
   - **Compatibility with S11 (checked by a test, not by argument alone):** S11 judges every slot after the newest one separately; the oldest missing slot `n + tf` has the earliest `expected_at`, so it is the most overdue. Therefore, for `classify_span_at` over `[n + tf, e + tf)`: `stale > 0` iff this rule says `stale`; `stale == 0 and due > 0` iff `due`; `due == 0 and stale == 0` iff `current`. The rule changes none of S11's boundaries.
   It reads only the newest stored candle: **holes behind it are not freshness** (`/v1/coverage` reports them) and a hole-ridden series can be `current`.
6. **What it cannot tell.** A stopped Collector, a halted or delisted symbol, an exchange outage and a wrong publication delay all show as `stale`; the endpoint does not know the cause or whether the Collector runs. Collector process supervision (restart policy, container health) belongs to S21; S17 adds no Collector HTTP endpoint, heartbeat or status table (that would need a schema change and the user's approval).
7. **Continuous A1 monitoring (F-12).** The existing hook is passed in every round. Each round line gains `clock_lead_slots=<n|->` (the maximum lead seen in that round). The CLI keeps the run-wide maximum and writes the stderr warning whenever a new maximum above `REACH_MARGIN_SLOTS` appears; the `_warn_clock_lead` call in `after_first_round` (`cli.py`) is removed so round one does not warn twice. **Policy cost, for the user:** a sustained lead of 3 warns once and then stays silent; only the `clock_lead_slots=` field on each round line shows it. Round one's `reconcile-after` field (`clock_lead_slots_max`) keeps its meaning. It changes no behaviour and adds no request; it is a diagnosis after the fact.
8. **Host clock.** Only `/health/ingest` reads it. A value outside the valid range is `500`, checked after parameter validation and before the database is opened (as `/v1/coverage`).
9. **Out of scope, recorded as limits:** Collector heartbeat or health endpoint, alerting, metrics (`docs/metrics.md`), per-series coverage in health, ingest lag in seconds as a histogram, authentication, readiness that depends on freshness, other timeframes.

10. **Stored series, not expected series.** The endpoint judges only series that exist in the database: at least one row in `candles` at the snapshot. It never claims to cover every configured symbol. The API has no universe configuration (S20) and no Collector channel, and S17 adds none; consequently **an instrument whose ingestion never started, or whose first write never succeeded, is invisible**: the endpoint cannot tell it from an instrument that is not configured, and cannot make `status` worse. The contract text, the README and INV-057's "Not covered" say so. A consumer who needs "is every expected symbol present" compares its own list with `series_count` and the listed series, or uses `/v1/coverage` per symbol (`404 unknown_series` for a never-stored one); an operator-side comparison against the configured universe belongs to S20 (`/v1/instruments`) and is recorded as F-18, not decided here. `no_series` therefore means **exactly**: the read transaction found zero stored series. It does not mean the Collector is healthy, idle, broken, not started or not initialised; the same body is produced by a freshly migrated database, a Collector that crashed before its first write and a database that holds only non-`candles` rows. It is HTTP `200` (information, Decision 4); a consumer that needs "data exists" tests `series_count > 0`.
11. **Read cost is a measured gate, not an assumption.** The reader does one `MAX` seek and one next-series seek per series (Task 2). Default-suite tests pin that the work depends on the number of series and not on rows per series, on a long and uneven history, and that the snapshot is consistent. A benchmark (`make bench-health`, not part of `make check`, same style as `make bench`) measures it on generated databases; **HARD STOP 2**: `/health/ingest` is not enabled against a real database with long history until the user accepts the numbers against the criteria below, which are proposals for the user to confirm or change: H1 the steps per series on the `target` profile are within 2x of the `smoke` profile (rows per series must not matter); H2 the whole snapshot on the `target` profile (600 series, 100 000 rows each, 5m) takes at most 250 ms on the reference machine, cold cache excluded; H3 a writer that commits in the middle of the read (a new series sorting after the cursor and newer candles in series already read and not yet read) neither fails the read nor changes its result (R3: the earlier 1 Hz thread could not fail; the commit now happens inside the read and the result is compared with the whole pre-read snapshot). Until accepted, the endpoint ships behind the HARD STOP as an implemented but unaccepted capability, and the README says so (S15's F-10 precedent).

## File Structure

- Create `src/quasar_market_data/domain/freshness.py` — `Freshness`, `SeriesFreshness`, `series_freshness`.
- Create `src/quasar_market_data/api/health_reader.py` — `StoredSeries`, `check_ready`, `read_stored_series`.
- Modify `src/quasar_market_data/api/database.py` — `_with_reader` accepts "no series request"; add `read_ready`, `read_ingest`.
- Modify `src/quasar_market_data/api/app.py` — three handlers, bodies, a no-query check.
- Modify `src/quasar_market_data/api/params.py` — `require_no_query(raw: bytes) -> None` (reuses `_decode`, so malformed escapes and non-ASCII give the same errors as the other endpoints).
- Modify `src/quasar_market_data/collector/collection_loop.py`, `collector/cli.py` — Decision 7.
- Modify docs: `docs/contracts/api-v1.md` (new §6.2, Q8, traceability), `docs/invariants.md`, `docs/follow-ups.md`, `README.md`, spec §11 item 13, S16 text that says "S17".
- Create `tests/api/bench_health.py` and add the Makefile target `bench-health` (profile `smoke|medium|target`, mirrors `tests/collector/bench_reconcile.py`).
- Tests: `tests/domain/test_freshness.py`, `tests/api/test_health_reader.py`, `tests/api/test_health_endpoints.py`, `tests/collector/test_clock_lead_monitor.py`.

### Task 0: Gate — the contract section (no code)

- [ ] **Step 1: Write `docs/contracts/api-v1.md` §6.2 "Health"** from Decisions 1 to 6, 8 and 10: paths, the three bodies, the status table, the consumer rules ("`/health/ingest` is information, never a gate"; "a `current` series can still have holes"; the indistinguishable causes of `stale`), the error order (`400` → `500` clock → `503`), the closed `status`/`state` values. Replace the "Health" row of §6; leave Q8 as the recorded 2026-10-23 answer and add a new "S17 decisions" table (as S12 has S1-S5). State that §1 (GET only, trailing slash, `no-store`, UTF-8, error body) applies to the health paths although they are outside `/v1`, and the compatibility rule (new fields may be added; no versioning promise). Add traceability rows.
- [ ] **Step 2: HARD STOP 1.** Show the section to the user. The contract text states Decision 10 (stored series only, the meaning of `no_series`, the invisibility of a never-started ingest) and Decision 5's arithmetic (`due` bounds). Tasks 1 to 3 and 5 start only after the user approves: the paths; the `ingest` body and its four `status` values; `HEALTH_BEHIND_MAX = 50`; "200 even when behind"; and **a query string on any health path is `400`** (recommended, §1 consistency; the alternative is to ignore it for probe-friendliness, since a `400` from a liveness probe can trigger a restart). Task 4 does not depend on the contract.
- [ ] **Step 3: HARD STOP 2 (read cost).** Before `/health/ingest` is used against a real database with long history, run `make bench-health PROFILE=medium` and `PROFILE=target` (Task 2 Step 6), show the numbers against H1 to H3 of Decision 11, and record the user's decision. The implementation may proceed before this; production use may not.

### Task 1: Pure freshness judgement

**Files:** Create `src/quasar_market_data/domain/freshness.py`; Test `tests/domain/test_freshness.py`.

**Interfaces:**
- Consumes: `Timeframe`, `ExpectationRule`, `newest_expected_open_ms`, `expected_at_ms`, `ensure_timestamp_ms`.
- Produces: `class Freshness(StrEnum): CURRENT="current"; DUE="due"; STALE="stale"`; `@dataclass(frozen=True) class SeriesFreshness: state: Freshness; lag_slots: int`; `series_freshness(newest_open_time_ms: int, as_of_ms: int, timeframe: Timeframe, rule: ExpectationRule) -> SeriesFreshness`.

- [ ] **Step 1: Write failing tests** `test_current_when_newest_is_the_newest_expected`, `test_due_bounds_lag_slots_by_one_plus_lag_over_timeframe` (parametrised over `(tf, L)` = (300 000, 60 000) → exactly 1; (300 000, 300 000) → 1 or 2; (300 000, 900 000) → up to 4, asserting `lag_slots == 4` is reachable and `due`; (60 000, 900 000) → up to 16; never above `1 + L // tf`), `test_lag_slots_equals_one_plus_elapsed_slots_since_x` (`lag_slots == 1 + (as_of - x) // tf` over a sweep), `test_one_slot_behind_is_due_until_the_lag_threshold_inclusive`, `test_stale_one_millisecond_after_the_threshold`, `test_stored_ahead_of_the_clock_is_current_with_zero_lag` (Review Focus 2), `test_lag_slots_counts_whole_missing_slots` (3 slots behind → `lag_slots == 3`), `test_before_the_first_expected_slot_is_current` (`newest_expected_open_ms` is `None`), `test_agrees_with_classify_span_at_over_all_slots_after_newest` (parametrised over `(tf, d, L)` including `L = 900 000` and offsets around `x` and `x + L`; the whole span `[n + tf, e + tf)` is classified by `classify_span_at` and the three-way equivalence of Decision 5 is asserted: `stale > 0` ⇔ `stale`, `stale == 0 and due > 0` ⇔ `due`, both zero ⇔ `current`), `test_rejects_non_int_and_out_of_range_inputs` (`TypeError`, `ValueError`).
- [ ] **Step 2: Run** `uv run pytest tests/domain/test_freshness.py -v` → FAIL (module missing).
- [ ] **Step 3: Implement** `series_freshness` per Decision 5 in `domain/freshness.py`; domain stays pure (stdlib + domain imports only; `tests/test_domain_purity.py` must stay green).
- [ ] **Step 4: Run** the same command → PASS; `uv run pytest tests/test_domain_purity.py -v` → PASS.
- [ ] **Step 5: Commit** `feat(domain): series freshness judgement (S17)`.

### Task 2: Read-only snapshot of the stored series

**Files:** Create `src/quasar_market_data/api/health_reader.py`; Modify `src/quasar_market_data/api/database.py`; Test `tests/api/test_health_reader.py`.

**Interfaces:**
- Consumes: `verify_schema`, `open_reader`, `_with_reader` (made to accept `request: CandlesRequest | CoverageRequest | None`; `UnknownSeries` cannot occur without a request).
- Produces: `@dataclass(frozen=True) class StoredSeries: venue: str; symbol: str; timeframe: str; newest_open_time_ms: int`; `check_ready(conn: sqlite3.Connection) -> int` (`BEGIN`, `verify_schema`, `ROLLBACK` in `finally` like `read_coverage_report`; returns `SCHEMA_VERSION`); `read_stored_series(conn: sqlite3.Connection) -> tuple[StoredSeries, ...]` (same `BEGIN`/`verify_schema`/`ROLLBACK` shape; ordered by `(venue, symbol, timeframe)`); in `database.py`: `read_ready(db_path, *, open_connection=open_reader) -> int` and `read_ingest(db_path, *, open_connection=open_reader) -> tuple[StoredSeries, ...]`, both through `_with_reader`.

- [ ] **Step 1: Write failing tests** `test_ready_returns_the_schema_version`, `test_missing_empty_foreign_newer_and_corrupt_database_map_to_the_503_reasons` (parametrised over the cases `test_database.py` already uses; same reasons), `test_readiness_is_checked_on_every_call` (replace the file between two calls), `test_no_series_gives_an_empty_tuple`, `test_rows_in_other_tables_do_not_create_a_series` (a hand-inserted `unavailable_candles` row and a `candle_conflicts`-only state leave the result empty; Decision 10), `test_the_series_list_describes_only_what_is_stored` (a symbol that was configured elsewhere but never written is absent), `test_series_and_newest_open_time_across_venues_symbols_and_timeframes`, `test_symbol_with_non_ascii_characters_round_trips`, `test_work_depends_on_series_not_on_rows` (databases of the same 600 series with uneven rows: profile A = 600 x 3 rows; profile B = one series with 200 000 rows, 200 series with 1 row, 399 with 300; a `sqlite3` progress handler counts VM steps; B is within 2x of A, and a deliberate `GROUP BY` variant of the reader fails the same assertion, proving the test can fail), `test_cursor_handles_prefix_colliding_and_unicode_keys` (symbols `A`, `A/B`, `AB`, `A\u00e9`, venues `v`, `v2`, timeframes `5m`, `15m`: the row-value cursor skips none and repeats none, and the result equals a `GROUP BY` oracle on the same database), `test_a_series_with_one_row_and_a_series_at_the_range_ends_are_read` (open times `946684800000` and `4102444800000`), `test_discovery_statement_plan_has_no_table_scan` (secondary; asserts the row-value `SEARCH … PRIMARY KEY ((venue,symbol,timeframe)>(?,?,?))` text and may be relaxed if a newer SQLite words it differently), `test_snapshot_is_consistent_while_a_writer_commits` (a real second writer connection to the same WAL file, driven by a hook called after the k-th series, inserts a new series and newer candles for a series already read and for one not yet read, and commits; the result equals the state before the commit exactly: no new series, no newer `newest_open_time_ms`; a fresh call then sees them), `test_reader_cannot_write` (existing `open_reader` guarantee reused).
- [ ] **Step 2: Run** `uv run pytest tests/api/test_health_reader.py -v` → FAIL.
- [ ] **Step 3: Implement** a Python-side loop inside one `BEGIN`: after each series' `SELECT MAX(open_time_ms) … WHERE venue=? AND symbol=? AND timeframe=?`, fetch the next series with `SELECT venue, symbol, timeframe, open_time_ms FROM candles WHERE (venue,symbol,timeframe,open_time_ms) > (?,?,?,?) ORDER BY venue,symbol,timeframe,open_time_ms LIMIT 1`, with the cursor `(v,y,t,MAX)`. Do not use a recursive CTE (measured quadratic) or `GROUP BY` (acceptable at small size, scans the whole index).
- [ ] **Step 4: Run** the same command plus `uv run pytest tests/api/test_database.py tests/api/test_read_only.py -v` → PASS (the `_with_reader` change keeps both green).
- [ ] **Step 5: Commit** `feat(api): read-only snapshot of stored series (S17)`.
- [ ] **Step 6: The measurable gate (Decision 11).** Create `tests/api/bench_health.py` (profiles `smoke`, `medium`, `target`, `scaling`; generated database in a fresh temp directory; prints steps per series, wall time of `read_stored_series` and of the whole `/health/ingest` handler, with a writer connection that commits inside each read for H3) and the Makefile target `bench-health` (`PROFILE ?= smoke`). Run `make bench-health PROFILE=smoke` and report it; `medium` and `target` are run by hand at HARD STOP 2. The smoke profile runs in `make test` only as a functional check of the script (a few seconds).

### Task 3: The three endpoints

**Files:** Modify `src/quasar_market_data/api/app.py`, `src/quasar_market_data/api/params.py`; Test `tests/api/test_health_endpoints.py`.

**Interfaces:**
- Consumes: `read_ready`, `read_ingest`, `series_freshness`, `now_ms`, `rule`, `ensure_timestamp_ms`, `_json`.
- Produces: `HEALTH_BEHIND_MAX: Final = 50` (in `api/app.py`); `require_no_query(raw: bytes) -> None` in `params.py` (raises `invalid_parameter("health endpoints take no parameters", parameter=<first name>, received=<raw cut>)`); `ingest_body(...)` per Decision 4.

- [ ] **Step 1: Write failing tests** (use the `tests/api/seed.py` helpers and a fake `now_ms`):
  - `test_liveness_is_200_and_touches_neither_database_nor_clock` (a `now_ms` and `open_connection` that raise if called);
  - `test_liveness_stays_200_when_every_readiness_failure_occurs` (Review Focus 3, parametrised);
  - `test_readiness_200_with_schema_version`; `test_readiness_503_reasons_headers_and_body` (`Retry-After: 5` except `schema_mismatch`);
  - `test_readiness_does_not_depend_on_freshness` (an empty migrated database and a database whose only series is days old are both `ready`);
  - `test_ingest_no_series_is_200_no_series_and_means_only_zero_stored_series` (Review Focus 1: a migrated empty database; a database with only an `unavailable_candles` row; both give the identical body, `series_count: 0`, `scope: "stored_series"`, never `current`), `test_ingest_never_reports_a_series_that_was_not_stored` (Decision 10), `test_ingest_body_carries_scope_stored_series`; `test_ingest_current_due_and_stale_counts_and_listing`; `test_ingest_due_with_lag_slots_above_one_under_a_900000_threshold` (`L = 900 000`, `lag_slots` up to 4 listed with state `due`, status `catching_up`); `test_ingest_behind_is_stalest_first_and_capped_with_truncated` (60 stale series → 50 listed, `truncated: true`, counts still 60); `test_ingest_holes_do_not_make_a_series_behind` (Review Focus, Decision 5); `test_ingest_stored_ahead_of_the_clock_is_current`; `test_ingest_a_frozen_series_is_stale_whatever_the_cause` (Review Focus 6); `test_ingest_a_retired_series_keeps_status_behind`;
  - `test_ingest_invalid_host_clock_is_500_even_with_an_unreadable_database` (clock checked before the open); `test_ingest_503_when_database_not_ready`;
  - `test_health_paths_reject_query_parameters_with_400` (all three; `?x=1`, `?=`, `?a=1&a=2`); `test_head_post_and_trailing_slash_use_the_contract_error_body` (`405` with `Allow: GET`, `404 not_found`); `test_health_responses_are_no_store_utf8_json`;
  - `test_ingest_every_series_due_is_catching_up_not_behind`, boundary tests at `as_of = x - 1` (`current`), `x` and `x + lag` (`due`), `x + lag + 1` (`stale`). The new modules need no new scan test: INV-044's import-linter contracts already cover every module of `api`.
- [ ] **Step 2: Run** `uv run pytest tests/api/test_health_endpoints.py -v` → FAIL.
- [ ] **Step 3: Implement** the handlers in `api/app.py` (`async def`, SQLite through `run_in_threadpool`, as the existing endpoints), the order `query check → clock (ingest only) → database`, and `ingest_body` rendering `Freshness` values as the contract's closed strings. Ordering of `behind`: state `stale` before `due`, then `lag_slots` descending, then `(venue, symbol, timeframe)`.
- [ ] **Step 4: Run** `uv run pytest tests/api -v` → all PASS; `uv run pytest tests/test_api_process.py -v` → PASS (add a small helper there and one assertion: a real `quasar-api serve` subprocess answers `GET /health` with `200` and `GET /health/ready` with `503 database_missing` before the database exists).
- [ ] **Step 5: Commit** `feat(api): /health, /health/ready and /health/ingest (S17)`.

### Task 4: Continuous A1 clock-lead monitoring (F-12)

Independent of Tasks 1 to 3 and of the contract; a separate commit. It changes a user-visible log line and a warning policy (Decision 7), so it is on the approval list.

**Files:** Modify `src/quasar_market_data/collector/collection_loop.py`, `src/quasar_market_data/collector/cli.py`; Test `tests/collector/test_clock_lead_monitor.py`; adjust `tests/collector/test_collection_loop.py:744` (rename to "every round", expect one observation per round; recheck :763-800) and the exact round-line assertions at `tests/collector/test_cli_run.py:138,340`; `test_reconcile_run.py` matches only prefixes and stays unchanged. `RoundReport.clock_lead_slots` is added last with default `None`, so existing constructors stay valid.

**Interfaces:**
- Consumes: `clock_lead_slots(forming_open_ms, wall_ms, tf_ms)`, `REACH_MARGIN_SLOTS`, `observe_forming_slot: Callable[[str, int, int], None]` (unchanged signature).
- Produces: `RoundReport.clock_lead_slots: int | None` (maximum lead observed in that round, `None` when no observation); round line field `clock_lead_slots=<n|->`; a stderr warning whenever the run-wide maximum rises above `REACH_MARGIN_SLOTS`.

- [ ] **Step 1: Write failing tests** `test_every_round_observes_the_forming_slot_not_only_round_one`, `test_round_report_carries_the_round_maximum_and_none_without_a_row`, `test_round_line_prints_clock_lead_slots` (`n` and `-`), `test_warning_on_a_new_run_wide_maximum_above_the_margin` (leads 2, 3, 3, 4 → warnings for 3 and 4 only, none for 2), `test_no_warning_within_the_margin`, `test_round_one_reconcile_after_still_reports_its_own_maximum` (regression of `clock_lead_slots_max`), `test_observation_does_not_change_collection_behaviour` (same writes and outcomes with and without the hook).
- [ ] **Step 2: Run** `uv run pytest tests/collector/test_clock_lead_monitor.py -v` → FAIL.
- [ ] **Step 3: Implement**: `run_collection_loop` owns `round_lead` (reset at each round start, updated inside `observe` through `clock_lead_slots` from `domain.reconcile`); `observe` is built in every round even when the user hook is `None`, and the user hook stays optional; the value goes into `RoundReport`. The CLI keeps the first-round maximum for `reconcile-after`, warns on a new run-wide maximum, and drops the `after_first_round` call to `_warn_clock_lead`. Backfill pages are not observed (they request ranges, not the newest rows; say so in a comment).
- [ ] **Step 4: Run** `uv run pytest tests/collector -v` → PASS.
- [ ] **Step 5: Commit** `feat(collector): watch the server-clock lead in every round (S17)`.

### Task 5: Documentation, invariants, follow-ups

- [ ] **Step 1: Invariants.** Add INV-056 (liveness independent of database and clock; readiness only verifies the schema through a read-only connection per call) and INV-057 (ingestion freshness never changes an HTTP status, never fabricates `current`, and describes only stored series: zero stored series → `no_series` and nothing else, unreadable → `503`, holes are not freshness, the read's work does not depend on rows per series and its snapshot is consistent; **Not covered:** a configured symbol whose ingest never started is invisible, `no_series` does not say whether the Collector is healthy), each `ENFORCED` with the Task 2 and Task 3 test names as evidence and the "Not covered" limits (Decision 6). Update INV-028 ("the health endpoint (S17) is not built yet" → built), INV-051 and INV-052 ("not watched afterwards (S17)" → watched, after the fact, by Task 4).
- [ ] **Step 2: Follow-ups.** Remove F-12 (fixed here). Add: F-16 the Collector has no health signal of its own (S21 supervision); F-17 the `/health/ingest` read cost on a soak database (S23 repeats `make bench-health`); F-18 an operator-side comparison of the configured universe with the stored series (S20 `/v1/instruments`; S17 only states the blind spot).
- [ ] **Step 3: README** (`quasar-api serve` section: the three paths and what each does and does not say; `run` section: `clock_lead_slots` on the round line and the warning); spec §11 gets item 13 for S17 (D1 to D11 of this plan); the S16 text and `docs/contracts/api-v1.md` Q8 stop saying "decided in S17".
- [ ] **Step 4: Verify** `make check` → green (lint, typecheck, imports, tests). `make test` must include `tests/test_invariant_register.py`.
- [ ] **Step 5: Commit** `feat: health and readiness (S17)`.

## Revision log (R2, 2026-10-28): what changed for each review point

| # | Review point | Verdict | Changed sections |
|---|---|---|---|
| 1 | D5 freshness arithmetic, `due => lag_slots == 1` | **Valid; the claim was false.** Counter-example from existing limits: `L = 900 000` (`MAX_LAG_THRESHOLD_MS`), `tf = 300 000`, `as_of = x + 900 000` → `lag_slots = 1 + 900 000 // 300 000 = 4`, state `due` (`as_of <= x + L`). True bound: `1 <= lag_slots <= 1 + L // tf`; exactly 1 only for `L < tf`. The rule itself is unchanged and S11-compatible. | Facts (bounds); Decision 5 (proof, withdrawn claim, S11 equivalence); Task 1 tests; Task 3 `due` test with `lag_slots` 4; ordering of `behind`; Residual risk 5a |
| 2 | Stored versus expected series | **Valid.** | Decision 4 (`scope`), new Decision 10, Review Focus 7, Task 0, Task 2 and 3 tests, INV-057 "Not covered", F-18, Residual risk 3a |
| 3 | Meaning of `no_series` | **Valid.** | Decision 4, Decision 10, Task 0 contract text, Task 3 test, INV-057 |
| 4 | SQL performance, uneven history, snapshot, measurable gate | **Valid.** | Facts, Review Focus 4, new Decision 11, Task 2 tests and Step 6, Task 0 Step 3 (HARD STOP 2), Makefile target, F-17, Residual risk 3 |
| 5 | Consistency across the plan | Applied: Decisions, Tasks 0 to 5, tests, invariants, contract list, residual risks, spec item 13 (D1 to D11). | all of the above |

## Residual risks

1. `/health/ingest` reads only the newest candle per series; a series with holes is `current`. By design (Decision 5), stated in the contract.
2. `stale` cannot name its cause (Decision 6).
3. The read cost is pinned by a rows-independence test and measured by `make bench-health`, but **no number is accepted yet** (HARD STOP 2); until it is, `/health/ingest` is not for a real database with long history. A very large number of series makes the read linear in series count; there is no deadline on the read beyond SQLite's busy timeout.
3a. **Invisible never-started ingest (Decision 10):** an instrument that never wrote a candle is not a stored series, so `current` and even `status: "current"` say nothing about it. `no_series` says nothing about the Collector's health.
4. The API's publication delay and lag threshold are its own configuration; a mismatch with the Collector's delay shifts `current`/`due` by the difference (as in `/v1/coverage`).
5a. A `due` series can have `lag_slots` above 1 when the lag threshold is at least one timeframe (Decision 5); a consumer reads `state` and `lag_slots` together.
5. The A1 monitor is a diagnosis after the fact; it neither prevents a miss nor measures the exchange's clock (only the local one against the newest returned row, with up to one slot of measurement error).

## Revision log (R3, 2026-10-28): the final whole-branch review

Applied in the fix pass: H3 of the benchmark made able to fail (commits inside the read, whole-tuple comparison, a test that it fails without a snapshot); the cap, the stale-before-due key and the 49/50/51 boundary pinned by tests (each verified by mutation); the snapshot test writes a series that sorts after the cursor; the "one read transaction through the app" test renamed to what it shows; a CLI-level test for the later-round warning text; tests for an unaligned open time and a locked database; contract 6.2.3, H3 and the README now state the round-longer-than-threshold effect, the empty-query rule, the lock latency, the cross-timeframe ordering and the two seeks per series; Q8 points at S17.
