# Reconcile read cost (F-10) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take the series-wide history scan off the Collector's start-up path and make every history read do the
minimum work, so that `quasar-collector run` starts in well under a second on a database with long history, and the
history read itself meets C1/C2 of S15 D13.

**Architecture:** (1) Query fixes, no behaviour change: `MIN` and `MAX` as two scalar subqueries (one combined
`MIN, MAX` aggregate walks the whole series), one materialized `LAG` pass for the hole aggregate and the five largest
runs (today two passes), and a backfill hole scan that starts at the last row at or before its horizon (today the
horizon filter is applied after the window function, so it walks the whole history). (2) Design change: the
pre-start read is window-only (fail closed as before); the post-check after round one is the only read with history,
runs in the background from a snapshot taken before the round-one hook returns, is interruptible on stop and is
waited for at shutdown for at most a short grace.

**Tech Stack:** Python 3.12, sqlite3 (SQLite ≥ 3.37: `MATERIALIZED` CTE hint), asyncio, pytest.

**Spec:** S15 plan `docs/superpowers/plans/2026-10-27-s15-checkpoints-and-restart-reconcile.md` (D4, D5, D9, D13),
the `target` benchmark of 2026-10-28 (75.6 s first run; `holes` 34.3 s, `largest_runs` 34.5 s, `bounds` 6.3 s;
C1 ≤ 60 s and C2 ≤ 180 s failed, C3 passed), and the user's decision of 2026-10-28: "body 1 a 2 vyřeš autonomně"
(point 1 = the query fixes, point 2 = option (b) of D13, "read the history after the loop has started; the window
read stays pre-start and fail-closed; this changes D4"). **This plan is the approved amendment of D4.**

## Global Constraints

- No schema change, no new table, index or migration; no change to the HTTP contract (`docs/contracts/api-v1.md`).
- Results of every changed statement are identical to today's for every input (only cost changes), except where Task 4
  and Task 5 change *when* history is read and what an unread history prints.
- The pre-start read stays before any network call and before the writer opens; its failure stays exit `1` (INV-051).
- A post-check failure or interruption never changes the exit code and never stops collection (today's rule).
- Collector and API stay independent: the API's bounds fix is a copy of the same SQL idea, not an import (F-9).
- Shutdown waits for the background post-check at most `POST_CHECK_GRACE_MS = 2_000`, then interrupts it.
- An unread history is printed as `-`, never as `0`; nothing prints "complete" (INV-051).

## Review Focus

- A series with no hole at all: the merged statement returns one aggregate row with `NULL` run columns — Task 2.
- Two hole runs of equal length: the five largest keep today's order (longer first, then older first) — Task 2.
- A backfill horizon that falls inside a hole, exactly on a stored row, or before the oldest row — Task 3.
- A stop while the background post-check is still reading: exit code unchanged, prompt exit, `unverified` on
  stderr — Task 5.
- A write committed right after round one must not appear in the post-check (snapshot taken before the hook
  returns) — Task 5.

---

### Task 1: `MIN` and `MAX` as separate scalar subqueries (Collector and API)

**Files:**
- Modify: `src/quasar_market_data/collector/reconcile_reader.py` (`BOUNDS_SQL`)
- Modify: `src/quasar_market_data/api/candle_reader.py:64-67`, `src/quasar_market_data/api/coverage_reader.py:46-49`
- Test: `tests/collector/test_reconcile_reader.py`, `tests/api/test_candle_reader.py`, `tests/api/test_coverage_reader.py`

**Interfaces:**
- Produces: `BOUNDS_SQL` with the same named parameters (`:venue`, `:symbol`, `:timeframe`) and the same one-row
  result `(oldest, newest)`, `(None, None)` for no rows. Imported unchanged by `hole_reader`.

- [ ] **Step 1: Failing tests.** One per reader, named `test_bounds_cost_does_not_grow_with_rows` (collector) and
  `test_series_bounds_cost_does_not_grow_with_rows` (both API readers). Each builds two databases, one series of 20
  rows and one of 5 000 rows (same shape otherwise), counts SQLite VM steps of *only the bounds statement* with
  `set_progress_handler(tick, 1)` (as `tests/api/test_health_reader.py::_steps` does), and asserts
  `big_steps < 2 * small_steps + 50`. For the API readers count the steps of the reader's whole call with a request
  window of 10 slots at the newest end (the only part that grows with rows is the bounds aggregate).
- [ ] **Step 2: Run, expect FAIL** (`uv run pytest -k "bounds_cost" -v`): the combined aggregate walks every row.
- [ ] **Step 3: Implement.** `SELECT (SELECT MIN(open_time_ms) FROM candles WHERE <series>), (SELECT MAX(open_time_ms)
  FROM candles WHERE <series>)` in all three places (positional `?` in the API readers: pass the series tuple twice).
- [ ] **Step 4: Run** `uv run pytest tests/collector/test_reconcile_reader.py tests/collector/test_hole_reader.py tests/api -q` → PASS.
- [ ] **Step 5: Commit** `perf: read series bounds with two primary-key seeks instead of a full series walk`.

### Task 2: One `LAG` pass for the hole aggregate and the largest runs

**Files:**
- Modify: `src/quasar_market_data/collector/reconcile_reader.py` (`HOLES_SQL` replaces `HOLES_SQL` + `LARGEST_RUNS_SQL`)
- Modify: `tests/collector/bench_reconcile.py` (`_SQL_NAMES`), `tests/collector/test_reconcile_bench_smoke.py` (`STATEMENT_CLASSES`)
- Test: `tests/collector/test_reconcile_reader.py`

**Interfaces:**
- Produces: `HOLES_SQL` (params `:venue :symbol :timeframe :tf :ws`) returning 1 to 5 rows
  `(raw, raw_old, runs, start_ms, end_ms)`; the first three columns are equal on every row; `start_ms`/`end_ms` are
  `NULL` on the single row of a series without holes; rows are ordered longest run first, then oldest first.
  `LARGEST_RUNS_SQL` is deleted.
- SQL (verified equal to today's two statements on a 3-year series with 52 holes):

```sql
WITH gaps AS MATERIALIZED (SELECT prev, open_time_ms FROM (<_PAIRS>) WHERE open_time_ms - prev > :tf)
SELECT s.raw, s.raw_old, s.runs, top.start_ms, top.end_ms FROM
  (SELECT COALESCE(SUM((open_time_ms - prev) / :tf - 1), 0) AS raw,
          COALESCE(SUM(MAX(0, (MIN(open_time_ms, :ws) - prev) / :tf - 1)), 0) AS raw_old,
          COUNT(*) AS runs FROM gaps) AS s
  LEFT JOIN (SELECT prev + :tf AS start_ms, open_time_ms AS end_ms FROM gaps
             ORDER BY open_time_ms - prev DESC, prev ASC LIMIT 5) AS top ON 1
ORDER BY top.end_ms - top.start_ms DESC, top.start_ms ASC
```

- [ ] **Step 1: Failing tests.**
  - `test_history_matches_a_reference_over_random_series`: 30 seeded random series (holes of random lengths
    including ties, unavailable rows inside holes); compare `HistoryFacts` captured from `reconcile_blocking` with a
    pure-Python reference (holes from consecutive stored open times; largest five sorted by `(-length, start)`).
  - `test_the_history_walks_the_series_once`: per symbol exactly one statement containing `LAG(` is issued
    (`HOLES_SQL`), and its `EXPLAIN QUERY PLAN` contains `SEARCH candles USING PRIMARY KEY` exactly once. (The rule
    is about `LAG` statements only: the new `BOUNDS_SQL` does two primary-key seeks by design.)
  - Update `test_the_history_statements_use_the_primary_key_and_no_table_scan`: drop `LARGEST_RUNS_SQL`; the
    no-sorter rule now uses the plan tree (`id`, `parent` columns of `EXPLAIN QUERY PLAN`): no `USE TEMP B-TREE`
    node is a descendant of the co-routine that holds `SEARCH candles` (the sorters left only order hole runs).
- [ ] **Step 2: Run, expect FAIL** (`once` test: two `LAG` statements per symbol).
- [ ] **Step 3: Implement** the statement; `_read_series` builds `largest` from the rows whose `start_ms` is not `NULL`.
- [ ] **Step 4: Run** `uv run pytest tests/collector -q -k "reconcile"` → PASS.
- [ ] **Step 5: Commit** `perf: one LAG pass for the hole aggregate and the largest runs`.

### Task 3: Backfill hole scan bounded by its horizon

**Files:**
- Modify: `src/quasar_market_data/collector/hole_reader.py` (`RUNS_SQL`)
- Test: `tests/collector/test_hole_reader.py`

**Interfaces:**
- Produces: `RUNS_SQL`, same params (`:venue :symbol :timeframe :tf :hs`) and result as today. The pairs start at
  `COALESCE((SELECT MAX(open_time_ms) FROM candles WHERE <series> AND open_time_ms <= :hs), :hs)`, so a hole that
  straddles the horizon keeps its stored left neighbour.

- [ ] **Step 1: Failing tests.**
  - `test_runs_equal_the_unbounded_scan_for_every_horizon_position`: parametrize the horizon: inside a hole, exactly on
    a stored row, one slot after a stored row, before the oldest row, after the newest row; compare `read_holes` with
    the result of today's unbounded statement (kept in the test as `UNBOUNDED_RUNS_SQL`).
  - `test_hole_scan_cost_does_not_grow_with_history_older_than_the_horizon`: same recent 30 days, 100 vs 20 000 older
    rows; VM steps of `RUNS_SQL` only: `big < 2 * small + 50`.
- [ ] **Step 2: Run, expect FAIL** on the cost test.
- [ ] **Step 3: Implement** the bounded `RUNS_SQL` (a horizon-bounded copy of `_PAIRS`; update the module docstring).
- [ ] **Step 4: Run** `uv run pytest tests/collector/test_hole_reader.py tests/collector/test_backfill.py -q` → PASS.
- [ ] **Step 5: Commit** `perf: start the backfill hole scan at the horizon`.

### Task 4: Window-only pre-start read; an unread history is `-`

**Files:**
- Modify: `src/quasar_market_data/domain/reconcile.py` (`SeriesReconcile`, `reconcile_series`)
- Modify: `src/quasar_market_data/collector/reconcile_reader.py` (`reconcile_blocking`, `reconcile`: `history: bool = True`)
- Modify: `src/quasar_market_data/collector/cli.py` (`_reconcile_and_collect`, `_print_reconcile`, `_summary_fields`)
- Modify: `README.md` (start-up reconcile paragraph), `docs/invariants.md` (INV-051)
- Test: `tests/domain/test_reconcile.py`, `tests/collector/test_reconcile_reader.py`, `tests/collector/test_reconcile_run.py`

**Interfaces:**
- Produces: `SeriesReconcile.history_unaccounted_holes`, `history_holes_older_than_window`, `hole_runs`,
  `history_conflicted_slots`, `needs_s16_slots` become `int | None`; they are `None` exactly when facts are given and
  history is not (`history is UNKNOWN` with a stored candle). A series with no stored candle keeps `0`.
- Produces: `reconcile_blocking(db, inputs, *, as_of_ms, target_open_ms, history: bool = True)` and the same keyword on
  `reconcile`; with `history=False` no history statement is issued (only `BOUNDS_SQL` and the three window selects).
- CLI: the pre-start call passes `history=False`; `_ms` prints `None` as `-` for the five fields; the summary prints
  `history_holes=-` and `needs_s16_slots=-` when any series' value is `None`.

- [ ] **Step 1: Failing tests.**
  - Domain: `test_unread_history_is_none_not_zero` (facts given, history `None` → the five fields `None`, status
    `UNKNOWN`); `test_no_stored_candle_keeps_zero_history_counts`.
  - Reader: `test_window_only_read_issues_no_history_statement` (statement proxy: no `HOLES_SQL`,
    `UNAVAILABLE_IN_HOLES_SQL`, `CONFLICTED_SQL`).
  - Run: `test_the_pre_start_read_prints_an_unread_history_as_a_dash` (seed a series with a window gap so the pre
    line is printed; `history=unknown history_holes=- holes_older_than_window=- needs_s16=-` on it; summary
    `history_holes=- needs_s16_slots=-`).
  - Known exception, kept: with `target_open_ms is None` (as_of before the first slot of 2000-01-01, which a running
    clock cannot produce) the reader reads nothing and returns "no stored candle" counts, as today.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** Update the tests that asserted history numbers on the *pre* line to assert them on the
  post line instead (the facts are unchanged, only where they are printed); never weaken an assertion.
- [ ] **Step 4: Run** `uv run pytest tests/domain/test_reconcile.py tests/collector -q` → PASS.
- [ ] **Step 5: Docs.** README: the pre-start lines carry the window and `history=unknown`; the history is reported by
  `reconcile-after`. INV-051: "the pre-start read covers bounds and the window only (the history read moved to the
  post-check by the 2026-10-28 amendment of S15 D4)"; add the new evidence tests.
- [ ] **Step 6: Commit** `perf(collector): read only the window before start; history moves to the post-check`.

### Task 5: Background post-check from a snapshot, interruptible, bounded at shutdown

**Files:**
- Modify: `src/quasar_market_data/collector/reconcile_reader.py` (`ReadGuard`, `snapshot` and `guard` keywords)
- Modify: `src/quasar_market_data/collector/cli.py` (`after_first_round`, shutdown, `_print_reconcile_after`)
- Modify: `README.md`, `docs/invariants.md` (INV-051), `docs/follow-ups.md` (F-10)
- Test: `tests/collector/test_reconcile_reader.py`, `tests/collector/test_reconcile_run.py`

**Interfaces:**
- Produces: `class ReadGuard` in `reconcile_reader`: a `threading.Event` wrapper with `interrupt() -> None` and
  `interrupted -> bool`. No cross-thread SQLite call: the worker installs `conn.set_progress_handler(lambda:
  guard.interrupted, 1000)` right after opening (and checks `guard.interrupted` before `BEGIN`), so SQLite aborts the
  running statement itself; `interrupt()` after the connection closed is harmless.
- Produces: `class ReadInterrupted(ReconcileError)`; `reconcile_blocking` raises it when SQLite reports
  `SQLITE_INTERRUPT` (`sqlite_errorcode == 9`) or the guard is set before `BEGIN`; `reconcile` re-raises it unwrapped.
- Produces: `reconcile_blocking(..., on_snapshot: Callable[[], None] | None = None, guard: ReadGuard | None = None)`:
  `on_snapshot()` is called in the worker right after `BEGIN` and `verify_schema` succeed (the read transaction holds
  its snapshot then: `verify_schema` runs real `SELECT`s). `reconcile(..., snapshot: asyncio.Event | None = None,
  guard: ReadGuard | None = None)` passes `on_snapshot = lambda: loop.call_soon_threadsafe(snapshot.set)`.
- CLI: `POST_CHECK_GRACE_MS: Final = 2_000`, measured on the **real monotonic clock** (`asyncio.wait(timeout=...)`),
  not the injected `sleep`: the post-check is a real thread whose duration the fake clock does not model.
  `after_first_round` captures `lead_max = monitor.max_lead`, calls `monitor.first_round_done()`, starts the
  post-check as a task and awaits whichever comes first: `snapshot` set or the task done; it never raises. The task
  prints `reconcile-after` lines, then `  hole START..END (raw)` lines for each series' largest runs, then the summary
  (with the captured `lead_max`), or `reconcile-after status=unverified error=...` on stderr; it catches
  `Exception` itself. The loop call is wrapped in `try/finally`: in the `finally` the CLI waits for the task at most
  `POST_CHECK_GRACE_MS`, then `guard.interrupt()`, then awaits it inside `except BaseException` (logged as a
  warning), so neither a cancellation, `KeyboardInterrupt`, nor an error in the task leaks the thread or changes the
  exit code. Shutdown bound: the loop's own grace (10 s) + `POST_CHECK_GRACE_MS` + one progress-handler tick.

- [ ] **Step 1: Failing tests.**
  - Reader: `test_on_snapshot_is_called_before_the_series_are_read_and_later_writes_are_not_seen` (`on_snapshot`
    records the call order and commits a candle through a writer; a proxy before-hook on the first `BOUNDS_SQL`
    asserts `on_snapshot` already ran; the result excludes the late candle).
  - Reader: `test_the_guard_interrupts_a_running_statement` (a before-hook on `HOLES_SQL` calls `guard.interrupt()`;
    the statement is aborted by the progress handler: `ReadInterrupted`, connection rolled back and closed).
  - Reader: `test_a_guard_set_before_the_read_issues_no_statement`.
  - Run: `test_round_two_does_not_wait_for_the_post_check` (a before-hook on `HOLES_SQL` blocks on a
    `threading.Event` that the test's round-two `fetch_klines` sets; the run completes, round two happened, and the
    `reconcile-after` lines appear after the second `round` line).
  - Run: `test_a_stop_during_the_post_check_exits_promptly_with_the_claim_withheld` (`POST_CHECK_GRACE_MS`
    monkeypatched to 50; a before-hook on `HOLES_SQL` waits until the guard is interrupted, at most 5 s, so the
    statement then starts with the flag set and is aborted; exit `0`, stderr `reconcile-after status=unverified`, no
    `reconcile-after` line on stdout, wall time of `main` < 3 s).
  - Run: `test_the_post_check_prints_hole_lines_and_the_round_one_lead`.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** Existing post-check tests keep their assertions (`second_fails`, clock-lead tests);
  where a test depended on the post-check finishing before the next round, it stops after a later round, never by
  loosening an assertion.
- [ ] **Step 4: Run** `uv run pytest tests/collector -q` three times → PASS each time (thread timing).
- [ ] **Step 5: Docs.** README post-check paragraph (background, snapshot right after round one, interrupted at
  shutdown after at most 2 s, then `unverified`; while it reads, the WAL cannot be checkpointed past its snapshot).
  INV-051 mechanism and evidence, and under "Not covered": a run whose round one is fatal, stopped or whose
  post-check is interrupted never reads the history, so repeated short runs report no old hole (the backfill still
  finds them); a corrupt page in old history no longer stops the start (exit `1`), it shows as `unverified`.
  F-10 records the new numbers (Task 6).
- [ ] **Step 6: Commit** `feat(collector): run the post-check in the background from a round-one snapshot`.

### Task 6: Benchmark the new shape and record the numbers

**Files:**
- Modify: `tests/collector/bench_reconcile.py`, `tests/collector/test_reconcile_bench_smoke.py`, `docs/follow-ups.md`

**Interfaces:**
- Produces: the JSON gains `pre_start_ms` (window-only read, run first, before any full read) next to `first_run_ms`/`second_run_ms`
  (full read with history, as before); statement classes are `bounds, candles, conflicts, unavailable, holes,
  unavailable_in_holes, conflicted`.

- [ ] **Step 1: Failing test:** the smoke test asserts `pre_start_ms` is present, `>= 0`, and the class set above.
- [ ] **Step 2: Implement**, run `make check` → PASS.
- [ ] **Step 3: Run `make bench PROFILE=target`** (about 9 min). Record in F-10: `pre_start_ms`, `first_run_ms`, the
  per-statement times, C1 (full read ≤ 60 s), C2 (projection ≤ 180 s), C3, and that start-up no longer waits for the
  history. F-10 stays open only for what is still unmeasured (cold disk, soak database S23).
- [ ] **Step 4: Commit** `docs: record the reconcile read cost after the F-10 fixes`.
