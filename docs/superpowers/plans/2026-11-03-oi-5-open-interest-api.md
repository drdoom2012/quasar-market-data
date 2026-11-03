# OI-5 Open Interest API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `GET /v1/open-interest` and the additive `open_interest` object of `GET /health/ingest`, read-only, from stored rows, the server clock and the configuration file (issue #5, ADR-0003 P-7, P-8, P-10, P-11, P-15 rows marked OI-5). The exact schema is contract section 6.5 of `docs/contracts/api-v1.md`.

**Gate (ADR-0003 P-11, issue #5):** contract section 6.5 and decisions OA1 to OA11 (section 9) are **PROPOSED**. **No code of Tasks 1 to 8 is written before the user approves them.** If the user changes a decision, this plan and the contract are edited first. Task 0 (the contract) is the only work done before approval.

**Out of scope:** the Collector, the schema (no migration), `/v1/coverage`, `/v1/candles`, `/v1/indicators`, `/v1/instruments`, the symbol overview page (OI-6), any per-run gap listing for open interest, any new configuration key.

**Architecture and layers.**

| Layer | New / changed | Rule |
|---|---|---|
| `domain` | new `open_interest_coverage.py` | pure; stdlib only (`dataclasses`, `enum`, `collections.abc`, `typing`); imports `open_interest_schedule.expected_after_ms`, `open_interest_cadence.open_interest_cadence`, `open_interest_request.RETRIEVAL_HORIZON_MS`, `freshness.Freshness/SeriesFreshness` |
| `api` | new `open_interest_page.py`, `open_interest_rule.py`; changed `params.py`, `errors.py`, `database.py`, `health_reader.py`, `app.py`, `cli.py` | read-only (`open_reader`), one read transaction per request, SQLite off the event loop, no exchange import (INV-014, INV-044) |
| `config` | none | `Settings.open_interest`, `Settings.rule`, `Settings.venues` are read as they are |
| `collector`, `storage` | none | the API never imports `collector` (INV-001); the "same `c`" test lives in `tests/` and may import both |

## Decisions (implementation; contract decisions are OA1 to OA11)

1. **New enum, not `SlotState`.** `SlotState` (candles) has no `no_longer_recoverable` and a different precedence; reusing it would put a state in the candle response type that can never occur. `OpenInterestSlotState(StrEnum)` has the seven values of contract 6.5.3. `Freshness` / `SeriesFreshness` are reused for the health judgement (same three values, same meaning).
2. **The rule is one frozen dataclass built once at start.** `OpenInterestRule(cadence, expected_after_ms, lag_threshold_ms, period: Timeframe, retrieval_horizon_ms)` (`period` is used to judge and to compute `configured`, it is not echoed in `rule`); `open_interest_rule(symbol_count, budget_per_5min, round_offset_ms, lag_threshold_ms, period)` derives `cadence` with `open_interest_cadence` and `expected_after_ms` with `expected_after_ms` (the Collector's own functions: one derivation, ADR-0003 P-10). `api/open_interest_rule.py::rule_from_settings(settings) -> OpenInterestRule | None` returns `None` when the table is absent or `enabled = false`. `api/cli.py` passes it as `create_app(..., open_interest=rule)`; the default `None` keeps the tests of the other endpoints unchanged; the tests that do change are listed in Task 6.
3. **`period` replaces `timeframe` without a second parser.** `_series_and_start` takes the name of the label parameter (`"timeframe"` today, `"period"` for this endpoint) so the 400 messages and `details.parameter` name the right one. `OpenInterestRequest(venue, symbol, period: Timeframe, start, end, limit)` reuses `SERVED_TIMEFRAME` (`5m`).
4. **404 mapping.** `UnknownSeries` (already raised by readers) gains no new type; `_with_reader` maps it by request type: `OpenInterestRequest` -> `unknown_open_interest_series` (details `venue`, `symbol`, `period`), the others unchanged. A reader for a request type it does not know still re-raises (a bug, not a 404).
5. **Order of checks in the handler** (contract 6.5.5): parse -> `open_interest is None` -> `open_interest_disabled` -> `ensure_timestamp_ms(now_ms())` -> `run_in_threadpool(read_open_interest, ...)`.
6. **Health in one snapshot, existing reader untouched.** `read_ingest` and `read_stored_series` keep their signatures and return types (their tests stay valid). A new `read_ingest_open_interest(db_path, *, conflicts_after_ms, open_connection) -> tuple[tuple[StoredSeries, ...], OpenInterestFacts]` opens one reader, one `BEGIN`, one `verify_schema`, walks the candle series (shared private `_walk_candle_series(conn)`) and then the open interest series in the same transaction. The handler calls it only when the rule is not `None`; otherwise it calls `read_ingest` and the object is `null` with no open interest statement. `OpenInterestFacts(series: tuple[StoredOpenInterestSeries, ...], conflicted: frozenset[tuple[str, str, str]])`.
7. **Cost.** Open interest health: per stored series one next-series seek and one `MAX(time_ms)` seek on `open_interest`'s primary key, and one `LIMIT 1` seek on `open_interest_conflicts_slot` (`time_ms > ?`): O(series), never O(rows) or O(conflicts). `GET /v1/open-interest`: at most `limit + 1` rows, plus conflict `time_ms` values of at most 10 000 slots on the same index.
8. **Boundaries.** Everywhere (code, tests, contract) the conflict window is `time_ms > as_of_ms - 86_400_000`. `conflicted_series` uses `time_ms > as_of_ms - 86_400_000` (contract 6.5.8). `due` is `as_of <= T + expected_after + L` (inclusive, as candles).
9. **Unjudged series (OA9).** The body builder checks each stored open interest series before calling `open_interest_freshness`: `period != rule.period.label` or an unaligned newest `time_ms` -> `unjudged_series += 1`, no exception, no 500. `series_count = current + due + stale + unjudged_series`; `unjudged_series > 0` makes the status `degraded`.
10. **Key position.** `open_interest` is the last key of the `/health/ingest` body (contract 6.2.3).
11. **Module name.** The API reader is `api/open_interest_page.py` (not `open_interest_reader.py`, which exists in `collector/`).
12. **No Collector change, no schema change.** If a test needs open interest rows, `tests/api/seed.py` inserts them with plain SQL (as it does candles), never through the Collector's writer.

## File map

- Create: `src/quasar_market_data/domain/open_interest_coverage.py`, `src/quasar_market_data/api/open_interest_page.py`, `src/quasar_market_data/api/open_interest_rule.py`.
- Modify: `src/quasar_market_data/api/{params,errors,database,health_reader,app,cli}.py`, `tests/api/seed.py` (open interest helpers), `docs/contracts/api-v1.md` (Task 0, approval status after approval), `docs/invariants.md`, `docs/architecture.md` (endpoint list), `docs/adr/0003-open-interest.md` (status line only), `README.md`, `docs/reference.md`.
- Tests (create): `tests/domain/test_open_interest_coverage.py`, `tests/api/test_open_interest_params.py`, `tests/api/test_open_interest_page.py`, `tests/api/test_open_interest_endpoint.py`, `tests/api/test_open_interest_health.py`, `tests/api/test_open_interest_rule.py`.

## Task 0: Contract section 6.5 (done before approval; the gate)

- [x] Write section 6.5, the error rows, the reserved-endpoint row, the health pointer, traceability and decisions OA1 to OA11 in `docs/contracts/api-v1.md`.
- [x] Independent review of this plan and the contract (fresh Opus 5.5 agent, read-only): no blocker, 5 major, 9 minor, nits; all applied (this revision and contract decisions OA9 to OA11).
- [x] **The user approved the contract section and OA1 to OA11 as proposed on 2026-11-03.** Status lines of the contract and of ADR-0003 are APPROVED with the date; commit `docs: OI-5 contract section 6.5 approved`.

## Task 1: Domain: slot states, counts, freshness (pure)

**Files:** Create `domain/open_interest_coverage.py`, `tests/domain/test_open_interest_coverage.py`.

**Interfaces:**
- `OpenInterestSlotState` (`PRESENT`, `CONFLICTED`, `NOT_YET_EXPECTED`, `NO_LONGER_RECOVERABLE`, `DUE`, `STALE`, `MISSING`).
- `OpenInterestRule(cadence, expected_after_ms, lag_threshold_ms, retrieval_horizon_ms = RETRIEVAL_HORIZON_MS)`: ints only (`bool` refused), `1 <= cadence <= 12`, the others `>= 0`, `expected_after_ms` consistent with `cadence` is **not** re-derived here (the builder does that).
- `open_interest_rule(symbol_count, budget_per_5min, round_offset_ms, lag_threshold_ms, period: Timeframe) -> OpenInterestRule`.
- `open_interest_slot_states(*, start, end, period_ms, stored: Set[int], conflicts: Set[int], newest_time_ms: int, as_of_ms: int, rule) -> tuple[tuple[int, OpenInterestSlotState], ...]`: validation like `span_slot_states` (aligned span, non-empty, members aligned and inside `[start, end)`, types), then per slot the first match of contract 6.5.3.
- `classify_open_interest_span(...) -> OpenInterestCoverage(slots, present, conflicted, not_yet_expected, no_longer_recoverable, due, stale, missing, conflicted_times: frozenset[int])` with `gap_free` (`present == slots`).
- `open_interest_freshness(newest_time_ms, as_of_ms, period: Timeframe, rule) -> SeriesFreshness` (contract 6.5.8).

- [ ] **Step 1: Failing tests:**
  - **Precedence:** one parametrized test per adjacent pair of the seven states (a slot that matches states k and k+1 gets k): conflicted over present (conflict record and a row), present over not_yet_expected (a stored row whose `T + expected_after > as_of`), not_yet_expected over no_longer_recoverable (impossible with sane inputs; assert with a tiny `retrieval_horizon_ms`), no_longer_recoverable over due/stale/missing, due/stale only after `newest`, missing only before it.
  - **Boundaries (exact, in ms):** `T + expected_after == as_of` is not `not_yet_expected`; `T + expected_after - 1 == as_of` is; `as_of == T + expected_after + L` is `due`, `+ 1` is `stale`; `T == as_of - H` is not `no_longer_recoverable`, `T == as_of - H - period` is (aligned `T`).
  - **Issue test (P-15) "row absent for 3 rounds":** `c = 1`, `expected_after = 500 000`: the slot is `not_yet_expected` until `T + 500 000`, `due` to `T + 560 000`, `stale` after; at no `as_of` is it `present` unless it is in `stored`.
  - **An absent row with newer rows stored (contract 6.5.3 notes):** `not_yet_expected` while `T + expected_after > as_of`, then `missing`; never `due`, `stale` or `present`; a hole before the newest row within `expected_after` of `as_of` is `not_yet_expected`, not `missing`.
  - **Issue test (P-15) "history ending 40 days before `as_of`" at domain level uses windows of at most 10 000 slots** (the API cap): one window of 10 000 slots ending at `as_of` with the exact count of every state, `as_of` pinned to an aligned value and `L = 60 000` (shorter than a period, so at most one slot is `due`; assert which).
  - **Slots before the oldest row** that are inside the horizon are `missing`; **a conflict without a row** is `conflicted` and counted once; **a stored row after `as_of`** (clock behind) is `present`.
  - **Sum:** the seven counts always sum to `slots` (randomized windows with a fixed seed); `gap_free` only when all are present.
  - **Validation:** float, bool, unaligned, out-of-range, empty span, member outside the span, `newest` unaligned, `stored` member newer than `newest`, all raise `TypeError` / `ValueError` (never a wrong count).
  - **Rule:** `open_interest_rule(10, 400, 200_000, 60_000, 5m)` -> `cadence 1`, `expected_after 500 000`; `(600, 400, ...)` -> `5`, `1 700 000`; `(432, 100, ...)` -> `12`; `(433, 100, ...)` raises `CadenceError`; bad types raise.
  - **Freshness:** table of `(newest, as_of)` -> `current` / `due` / `stale` and `lag_slots` at the exact boundaries; `as_of` behind the stored data is `current` with `lag_slots` 0; unaligned newest raises.
- [ ] **Step 2:** RED. **Step 3:** implement. **Step 4:** GREEN; `make check` (the domain purity AST test and the import contracts must pass).
- [ ] **Commit:** `feat(domain): open interest slot states, counts and freshness (ADR-0003 P-7, P-10)`

## Task 2: Rule from the configuration file

**Files:** Create `api/open_interest_rule.py`, `tests/api/test_open_interest_rule.py`.

- [ ] **Step 1: Failing tests:** `rule_from_settings` returns `None` for an absent table and for `enabled = false`; for an enabled table returns the rule built from `len(venues[0].symbols)`, `budget_per_5min`, `round_offset_ms` and `settings.rule.lag_threshold_ms`; **P-15 "API and Collector derive the same `c`" for b = 100, 200, 400:** parse a TOML text per budget with a symbol count that gives a different `c` for each (for example S = 10, 150, 300, 600 where accepted), call the Collector's own `quasar_market_data.collector.cli._open_interest_run(settings, fake_open_source)` on the **same parsed `Settings`** (not a hand-built copy of its logic) and assert `run.config.cadence == rule.cadence` and `rule.expected_after_ms == expected_after_ms(run.config.cadence, run.config.round_offset_ms, run.config.period)`; **window cross-check:** for several `as_of` values the slots the API labels neither `no_longer_recoverable` nor `not_yet_expected` are exactly `[oldest_allowed_ms(as_of, period), newest_expected_ms(as_of, cadence, offset, period)]` of `domain/open_interest_holes.py`, the window the Collector requests (importing `collector` from `tests/api` is allowed: import-linter covers `src` only, and `tests/api/seed.py` already does); a configuration with `c > 12` never reaches the function (it is refused by `parse_settings`, asserted).
- [ ] **Step 2:** RED. **Step 3:** implement. **Step 4:** GREEN; `make check`.
- [ ] **Commit:** `feat(api): open interest expectation rule derived from the configuration file (ADR-0003 P-10)`

## Task 3: Parameters and errors

**Files:** Modify `api/params.py`, `api/errors.py`; Create `tests/api/test_open_interest_params.py`; extend `tests/api/test_errors.py`.

- [ ] **Step 1: Failing tests:** `parse_open_interest_request` accepts `venue, symbol, period, start[, end][, limit]`; defaults `limit` 1000; rejects (400, `details.parameter` named) a missing / repeated / empty / unknown parameter (`timeframe=5m` is **unknown** here), `period` not `5m`, unaligned or out-of-range `start`/`end`, `end <= start`, a window above 10 000 slots (`details.maximum`), `limit` 0 / 1001 / `1_000`; the ordering of the checks equals `parse_candles_request`'s (same table-driven cases, run against both parsers). `open_interest_disabled()` is `404` with code `open_interest_disabled` and empty details; `unknown_open_interest_series(venue, symbol, period)` is `404 unknown_series` with details `venue`, `symbol`, `period` and **no** `timeframe`; a closed-list test is **created** in `tests/api/test_errors.py` (it has none today) and includes the new code; the `unknown_series` message of this endpoint is "no open interest row of this series has been stored" (OA11), the candle message is unchanged.
- [ ] **Step 2:** RED. **Step 3:** implement (decision 3). **Step 4:** GREEN (the candle and coverage parser tests unchanged); `make check`.
- [ ] **Commit:** `feat(api): open interest request parameters and errors (contract 6.5.2, 6.5.5)`

## Task 4: The reader

**Files:** Create `api/open_interest_page.py`, `tests/api/test_open_interest_page.py`; Modify `tests/api/seed.py` (helpers `insert_open_interest(conn, time_ms, ...)`, `insert_open_interest_conflict(conn, time_ms, variant, ...)`, deterministic canonical values with optional value overrides, so the contract example's values can be reproduced), `api/database.py` (`read_open_interest`, `_with_reader` mapping of decision 4).

**Interfaces:** `read_open_interest_page(conn, request, *, as_of_ms, rule) -> OpenInterestPage(venue, symbol, period, start, end, oldest_time_ms, newest_time_ms, rows: tuple[StoredOpenInterest, ...], next, span_start, span_end, coverage: OpenInterestCoverage)`; `StoredOpenInterest(time_ms, open_interest, open_interest_value, conflicted)`. Same shape and discipline as `candle_reader` (one `BEGIN`, `verify_schema`, two scalar subqueries for MIN/MAX, `limit + 1`, bound statements, results consumed before the next statement, `ROLLBACK` in `finally`).

- [ ] **Step 1: Failing tests** (real SQLite file from `make_database`, rows inserted with the seed helpers):
  - rows ascending with the stored text byte for byte; `limit + 1` read, an exactly full last page has `next: None`; `next` strictly increasing and a paging loop terminates and sees every row once (INV-043 analogue); the span ends at `next`.
  - **End to end with the real writer (P-15 "re-delivered row with a different value"):** write a row and then a differing duplicate through `collector/open_interest_writer.py` (as `seed.py` does for candles with `CandleWriter`), read through the page reader and the endpoint: `conflicted: true`, first stored values, and (Task 6) `conflicted_series 1`; proves the writer's labels and the reader agree.
  - **Defaulted end:** `start <= newest` -> `newest + period` capped at `start + 10 000 x period`; `start > newest` -> one slot, empty `rows`.
  - **Unknown series:** no row of the series -> `UnknownSeries`; a series that has only **candles** is also unknown; rows of another period label / symbol do not leak.
  - **Conflicted slot (P-15):** a row plus a conflict record -> the **first stored** values, `conflicted True`, counted once in `coverage.conflicted`, `gap_free` False; two different conflict records for one slot count once; a conflict in another series does not leak.
  - **States through the reader:** a window with every one of the seven states at a chosen `as_of_ms` (the example of contract 6.5.4 reproduced exactly: counts 2 / 1 / 1 / 0 / 1 / 1 / 1); candles in the same database never change a state.
  - **All statements in one read transaction** and the interleaving probe from `test_candle_reader.py` can observe a missing snapshot (copy of the existing pattern); the number of statements is constant (not per row).
  - Schema mismatch / empty database raise the existing exceptions; `database.read_open_interest` maps them to `503` (`database_missing`, `schema_mismatch`, `database_unreadable`) and `UnknownSeries` to `404 unknown_series` with `period`; a candle request still maps with `timeframe`.
- [ ] **Step 2:** RED. **Step 3:** implement. **Step 4:** GREEN; `make check` (`tests/api/test_read_only.py` scans the new files).
- [ ] **Commit:** `feat(api): open interest page reader in one read transaction (contract 6.5)`

## Task 5: The endpoint

**Files:** Modify `api/app.py`, `api/cli.py`; Create `tests/api/test_open_interest_endpoint.py`; extend `tests/api/test_cli_serve.py`.

- [ ] **Step 1: Failing tests** (`TestClient`, `create_app(..., open_interest=rule, now_ms=...)`):
  - **Body:** the key order and every field of contract 6.5.4; the example of the contract is reproduced from a seeded database and compared as **parsed JSON plus key order** (the API emits compact JSON, the contract pretty-prints; the example is in the test as a literal so the contract and the code cannot drift); `Cache-Control: no-store`; the media type of the others.
  - **Disabled (OA1):** `open_interest=None` -> `404 open_interest_disabled` for a valid request, `400` for an invalid one (parameters first), and **no database is opened** (a connection factory that raises proves it); the other endpoints are unaffected and `/health/ingest` has `"open_interest": null`.
  - **Order of checks:** malformed + broken database -> 400; disabled + broken database -> 404 disabled; bad host clock + broken database -> 500; broken database + unknown series -> 503; unknown series -> 404.
  - **Errors:** unknown series body, 405 with `Allow: GET` for `POST`/`HEAD`, trailing slash 404 `not_found`, `503` reasons and `Retry-After` as in the candle tests.
  - **P-15 at API level:** (a) conflicted slot -> `"conflicted": true` in the row and `conflicted_slots` 1; (b) row absent for three rounds -> the slot walks `not_yet_expected` -> `due` -> `stale` as `now_ms` advances and is never `present` until the row is inserted, then is `present` on the next request; (c) history ending 40 days before `as_of`: a window of at most 10 000 slots ending at a pinned `as_of` (a 41-day window is above the cap and is a `400`, asserted) -> the exact count of every state, `no_longer_recoverable_slots` included; (c2) an absent row with newer rows stored is `not_yet_expected`, then `missing`, never `due`, `stale` or `present`; (c3) a defaulted `end` on a stopped series answers `gap_free: true` (the documented limit, contract 6.5.2) and an explicit `end` after the newest row shows `stale`; (d) `rule` in the body equals `open_interest_rule(...)` for b = 100, 200, 400.
  - **Independence:** the handler never fetches (the autouse no-network guard is active); `tests/api/test_read_only.py` stays green; `/v1/coverage`, `/v1/candles`, `/v1/indicators`, `/v1/instruments` bodies on a database that also holds open interest rows are byte-identical to the same database without them (golden comparison in the test).
  - **Read-only trace:** `tests/api/test_read_only.py::_exercise` (lines 146-153) gains a `/v1/open-interest` request and a `/health/ingest` request with open interest enabled, on a database seeded with open interest rows and conflicts, so the trace test covers the new SQL.
  - **CLI:** `quasar-api serve` with an enabled table builds the app with the rule (spy on `create_app`), with an absent table passes `None`; the start line is unchanged.
- [ ] **Step 2:** RED. **Step 3:** implement. **Step 4:** GREEN; `make check`.
- [ ] **Commit:** `feat(api): GET /v1/open-interest (ADR-0003 P-11, contract 6.5)`

## Task 6: `/health/ingest` `open_interest`

**Files:** Modify `api/health_reader.py`, `api/database.py`, `api/app.py`; Create `tests/api/test_open_interest_health.py`.

**Existing tests that change (the review found them; nothing else is expected to):**
- `tests/api/test_health_universe.py:192` asserts `list(body)[-2:] == ["truncated", "universe"]`. With `open_interest` last (decision 10) it becomes `list(body)[-3:] == ["truncated", "universe", "open_interest"]`, and the key order of the S17 part is asserted separately without it.
- `tests/api/test_health_universe.py:345` (`test_a_commit_after_the_read_finished_is_not_in_the_answer`) patches `database.read_stored_series`; it stays valid for the disabled path because `read_ingest` is untouched. A **twin test** patches the snapshot function used by `read_ingest_open_interest` and asserts that the commit really happened (a row count after the call), so it cannot pass vacuously.
- `tests/api/test_health_reader.py` (lines 61 to 289) calls `read_ingest(path)`: unchanged by decision 6; assert this by running it.

**Interfaces:** decisions 6, 9, 10; `StoredOpenInterestSeries(venue, symbol, period, newest_time_ms)`; `_open_interest_body(facts, as_of_ms, rule, universe)` building the object of contract 6.5.8 (status precedence `no_series`, `degraded`, `behind`, `catching_up`, `current`; `behind` ordered and cut at `HEALTH_BEHIND_MAX`).

- [ ] **Step 1: Failing tests:**
  - **Unchanged candle part:** with and without open interest rows in the database, and with the table enabled or not, the S17 fields and `universe` are byte-identical (`s17_fields` / the golden file); the only new key is `open_interest`.
  - **Null:** not enabled -> `"open_interest": null`, and the reader issues **no** open interest statement (statement log).
  - **Counts and status:** no series -> `no_series` with zeros; all current -> `current`; one due -> `catching_up`; one stale -> `behind`; **conflicted series (P-15):** a conflict at a slot inside the last 24 h -> `conflicted_series 1` and `status degraded` even with a stale series present; with `as_of` chosen so that `as_of - 24 h` is aligned: a conflict at slot `as_of - 24 h` does not count and one at the next slot does; to test the millisecond edge move `as_of` by 1 ms, never the aligned slot; a conflict for an older slot does not count; two conflicts in one series count once; a candle conflict never counts.
  - **Unjudged series (OA9):** a stored series with `period = '15m'` and one with an unaligned newest `time_ms` (inserted with plain SQL) -> `200`, `unjudged_series` counts them, `series_count = current + due + stale + unjudged_series`, status `degraded`, they are not in `behind`, the candle part of the answer is unchanged.
  - **Key position:** `open_interest` is the last key; the keys of the object are exactly those of contract 6.5.8.
  - **Per-series boundaries** (`current` / `due` / `stale`, `lag_slots`) at the exact milliseconds of Task 1 for `c = 1` and `c = 5`; a retired series (not in the universe) is listed with `configured: false`; `behind` order and the cut at 50 with `truncated`.
  - **One snapshot:** all statements of `/health/ingest` (candle and open interest) run inside one `BEGIN ... ROLLBACK` (statement log); the number of statements per series is constant (3 for open interest) so the cost does not grow with the rows or the conflicts (a database with 10 000 conflict rows in other series gives the same statement count); **`EXPLAIN QUERY PLAN` assertions** (the pattern of `tests/api/test_health_reader.py`): the next-series seek and the `MAX` use `SEARCH` on the primary key of `open_interest`, the conflict probe uses `SEARCH` on `open_interest_conflicts_slot` with `venue`, `symbol`, `period` bound, never `SCAN`.
  - **503:** a missing / mismatched / unreadable database still answers `503` for `/health/ingest`; `/health` and `/health/ready` unchanged.
- [ ] **Step 2:** RED. **Step 3:** implement. **Step 4:** GREEN; `make check`.
- [ ] **Step 5 (measurement, required before the PR; not in `make check`):** the review computed that 600 series take `/health/ingest` from about 1 200 to about 3 000 statements against the 250 ms H2 target. Extend `tests/api/bench_health.py` with an open interest option (one insert per series), run `make bench-health PROFILE=smoke` before and after and a 600-series run, report the medians. If 600 series exceed H2, stop and bring the options (a single conflict query for all series, a cheaper walk) to the user; do not ship an unmeasured cost.
- [ ] **Commit:** `feat(api): /health/ingest open_interest object (ADR-0003 P-8, P-11)`

## Task 7: Documentation and register

**Files:** `docs/invariants.md`, `docs/architecture.md`, `docs/adr/0003-open-interest.md`, `docs/contracts/api-v1.md`, `README.md`, `docs/reference.md`; read `tests/test_invariant_register.py` first.

- [ ] Register rows (next free ids after INV-087, each `ENFORCED` with a named negative test): the API serves open interest only from stored rows and reports every unstored slot with its state, never `present` by inference (domain precedence + endpoint P-15 tests); a conflicted open interest slot is flagged in the API and counted in health (P-15); the API and the Collector derive the same cadence from the file (Task 2); `/health/ingest` keeps the candle fields unchanged when open interest is added (Task 6); the open interest handler never fetches and never writes (Task 5 + `test_read_only.py`). Update INV-011 / INV-010 text only if their evidence honestly changes.
- [ ] `docs/architecture.md` endpoint list gains `GET /v1/open-interest`; ADR-0003 status line: the P-11 contract section is approved (date) and OI-5 is implemented; the slice table is untouched.
- [ ] `README.md` (section "Open interest requests"): a short "Reading it" paragraph: the endpoint, `time_ms` is the period end, join rule, the seven states, `open_interest_disabled`, the health object; `docs/reference.md`: the API list and the health fields.
- [ ] `make check` with real output; the full test count before / after.
- [ ] **Commit:** `docs: open interest API, register and reference (ADR-0003 OI-5)`

## Task 8: Review and PR (the workflow of the project)

- [ ] **Independent implementation review:** a fresh read-only agent on **Opus 5.5** (`model: "opus"`) against ADR-0003, contract 6.5 and the diff; apply **all** findings; re-run `make check`.
- [ ] PR from `oi-5-open-interest-api`: title `feat: OI-5 GET /v1/open-interest and /health/ingest open_interest`; body readable as the squash commit, with `Closes #5`, the verified commands, which reviews ran and what each found, remaining risks; the attribution line at the end. Nothing is pushed or merged without the user's word in that turn; no tag, no version bump.

## Risks and open points (report in the PR)

- **API outage on deploy (ADR-0003 P-14):** unchanged by OI-5 (schema 2 was shipped by OI-2); no new migration.
- **Same file for both processes:** if the API and the Collector read different `[open_interest]` tables the states shift (contract 6.5.7); only the printed `sha256` shows it.
- **`lag_threshold_ms` is shared with candles (OA3):** with `c > 1` the operator may need a larger value; not measured on the VPS.
- **Health scope is stored series (OA6):** a configured symbol with no open interest row yet is invisible in `open_interest`; a `missing`-style listing is a possible follow-up, not part of this slice.
- **Conflicts older than 24 h** do not raise `degraded` (OA8); they stay visible per slot in `/v1/open-interest`.
- **Soak debt (ADR-0003 A-1)** is not touched by this slice.
