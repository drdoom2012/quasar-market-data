# S8 Conflict Handling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When `CandleWriter` meets a re-delivered candle whose values differ from the stored one, it records the rejected observation in `candle_conflicts`, in the same transaction as the rest of the batch, idempotently, and still never touches the stored candle.

**Architecture:** S7 already detects a differing duplicate and reports it in `WriteReport.conflicts`. S8 adds one more statement to the same `BEGIN IMMEDIATE` transaction: `INSERT INTO candle_conflicts … ON CONFLICT DO NOTHING` for the incoming (rejected) values. The unique index `candle_conflicts_observation` (ADR-0002 P-5) makes recording idempotent, so every recheck and every restart that sees the same wrong value adds nothing. No schema change, no new module, no new public type.

**Tech Stack:** Python 3.12, stdlib `sqlite3`, pytest, mypy strict, ruff, import-linter. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-20-market-data-v1-design.md` (S8 in section 8, §6.3, §11 item 3). Approved decisions: `docs/adr/0002-sqlite-schema-v1.md` (C-3, P-4, P-5, Q3). Earlier plan: `docs/superpowers/plans/2026-10-22-s7-idempotent-candle-writer.md` (the writer this extends).

## Decisions made in this plan

1. **Recorded inside the batch transaction.** A conflict record and the batch's new candles commit or roll back together. If recording fails, the whole batch fails closed and can be retried (INV-030 applies unchanged). The alternative, a separate transaction, could commit candles while losing the conflict record.
2. **What is recorded is the rejected (incoming) observation only.** The accepted candle stays untouched in `candles`; the conflict is "stored candle versus this row" through the identity columns (ADR-0002 P-5).
3. **`first_seen_at_ms` is the `ingested_at_ms` argument of the batch that first saw the observation.** A later sighting of the same observation changes nothing, including that timestamp.
4. **`WriteReport` and `CandleConflict` keep their shape.** `conflicts` still lists every conflicting candle in the batch, including ones already recorded earlier; "reported" now implies "recorded in the same commit". A new `recorded` counter is not added (YAGNI: tests read the table; S9 can ask for it when it needs it).
5. **No read API for `candle_conflicts`.** Readers arrive with coverage (S11) and the API (S10). S8 tests use raw `SELECT`.
6. **Statement form is `INSERT … ON CONFLICT DO NOTHING`, not `INSERT OR IGNORE`.** ADR-0002 P-5 and its Consequences section name `INSERT OR IGNORE`, but S6 proved that `OR IGNORE` also swallows CHECK violations (`test_insert_or_ignore_would_swallow_an_invalid_conflict_record`) and S7 ruled `ON CONFLICT DO NOTHING` for the writer. S8 follows the S6/S7 ruling. The ADR wording is **not** edited here (accepted decision, needs approval); see "Needs your approval".

## Global Constraints

- Branch `market-data-init` (equal to `main` at `97f48d8` when this plan was written); one commit per task, message in each task; trailer `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. Do not push.
- No schema change, no new migration, no edit of `docs/adr/`, `pyproject.toml`, `uv.lock`, or the domain purity allowlist. `domain` and `storage` are not modified. No new dependency, framework or module.
- The writer imports only the stdlib, `domain` and `storage.connection` / `storage.schema` (unchanged from S7).
- Never `UPDATE`, `DELETE`, `INSERT OR REPLACE` or `INSERT OR IGNORE` on any table. A CHECK violation while recording must raise, not vanish.
- No clock reads: `first_seen_at_ms` is the caller's `ingested_at_ms`. All values are the candle's canonical decimal text; no `float` anywhere.
- Fail closed: a database error while recording a conflict fails and rolls back the whole batch; nothing from it is stored.
- A conflicted slot has no resolution path in v1 (ADR-0002 Q3): S8 adds none. The stored candle is never modified, deleted or replaced.
- Every guarantee introduced here has a targeted negative test, listed in the guarantee map. Tests use `tmp_path` databases only. `tests/` is mypy-strict. A PreToolUse hook runs `make check` on `git commit`; run `make check` before each commit and report real output.
- TDD: failing test first, record RED and GREEN output.

## Review Focus

Failure modes a reasonable user would expect handled, most likely first. Each has a test in the owning task.

1. The same wrong value delivered again, on every recheck and after a process restart → exactly one record, `first_seen_at_ms` unchanged (Task 1).
2. A conflict where `turnover` is `NULL` on one side and a value on the other, in both directions → recorded, and recorded once when repeated (Task 1; SQLite treats `NULL`s as distinct in unique indexes, the index uses `coalesce`).
3. A conflicted slot receives the original candle again → counted as a duplicate, no new record, stored candle untouched (Task 1).
4. Two different wrong values for the same slot, over time → two records, the full history (Task 1).
5. Recording fails midway through a batch → the batch's new candles are rolled back too, and retrying the batch stores everything and records the conflict once (Task 2).
6. The writer is used through `AsyncCandleWriter` → conflicts are recorded the same way (Task 2).
7. Some statement path quietly turns into `OR IGNORE`, `OR REPLACE`, `UPDATE` or `DELETE` → a statement-trace test fails (Task 1).

---

### Task 1: Record differing duplicates

**Files:**
- Modify: `src/quasar_market_data/collector/candle_writer.py`, `tests/collector/test_candle_writer.py`

**Interfaces:**
- Consumes: `CandleWriter.write`, `WriteReport`, `CandleConflict` (S7, unchanged), the test helpers already in `tests/collector/test_candle_writer.py` (`valid(n, **fields)`, `writer`, `writer_conn`, `AT`).
- Produces: no new public names. In `candle_writer.py`, a module constant `_RECORD_CONFLICT` (statement text) and the extended behavior of `write`: for every candle that becomes a `CandleConflict`, the incoming values are inserted into `candle_conflicts` inside the same transaction.

Semantics. The statement is `INSERT INTO candle_conflicts (venue, symbol, timeframe, open_time_ms, open, high, low, close, volume, turnover, first_seen_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING`, bound to the slot, the **incoming** values and `ingested_at_ms`. It runs right after the conflict is detected, before the next candle. `WriteReport` is unchanged. Update the module docstring and the `CandleConflict` docstring: a conflict is now reported **and recorded**; remove the sentence that says recording is S8.

- [ ] **Step 1: Write failing tests** in `tests/collector/test_candle_writer.py`. First **replace** `test_conflicts_record_nothing_yet` (it asserts the S7 boundary and must not survive). Helper `conflict_rows(conn)` = `SELECT venue, symbol, timeframe, open_time_ms, open, high, low, close, volume, turnover, first_seen_at_ms FROM candle_conflicts ORDER BY conflict_id`.
  - `test_differing_duplicate_is_recorded_not_overwritten`, parametrized over `open, high, low, close, volume, turnover` (ids = field names; for `turnover` both `None → value` and `value → None`): after the conflicting write, `conflict_rows` holds exactly one row equal to the **incoming** values with `first_seen_at_ms == ingested_at_ms` of that write; the `candles` row is unchanged, including its `ingested_at_ms`.
  - `test_same_conflict_delivered_again_is_recorded_once`, parametrized over `turnover` = `None` and `"5"`: write the stored candle, then the same conflicting candle three times with increasing `ingested_at_ms`, the third through a **new** `open_writer` connection and `CandleWriter` on the same file (restart) → one row, `first_seen_at_ms` equals the first sighting; every write still reports the conflict in `WriteReport.conflicts`.
  - `test_each_distinct_wrong_value_is_kept`: conflicts with `close="100.1"` and `close="100.2"` for the same slot → two rows; the stored candle is unchanged.
  - `test_original_after_a_conflict_is_a_duplicate_and_records_nothing`: stored `c`, conflicting `c'` recorded, then `c` again → `WriteReport(0, 1, ())`, still one conflict row.
  - `test_conflict_inside_one_batch_is_recorded`: `[c, c_changed]` → one insert, one conflict, one row equal to `c_changed`; `[c, c_changed, c_changed]` → two reported conflicts, one row.
  - `test_conflict_does_not_stop_the_rest_of_the_batch` (existing, keep): also assert `conflict_rows` has one row.
  - `test_writer_never_issues_overwriting_statements`: install `writer_conn.set_trace_callback(statements.append)`, run a batch with new, duplicate and conflicting candles, then assert no captured statement, uppercased, contains `OR IGNORE`, `OR REPLACE`, ` UPDATE ` or `DELETE`, and every statement that starts with `INSERT` contains `ON CONFLICT DO NOTHING` (the `BEGIN`/`COMMIT`/`SELECT` statements are ignored).
- [ ] **Step 2: Run** `uv run pytest tests/collector/test_candle_writer.py -v`. Expect: the new tests FAIL (no rows recorded), everything else still passes.
- [ ] **Step 3: Implement** `_RECORD_CONFLICT` and the extra `execute` in the conflict branch of `write` in `candle_writer.py`; update the two docstrings.
- [ ] **Step 4: Mutation check (evidence, not committed):** (a) remove the record statement, (b) record with a plain `INSERT` (no `ON CONFLICT DO NOTHING`), (c) change the statement to `INSERT OR IGNORE`, (d) bind the **stored** values instead of the incoming ones; show that respectively `test_differing_duplicate_is_recorded_not_overwritten`, `test_same_conflict_delivered_again_is_recorded_once`, `test_writer_never_issues_overwriting_statements` and `test_differing_duplicate_is_recorded_not_overwritten` fail; restore; record the four outputs.
- [ ] **Step 5: Run** the tests, then `make check`. Expect: pass; record output.
- [ ] **Step 6: Commit** `feat(collector): record differing duplicates in candle_conflicts`.

### Task 2: Atomic with the batch, and through the asyncio wrapper

**Files:**
- Modify: `tests/collector/test_candle_writer.py`, `tests/collector/test_async_candle_writer.py`; `src/quasar_market_data/collector/candle_writer.py` only if a test shows the Task 1 code is not already atomic.

**Interfaces:**
- Consumes: Task 1 behavior; `FaultyConnection` / `open_faulty_writer` (`tests/collector/faulty_connection.py`, S7); `AsyncCandleWriter.open` / `write` / `close` (S7).
- Produces: no new names. This task pins the atomicity and the async path with tests; production code changes only if they fail.

- [ ] **Step 1: Write failing tests.**
  - In `test_candle_writer.py`, `test_failure_while_recording_a_conflict_rolls_back_the_whole_batch`: stored `c`; on the writer connection `CREATE TEMP TRIGGER boom BEFORE INSERT ON main.candle_conflicts BEGIN SELECT RAISE(ABORT, 'boom'); END`; batch `[new0, conflicting c', new2]` → `CandleWriteError` with `__cause__` a `sqlite3.Error`; `candles` still holds only `c`, `candle_conflicts` is empty, not in a transaction. After `DROP TRIGGER boom`, writing the same batch stores `new0` and `new2`, records one conflict row, and reports two inserts and one conflict.
  - In `test_candle_writer.py`, `test_commit_failure_after_a_recorded_conflict_leaves_no_record`: `FaultyConnection` fails `COMMIT` once on a batch `[new, conflicting]` → `CandleWriteError`; `candle_conflicts` is empty and `new` is not stored; the same batch written again succeeds with one record.
  - In `test_async_candle_writer.py`, `test_conflict_is_recorded_through_the_async_writer`: open the wrapper, write `c`, write `c'` twice → the second report lists the conflict; after `close()` a raw connection sees one `candle_conflicts` row with the incoming values and the stored candle unchanged.
- [ ] **Step 2: Run** `uv run pytest tests/collector -v`. Expect: normally PASS already, because Task 1 runs inside the batch transaction. If one fails, that is a real atomicity defect: fix it in `candle_writer.py` and say so in the report.
- [ ] **Step 3: Mutation check (evidence, not committed):** run the record statement after `COMMIT` on the same connection (outside the transaction); show that `test_failure_while_recording_a_conflict_rolls_back_the_whole_batch` and `test_commit_failure_after_a_recorded_conflict_leaves_no_record` fail; restore; record the outputs. This proves the tests can fail, so an already-green Step 2 is meaningful.
- [ ] **Step 4: Run** the tests, then `make check`. Expect: pass; record output.
- [ ] **Step 5: Commit** `test(collector): conflict records are atomic with the batch and work through the async writer`.

### Task 3: Register and README

**Files:**
- Modify: `docs/invariants.md`, `README.md`

- [ ] **Step 1: Update the register.** An ENFORCED row cites a test that fails when the invariant is violated; positive tests and controls are not evidence.
  - `INV-009` (PLANNED → ENFORCED): conflicting market data is never silently overwritten; a differing duplicate is rejected and recorded. Mechanism: read-and-compare plus `INSERT … ON CONFLICT DO NOTHING` into `candle_conflicts` in `candle_writer.py`, and the database immutability triggers (INV-025) for every other path. Evidence: `tests/collector/test_candle_writer.py::test_differing_duplicate_is_recorded_not_overwritten`, `tests/collector/test_candle_writer.py::test_differing_duplicate_is_reported_and_never_overwritten`. Scope in the row: a conflicted slot has no resolution path in v1 (ADR-0002 Q3); reporting a slot as conflicted to readers is S11 and the indicator refusal is S18 (INV-011).
  - New `INV-034` (ENFORCED): recording a conflict is idempotent and atomic with its batch; re-delivery and restart add no record, and a failure while recording rolls the whole batch back. Evidence: `tests/collector/test_candle_writer.py::test_same_conflict_delivered_again_is_recorded_once`, `tests/collector/test_candle_writer.py::test_failure_while_recording_a_conflict_rolls_back_the_whole_batch`, `tests/collector/test_candle_writer.py::test_writer_never_issues_overwriting_statements`. Scope: failures are injected (temporary trigger, fault-injecting connection).
  - Reword, keeping the status: `INV-029` (remove "recording the conflict is S8"; recording is now INV-034), `INV-012` (conflict recording is idempotent per INV-034; checkpoints and restart reconcile stay S15). Do not change any other row.
- [ ] **Step 2: Update `README.md`:** one line saying that a differing duplicate is recorded in `candle_conflicts` and the stored candle is never changed; no new commands.
- [ ] **Step 3: Run** `uv run pytest tests/test_invariant_register.py -v`, then `make check`. Expect: pass; record output.
- [ ] **Step 4: Commit** `docs: record S8 invariants and conflict handling`.

**S8 Definition of Done:** `make check` ran green and its output is in the report; every row of the guarantee map has its negative test and its mutation output; the writer never issues `UPDATE`, `DELETE`, `OR REPLACE` or `OR IGNORE`; `test_conflicts_record_nothing_yet` no longer exists; the register, docstrings and README no longer say that recording is S8; no file under `domain/`, `storage/` or `docs/adr/` changed.

## Guarantee map

| Guarantee | Targeted negative test | Mutation that must break it (reported) |
|---|---|---|
| A differing duplicate is recorded with the incoming values, any one field differing | `test_differing_duplicate_is_recorded_not_overwritten[...]` | Task 1 (a), (d) |
| The stored candle is never changed by a conflict | same test (asserts the `candles` row), `test_differing_duplicate_is_reported_and_never_overwritten` (S7) | n/a (S7 mutations) |
| Recording is idempotent across redelivery and restart; `first_seen_at_ms` is kept | `test_same_conflict_delivered_again_is_recorded_once[...]` | Task 1 (b) |
| `NULL` versus value turnover is recorded and deduplicated correctly | the `turnover` cases of the two tests above | Task 1 (b) |
| Every distinct wrong value is kept | `test_each_distinct_wrong_value_is_kept` | n/a (asserts two rows) |
| The original after a conflict is a duplicate and adds no record | `test_original_after_a_conflict_is_a_duplicate_and_records_nothing` | n/a |
| Conflicts inside one batch are recorded | `test_conflict_inside_one_batch_is_recorded` | n/a |
| No overwriting statement form is ever issued | `test_writer_never_issues_overwriting_statements` | Task 1 (c) |
| A failure while recording rolls back the whole batch; retry is safe | `test_failure_while_recording_a_conflict_rolls_back_the_whole_batch`, `test_commit_failure_after_a_recorded_conflict_leaves_no_record` | Task 2 (after-`COMMIT` recording) |
| The async writer records conflicts the same way | `test_conflict_is_recorded_through_the_async_writer` | n/a |

## Needs your approval

- **ADR-0002 wording.** P-5 and the Consequences section say conflicts are recorded with `INSERT OR IGNORE`. The implementation uses `INSERT … ON CONFLICT DO NOTHING` (S6 ruling, S7 precedent) because `OR IGNORE` hides CHECK violations. The behavior the ADR specifies (idempotent recording through the unique index) is unchanged. This plan does not edit the accepted ADR; a one-line docs-only amendment is proposed if you want the text to match the code.

## Risks and boundaries

- **Contract:** `WriteReport` / `CandleConflict` unchanged; only their meaning gains "and recorded". No public contract file exists yet (`docs/contracts/` is absent), so nothing to update there.
- **Data integrity:** the `candle_conflicts` schema has no range checks on `open_time_ms` or length checks on `venue`/`symbol`/`timeframe` (unlike `candles`). Values come from a validated `Candle` that already satisfies the same rules, so a bad record cannot come from this writer; tightening the table would be a schema change and is out of scope.
- **Growth:** a source that keeps emitting new, different wrong values for one slot adds one row per distinct value, bounded by what the exchange sends. There is no retention (ADR-0002 P-4).
- **Security:** none; no network, credentials or new inputs.

## Self-review notes

- **Spec coverage:** S8 "differing duplicate rejected and recorded, never overwritten" → Task 1 (recorded, not overwritten), Task 2 (atomic), Task 3 (register INV-009). Spec §6.3 / ADR C-3 "rejected and recorded in a table" → Task 1. "Reported as conflicted and indicators refuse" is explicitly S11 / S18, not S8.
- **Type consistency:** `CandleWriter`, `WriteReport`, `CandleConflict`, `CandleWriteError`, `FaultyConnection`, `open_faulty_writer`, `AsyncCandleWriter` are the S7 names used unchanged; the only new name is the private `_RECORD_CONFLICT`.
- **Not in scope:** reading conflicts, coverage and the conflicted state (S11), indicator refusal (S18), conflict resolution (later ADR), `unavailable_candles` writes (S16), eligibility wiring and `fetch-once` (S9), any schema or ADR change.
