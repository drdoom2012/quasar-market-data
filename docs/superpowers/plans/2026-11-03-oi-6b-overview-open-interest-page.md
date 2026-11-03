# OI-6b: open interest on the symbol overview page — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (the user chose inline execution) to implement this plan task by task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** the local symbol overview page (`tools/symbol_overview/`) shows open interest like candles: a second strip per symbol, its own summary row, reasons from both sources in the problem list, in a two-phase refresh that leaves the candle part exactly as it is.

**Architecture:** the tool stays a stdlib-only, separate HTTP consumer (`tests/tools/symbol_overview/test_boundary.py`). New pure functions validate and judge a `GET /v1/open-interest/coverage` answer (the endpoint of OI-6a, contract 6.6); `refresh.py` runs the candle phase unchanged and then an open interest phase under the same 60 s deadline; `build_overview` gains an optional open interest argument, so a call without it returns today's object byte for byte.

**Tech Stack:** Python 3.12 standard library only in `tools/symbol_overview/`, pytest with the existing fake API (`tests/tools/symbol_overview/fake_api.py`), plain HTML/JS in `page.html`.

**Spec:** `docs/superpowers/specs/2026-11-03-oi-6-open-interest-overview-design.md` (approved 2026-11-03; sections 5, 8, 9; U2 = option C; G0 and G1 measured, section 14). Contract: `docs/contracts/api-v1.md` 6.5.8 (health object), 6.6 (the endpoint). Same branch and the same PR as OI-6a. **Recorded decisions that override the design's slicing (sections 8.9, 10, 13) and its gate wording:** the user wrote in this session, in answer to the proposal to open a PR for OI-6a alone, "tak to teda ne rekl jsem at to udelas najednou ne rozdeleni na 6a a 6b" (do it at once, not split into 6a and 6b), after asking "tak proc to teda nedokoncis?" (why do you not finish it). I read that as: one branch, one PR, and OI-6b is written and implemented without waiting for OI-6a to be merged. **G1 was therefore run on this branch (2026-11-03, design section 14), before the merge**; AC1, AC2, AC4, AC5 and AC6 pass on loopback; the user has not yet read the numbers, **AC7 (the tunnel) is open until the user supplies `L_tunnel`**, and G2 is measured in Task 6. Task 6 records all of this in the design (status and change history). Nothing is merged: the PR is opened only after the final review and merged only on the user's word.

## Global Constraints

- The tool imports only the standard library (`ALLOWED` in `tests/tools/symbol_overview/test_boundary.py`) and nothing from `quasar_market_data`; contract values are repeated in `model.py` with their source.
- **Every existing test of the tool passes unedited.** No existing test file is edited; new tests go in new files.
- **Candle behavior is identical whatever open interest does:** the candle phase is today's code path and finishes first; the candle `state`, `reasons`, `strip`, candle `summary` and candle columns never change because of open interest.
- `build_overview(health, window, reports, duration_ms=…, deadline_hit=…)` called as today returns exactly today's object (no new key). New keys appear only when its new optional argument is passed.
- The page decides no state and has no clock of its own; the tool compares no millisecond value against a threshold. Clock checks only bound consistency: `T0 <= A <= T0 + REFRESH_DEADLINE_MS + CALL_TIMEOUT_MS`.
- Open interest `ok` only for a validated answer with no real finding; every other path is `unknown`, `no_data` or "disabled".
- One deadline of 60 s from the start of the refresh, checked before every call in both phases; at most 4 calls in flight; a new connection per call; allowed paths gain `/v1/open-interest/coverage` and nothing else; `MAX_BODY_BYTES` unchanged (2 000 000).
- No new config key, no database migration, no change to the API.
- Page text is English (U3). Unrecoverable holes: option C (U2): reason `unrecoverable` is information only, strip `r`, no headline state, no tile.

## Review Focus

1. The health object is missing or malformed (an API without OI-5, `open_interest: 3`, `as_of_ms != T0`, a rule that differs from the top-level `lag_threshold_ms`): whole open interest part `unknown`, candle part untouched, zero open interest calls (Task 3, Task 5).
2. `O` against the window: before `S'`, exactly `S'`, one slot after, in the middle, at the last slot `E`, and `N >= E'` (`clock_inconsistent`, never an `ok` strip of `_`) (Task 2, Task 3).
3. A slow API: the candle phase is identical to a candle-only refresh and unreached open interest symbols are `unknown (deadline)` for open interest only; no call starts after the deadline in either phase (Task 5).
4. An answer the API can legally produce is never rejected: touching runs of different states, a conflict without a row before `O`, between, and after `N`, `A == T0 + 70 s`, two runs of one state separated by a present slot (Task 2).
5. The largest feasible body (the page window has 8 640 slots, so at most 8 640 runs; about 637 KB measured at G1) decodes under the cap, and a judged report keeps no runs (Task 4).

## File map

- Modify: `tools/symbol_overview/model.py` (constants, dataclasses), `window.py` (`open_interest_window`, `build_open_interest_strip`), `validate.py` (`open_interest_health`, `open_interest_coverage_result`), `judge.py` (`judge_open_interest`, `build_overview` option, `open_interest_summary`), `fetch.py` (`ALLOWED_PATHS`, `open_interest_path`), `refresh.py` (two phases), `page.html`, `README.md`.
- Create: `tests/tools/symbol_overview/oi_helpers.py`, `test_oi_window.py`, `test_oi_validate.py`, `test_oi_judge.py`, `test_oi_refresh.py`, `test_oi_page.py`, `test_oi_against_real_api.py`.
- Docs: design section 13 history + status, ADR-0003 P-16 row, `docs/reference.md` one sentence if the tool is described there.

## Interfaces (shared by the tasks)

```python
# model.py
OI_PATH = "/v1/open-interest/coverage"
OI_STATE_CHARS = {"not_yet_expected": "n", "no_longer_recoverable": "r", "due": "d", "missing": "m", "conflicted": "c", "stale": "s"}
OI_STRIP_RANK = "_.rndmcs"           # lowest to highest
OI_RUN_STATES = ("conflicted", "no_longer_recoverable", "missing", "not_yet_expected", "due", "stale")
OI_STATE_RANK = {"no_longer_recoverable": 0, "missing": 1, "stale": 2, "due": 3, "not_yet_expected": 4}  # P8

@dataclass(frozen=True) class OiRule: cadence: int; expected_after_ms: int; lag_threshold_ms: int
@dataclass(frozen=True) class OiHealth: rule: OiRule            # health.open_interest, validated
@dataclass(frozen=True) class OiCoverage:
    as_of_ms: int; retrieval_horizon_ms: int; oldest_time_ms: int; newest_time_ms: int
    present_slots: int; conflicted_slots: int; not_yet_expected_slots: int; no_longer_recoverable_slots: int
    due_slots: int; stale_slots: int; missing_slots: int; runs: tuple[Run, ...]
@dataclass(frozen=True) class OiReport:                          # one symbol's open interest, judged
    state: str; reasons: tuple[Reason, ...]; oldest_time_ms: int | None; newest_time_ms: int | None
    as_of_ms: int | None; retrieval_horizon_ms: int | None; strip: str | None

# window.py
def open_interest_window(window: Window) -> Window                # Window(window.start + TF_MS, window.end + TF_MS)
def build_open_interest_strip(runs: Sequence[Run], oldest_time_ms: int, oi_window: Window) -> str

# validate.py
def open_interest_health(status: int, content_type: str | None, body: bytes, *, health: Health) -> OiHealth | None | Invalid
    # None = disabled (the key is present and null); Invalid = absent or malformed: the whole open interest part is unknown
def open_interest_coverage_result(status, content_type, body, *, instrument: InstrumentRow, oi_window: Window,
                                  health: Health, oi_health: OiHealth) -> OiCoverage | Invalid
    # a 404 error body with code unknown_series comes back as Invalid("api_error", "unknown_series") (the caller turns it into no_data)

# judge.py
def judge_open_interest(instrument: InstrumentRow, outcome: OiCoverage | Invalid, oi_window: Window) -> OiReport
def open_interest_summary(reports: Sequence[OiReport]) -> dict[str, int]       # {"total", *STATE_ORDER}
def build_overview(health, window, reports, *, duration_ms, deadline_hit, open_interest: OiPart | None = None) -> dict
@dataclass(frozen=True) class OiPart: status: str; detail: str; reports: tuple[OiReport, ...]; deadline_hit: bool; candle_not_asked: int; oi_not_asked: int
    # status: "disabled" | "unknown" | "enabled"

# fetch.py
def open_interest_path(instrument: InstrumentRow, oi_window: Window) -> str
```

Reason codes of an open interest symbol: `no_data`, `invalid` (detail `<code>[:<detail>]`), `stale`, `conflict`, `missing`, `due`, `unrecoverable`. Headline order is the candle one: `no_data, unknown, stale, conflict, holes, behind, ok`; `unrecoverable` maps to **no** headline state.

---

### Task 1: Types, constants and the shifted window with its strip

**Files:** Modify `tools/symbol_overview/model.py`, `window.py`, `fetch.py`; create `tests/tools/symbol_overview/oi_helpers.py`, `test_oi_window.py`.

**Interfaces:** Produces the block above for `model.py`, `window.py`, `fetch.py`. Consumes `Window`, `Run`, `window_for`, `TF_MS`, `BUCKETS`.

- [ ] **Step 1: Failing tests** (`test_oi_window.py`; `oi_helpers.py` holds `oi_doc(...)` of Task 2, written there and imported here only for constants `T0`, `S`, `E` from `window_for(T0)`):
  - `test_the_open_interest_window_is_shifted_by_one_period`: `open_interest_window(Window(S, E)) == Window(S + TF_MS, E + TF_MS)` and has 8 640 slots.
  - `test_a_bucket_of_both_strips_is_the_same_hour`: for slot time `T = S + 12·k·TF_MS + TF_MS` (first slot of OI bucket k) the candle strip bucket of the candle opened at `T − TF_MS` equals `k` (compute both indices with the two window definitions).
  - `test_the_strip_has_720_characters_and_the_alphabet_of_open_interest`: all present → `"." * 720` with `oldest` at `S'`; a `no_longer_recoverable` run after `O` gives `r`; `not_yet_expected`, `due`, `missing`, `conflicted`, `stale` give `n d m c s`.
  - `test_the_worst_state_of_a_bucket_wins_in_the_documented_order`: one bucket holding runs of every state: ranks `_ < . < r < n < d < m < c < s` (pairwise: `r` outranks `.`, `n` outranks `r`).
  - `test_old_holes_are_clipped_at_the_oldest_row_inside_a_bucket`: `O = S' + 18·TF_MS` (inside bucket 1, which spans slots 12 to 23): a `missing` run `[S', O)` gives bucket 0 `_`, and bucket 1 is `.`, not `m` (the part before `O` is clipped; this fails without the clip); the same with a `no_longer_recoverable` run `[S', O)`; a `no_longer_recoverable` run starting at `O + TF_MS` gives `r` in its bucket; a `conflicted` run before `O` stays `c` and a `not_yet_expected` run before `O` stays `n` (findings or pending, not pre-history). Whole buckets before `O` are `_` (also with `O = S' + 24·TF_MS`).
  - `test_a_run_outside_the_window_or_misaligned_is_refused`: `ValueError` for `end > E'`, `start < S'`, unaligned, and a state not in `OI_STATE_CHARS` (`present`, `unavailable`).
  - `test_open_interest_path_has_exactly_the_documented_parameters`: `open_interest_path(instrument, w) == "/v1/open-interest/coverage?venue=binance-usdm&symbol=BTC/USDT:USDT&period=5m&start=…&end=…"`, encodes `&`, `+`, space, non-ASCII like `coverage_path`; `"/v1/open-interest/coverage"` is in `ALLOWED_PATHS` and `http_fetch` still refuses any other path.
- [ ] **Step 2: Run** `uv run pytest tests/tools/symbol_overview/test_oi_window.py -q`. Expected: FAIL (names missing).
- [ ] **Step 3: Implement** the constants and dataclasses in `model.py` (nothing existing changes); `open_interest_window`, `build_open_interest_strip` in `window.py`: the same algorithm as `build_strip` with `OI_STATE_CHARS`/`OI_STRIP_RANK`, a `missing` or `no_longer_recoverable` run clipped to start at `max(run.start, oldest)`; `open_interest_path` and the `ALLOWED_PATHS` entry in `fetch.py`.
- [ ] **Step 4: Run** the new file and `uv run pytest tests/tools -q`. Expected: PASS, no existing test edited. `make lint typecheck`.
- [ ] **Step 5: Commit:** `feat: OI-6b open interest window, strip and path`.

### Task 2: Validation of the health object and of an open interest answer

**Files:** Modify `tools/symbol_overview/validate.py`; extend `oi_helpers.py`; create `test_oi_validate.py`.

**Interfaces:** Consumes Task 1. Produces `open_interest_health`, `open_interest_coverage_result` (signatures above).

- [ ] **Step 1: Failing tests.** `oi_helpers.py`: `health_with(open_interest: object, as_of_ms=T0, rule=RULE) -> bytes` (the `health_doc` of `helpers.py` plus the key; the key omitted when `open_interest` is the sentinel `ABSENT`), `oi_rule_doc()`, and `oi_doc(oi_window, *, oldest, newest, rows=None, conflicts=(), as_of_ms=T0 + 1000, ...)` that builds a self-consistent answer with **the same slot model as the API** (rows present; conflict first; slots after `as_of` minus expected-after are `not_yet_expected`; no row after `newest` is `due`/`stale`; before `newest` `missing`/`no_longer_recoverable` beyond a given `horizon`), with runs grouped so touching runs differ in state and `slot_states` overrides for one-inconsistency tests (like `coverage_doc`). Tests in `test_oi_validate.py`:
  - Health: `test_the_health_object_null_is_disabled` (`open_interest_health(...) is None`); object with `as_of_ms == T0` and a well-formed rule → `OiHealth(OiRule(...))`; `test_a_missing_or_malformed_health_object_is_invalid_schema` (key absent, `3`, `[]`, `as_of_ms != T0`, `as_of_ms` a string, rule with a missing or negative or boolean field, `rule.lag_threshold_ms != health.rule.lag_threshold_ms` → `Invalid("rule_mismatch")`); every other case `Invalid("schema")`.
  - Answer, accepted: `test_a_valid_answer_is_accepted` (counts and the six fields), `test_an_answer_the_api_produces_always_validates`: 2 000 seeded random series whose answers come from **the API's own pure function** `quasar_market_data.domain.open_interest_coverage.classify_open_interest_runs` (tests may import it; only the tool's sources are scanned by `test_boundary.py`), serialized to the body of contract 6.6.3 by a small test helper (key names and run `slots` as the contract shows): rows, conflicts, `A` from `T0` to `T0 + 70 s`, a rule with a random `retrieval_horizon_ms`, `O` and `N` before, inside and after the window up to `N < E'`; every generated answer validates. The hand-built `oi_doc` is used only for single-inconsistency tests, `test_no_false_rejection` (two runs of one state separated by a present slot; `A == T0 + 70_000` exactly; a conflict without a row before `O`, between and after `N`; a `not_yet_expected` run before `O`; touching runs of different states).
  - Rejected, each `Invalid` with the stated code and never accepted: `echo` (wrong venue, symbol, period, start, end, `time_semantics`); `schema` (missing or wrongly typed fields; `O > N`; unaligned `O`/`N`; rule fields); `counts` (`slots != 8640`, counts that do not sum, `gap_free` disagreeing with `present_slots == slots`); `runs` (unknown or `present` state, unaligned, outside the window, unordered or overlapping, declared `slots` wrong, per-state sums differing from counts, **touching runs of one state**); `inconsistent_runs` (a non-`conflicted` run containing `O` or `N`); `runs` (a `due`/`stale` run that lies entirely at or before `N` (a run that merely contains `N` is `inconsistent_runs`, so the test uses a run ending at `N`); a `missing` run that lies entirely after `N`; a slot before `O` or after `N` in no run; a `due` or `stale` run before `O`; a `missing` run after `N`; **order violations** `[not_yet_expected][missing]`, `[due][stale]` after `N`, `[missing][no_longer_recoverable]`); `rule_mismatch` (cadence, expected_after_ms or lag_threshold_ms differ from `oi_health`; `retrieval_horizon_ms` is not compared); `clock_inconsistent` (`A < T0`, `A == T0 + 70_001`, `N >= E'`, `O >= E'`).
  - Statuses: `test_a_404_unknown_series_comes_back_as_its_own_invalid` (`Invalid("api_error","unknown_series")`), `404 open_interest_disabled` → `Invalid("api_error","open_interest_disabled")`, `500`, bad content type, bad JSON, body over 2 000 000 bytes → `too_large`.
- [ ] **Step 2: Run** `uv run pytest tests/tools/symbol_overview/test_oi_validate.py -q`; expected FAIL.
- [ ] **Step 3: Implement** both functions in `validate.py`, reusing `_decode`, `_bad`, `_is_int`, `_non_negative`, `_ts`, `_aligned_ts`, `_runs`; structure and position checks follow design 8.5 exactly (the first position rule returns `inconsistent_runs`, the others `runs`); coverage of the regions before `O` and after `N` by summed run lengths as `coverage_result` does for candles; P8 on the runs with `conflicted` removed using `OI_STATE_RANK`. No existing function changes.
- [ ] **Step 4: Run** the new file, `uv run pytest tests/tools -q`, `make lint typecheck`. Expected PASS.
- [ ] **Step 5: Commit:** `feat: OI-6b validation of the open interest answers`.

### Task 3: Judging one symbol and the overview object

**Files:** Modify `tools/symbol_overview/judge.py`; create `test_oi_judge.py`.

**Interfaces:** Consumes Tasks 1 and 2. Produces `judge_open_interest`, `open_interest_summary`, `OiPart`, the `open_interest` argument of `build_overview`.

- [ ] **Step 1: Failing tests** (`test_oi_judge.py`, answers built with `oi_doc` then `open_interest_coverage_result`):
  - Reasons and headline (design 8.4): `test_stale_conflict_and_holes_together` (headline `stale`, reasons in the order `stale`, `conflict`, `missing`); `test_due_alone_is_behind`; `test_not_yet_expected_alone_is_ok`; `test_missing_before_the_oldest_row_is_not_a_hole` (a `no_longer_recoverable` run followed by a `missing` run, both ending at `O`, in the order P8 allows: state `ok`, no reason); `test_a_conflict_before_the_oldest_row_is_a_finding`; `test_missing_and_unrecoverable_together_give_holes_and_both_reasons`; `test_unrecoverable_alone_is_ok_with_the_note` (a `no_longer_recoverable` run starting at `O + TF_MS`: state `ok`, reasons `[unrecoverable]`, strip contains `r`; the reason carries its slots and runs; `retrieval_horizon_ms` is in the report); `test_no_longer_recoverable_after_the_newest_row_is_not_a_reason`; `test_a_conflict_without_a_row_is_reported_as_conflict_never_pre_history` (before `O`, between, after `N`).
  - `test_the_position_of_the_oldest_row_against_the_window` (parametrized through `open_interest_coverage_result` and `judge_open_interest`, answers from the API's pure function): `O` far before `S'`; `O == S' - TF_MS`; `O == S'`; `O == S' + TF_MS` (one pre-history slot, no reason); `O` in the middle; `O == E` (the last slot: every earlier slot is in runs; the strip is `.` only in the last bucket); for each, assert state, reasons and the strip prefix (`_` only for buckets wholly before `O`); `O >= E'` or `N >= E'` → `Invalid("clock_inconsistent")` and, judged, `unknown`, never a strip of `_`.
  - `test_no_data_and_invalid_outcomes`: `Invalid("api_error","unknown_series")` → state `no_data`, reason `no_data`, `strip is None`; any other `Invalid` → state `unknown`, reason `invalid` with detail `code[:detail]`, `strip is None`; `Invalid("deadline")` → `invalid` detail `deadline`.
  - `test_the_strip_matches_the_runs_and_the_oldest_row`.
  - `test_summary_counts_each_symbol_once_by_headline` and `test_summary_has_every_state_key_with_zero`: `open_interest_summary` has `total` and the seven candle `STATE_ORDER` keys; no `unrecoverable` key.
  - Overview: `test_build_overview_without_open_interest_is_todays_object` (call as the existing tests do and compare with a golden copy built from the same pieces: no `open_interest` key at top level or in a symbol, `refresh` exactly `{"duration_ms", "deadline_hit"}`); `test_build_overview_with_open_interest_adds_only_new_keys` (every existing key and value unchanged; top-level `open_interest` = `{"status", "detail", "summary" | None, "window": {"start","end"} (the shifted one)}`; per symbol `open_interest` = `None` when disabled, else `{"state","reasons","oldest_time_ms","newest_time_ms","as_of_ms","retrieval_horizon_ms","strip"}`; `refresh` gains `candle_not_asked`, `oi_deadline_hit`, `oi_not_asked` whenever an `OiPart` is passed (`oi_deadline_hit` false and `oi_not_asked` 0 for `disabled` and `unknown`), and keeps `deadline_hit` candle-only; `OiPart.reports` is aligned by position with the candle reports and `build_overview` raises `ValueError` on a length mismatch); `disabled`: `summary` is `None` and symbols' blocks are `None`; `unknown`: every symbol `unknown`/`invalid`, summary all `unknown`.
  - `test_the_candle_part_is_identical_with_and_without_open_interest`: build both objects from the same candle reports; remove the added keys from the second; equal.
- [ ] **Step 2: Run** `uv run pytest tests/tools/symbol_overview/test_oi_judge.py -q`; expected FAIL.
- [ ] **Step 3: Implement.** `judge_open_interest` classifies each run after Task 2's validation: `before_O` slots of `missing`/`no_longer_recoverable` are excluded (clipped to `max(start, O)`); class H = `missing` after clipping, `hole_runs` its runs; class U = `no_longer_recoverable` slots after clipping that lie before `N`; reasons as in the table of design 8.4 in the order `stale`, `conflict`, `missing`, `due`, `unrecoverable`; the headline from the reasons except `unrecoverable`; `build_open_interest_strip` for the strip. `build_overview` keeps its body and adds the keys only when `open_interest` is not `None`. Define `OiPart` in `judge.py`.
- [ ] **Step 4: Run** the new file, `uv run pytest tests/tools -q`, `make lint typecheck`. Expected PASS (existing `test_judge.py` unedited).
- [ ] **Step 5: Commit:** `feat: OI-6b judging of open interest and the overview object`.

### Task 4: The two-phase refresh

**Files:** Modify `tools/symbol_overview/refresh.py`; extend `oi_helpers.py` (fake API routes); create `test_oi_refresh.py`.

**Interfaces:** Consumes Tasks 1 to 3, `FakeApi`, `Overviewer`. Produces the open interest phase inside `Overviewer._refresh`.

- [ ] **Step 1: Failing tests** (real HTTP fake API on 127.0.0.1, the pattern of `test_refresh.py`; fake monotonic clock where a deadline matters):
  - `test_disabled_makes_no_open_interest_call` (`open_interest: null`: `api.count("/v1/open-interest/coverage") == 0`; overview `open_interest.status == "disabled"`; every symbol's block `None`; the candle part equals a refresh against an API with the same candle routes and a health without the key, apart from the added keys).
  - `test_a_malformed_or_missing_health_object_makes_the_open_interest_part_unknown` (parametrized: key absent, `3`, `as_of_ms != T0`, rule mismatch): zero open interest calls, status `unknown`, each symbol's open interest `unknown`, the candle states and strips exactly as in a candle-only refresh.
  - `test_every_instrument_gets_an_open_interest_call_with_the_shifted_window` (an instrument with no stored candle is asked too: `404 unknown_series` → `no_data`; the query has `period=5m`, `start == S + TF_MS`, `end == E + TF_MS`).
  - `test_candles_run_first` (the recorded request order: every `/v1/coverage` before every `/v1/open-interest/coverage`; `/health/ingest`, `/v1/instruments` first).
  - `test_a_failed_candle_call_does_not_skip_the_open_interest_call_and_the_reverse` (500 on one, malformed body on the other: only that part is `unknown`).
  - `test_the_candle_part_is_identical_to_a_candle_only_refresh_whatever_open_interest_costs`: a fake clock where each open interest call advances time by 30 s: the candle summary, states, reasons, strips and `deadline_hit` equal those of the same API with open interest disabled; unreached open interest symbols are `unknown` with reason detail `deadline`; `oi_deadline_hit` is true and `oi_not_asked` equals their number; `deadline_hit` is `False` and `candle_not_asked == 0`.
  - `test_no_call_starts_after_the_deadline_in_either_phase` (fake clock; records of `api.requests` after the deadline time: none).
  - `test_a_partial_snapshot_is_a_200_and_cached_for_15_seconds_and_nothing_older_is_served` (two `get()` calls within 15 s do one refresh; after expiry a new one runs and an open interest answer of the first refresh is not reused).
  - `test_at_most_four_calls_are_in_flight_across_both_phases` (`api.max_in_flight <= 4` with `delay_s`).
  - `test_only_the_allowed_paths_are_requested`.
  - `test_a_judged_open_interest_report_holds_no_runs` (a limited structural check, as design 9 allows: `OiReport` is a dataclass without a `runs` field or any field typed as runs, and every element of `OiPart.reports` produced by a real refresh is an `OiReport`; the worker itself is a closure and is not reachable).
  - `test_the_largest_feasible_body_passes_through`: the page window has 8 640 slots, so at most 8 640 runs. Rows at `S'` (also a conflict record, so `O` is `conflicted`) and at `E` (so `N` is the last slot, present), conflict records on every even slot and no other row: about 8 638 alternating runs of `conflicted` and one other state; the body is over 500 000 bytes, validates under the 2 000 000-byte cap through a real refresh and judges to a report; a separate case: a body of 2 000 001 bytes is `too_large`.
  - `test_other_failures_of_the_open_interest_call_touch_only_that_part`: a transport failure and a timeout of the open interest call (`unknown`, detail `transport:…`); a `404 open_interest_disabled` that contradicts the health answer (`unknown`, `invalid` with detail `api_error:open_interest_disabled`); a `404 unknown_series` for one symbol and a `500` for another in the same refresh: `no_data` and `unknown` respectively; candle fields identical in all of them.
- [ ] **Step 2: Run** `uv run pytest tests/tools/symbol_overview/test_oi_refresh.py -q`; expected FAIL.
- [ ] **Step 3: Implement** in `Overviewer._refresh`: after the health step compute `oi_health = open_interest_health(...)` from the **same** health response bytes (keep them from the first call; no second call), derive `oi_window = open_interest_window(window)`; leave the candle `one()` and its `ThreadPoolExecutor` block unchanged; then, only when `oi_health` is an `OiHealth`, a second `ThreadPoolExecutor(max_workers=self._concurrency)` runs `one_oi(instrument) -> OiReport` for every instrument: unsupported series → `Invalid("schema","unsupported_series")`; deadline check **before the call** with `oi_deadline_hit`; `answer = self._call(open_interest_path(...))`; validation, then `judge_open_interest` inside the worker. `candle_not_asked` and `oi_not_asked` count `Invalid("deadline")` outcomes. Build `OiPart` and pass it to `build_overview`. Disabled and unknown parts build their `OiPart` without calls.
- [ ] **Step 4: Run** the new file, `uv run pytest tests/tools tests/api/test_overview_bench_model.py tests/api/test_overview_bench_smoke.py -q` (existing `test_refresh.py` unedited; the benchmark imports tool functions), `make lint typecheck`. Expected PASS.
- [ ] **Step 5: Commit:** `feat: OI-6b two-phase overview refresh`.

### Task 5: The page

**Files:** Modify `tools/symbol_overview/page.html`; create `tests/tools/symbol_overview/test_oi_page.py`.

**Interfaces:** Consumes the overview object of Task 3.

- [ ] **Step 1: Failing tests** (`test_oi_page.py`). pytest does not run JavaScript, so the page is verified in two ways: (a) string and markup checks, as `test_server.py` does for element ids; (b) **the page's pure decision functions run under Node.js** when `node` is on the path (it is here, v16; the test is `pytest.skip`ped otherwise and the final report says whether it ran): the test extracts the `<script>` text up to the marker comment `// --- DOM below ---`, runs it with `node -e` in a `vm` context and compares the JSON result. The pure functions, defined above the marker: `worstState(a, b)`, `isProblem(state)`, `problemSymbols(symbols)` (the filter and the order of design 8.8: candle **or** open interest is a problem; order by the worse state, then the candle state, then symbol), `okSymbols(symbols)` (both `ok`; a symbol with open interest `null` counts as `ok` for that part only when the status is `disabled` or the block is absent, never when it is `unknown`), `reasonLines(symbol)` (reasons prefixed "candles: " / "open interest: "), `okGroupLabel(status)`. Node tests: `test_problem_symbols_filter_and_order` (a candle-only problem, an open-interest-only problem, both, neither; a tie broken by candle state then symbol), `test_the_ok_group_holds_only_symbols_where_both_are_ok`, `test_the_reasons_are_prefixed_by_their_source`, `test_the_unrecoverable_note_is_shown_for_an_ok_symbol`, `test_disabled_and_missing_blocks_never_look_green` (an `unknown` status gives no `ok` symbol for open interest; the label of the group is "(candles and open interest)" only when the status is `enabled`, "(candles)" for `disabled` and for a missing block). String checks: `test_the_page_keeps_every_existing_element_id`; `test_the_page_has_the_new_elements` (`oi-summary`, `oi-notice`, `refresh-notice`); `test_clear_states_clears_the_new_elements` (`clearStates` lists `oi-summary`, `oi-notice` and `refresh-notice`, so a later page-level error leaves no old green open interest tile); `test_the_legend_has_the_new_character_r_and_open_interest_wording` (`r`, "no longer recoverable", "before the first stored row", "first stored row"); `test_every_reason_code_of_open_interest_has_text` (`no_data`, `invalid`, `stale`, `conflict`, `missing`, `due`, `unrecoverable` in `TEXT.oiReasons`); `test_the_page_text_is_english`; `test_the_page_computes_no_state_and_reads_no_clock_for_states` (no `Date.now()` use except the age line, as today: the string `Date.now()` appears only in the lines of `updateAge` and `refresh`); `test_the_page_does_not_call_the_api_directly` (`fetch(` only for `/overview.json`).
- [ ] **Step 2: Run** `uv run pytest tests/tools/symbol_overview/test_oi_page.py -q`; expected FAIL.
- [ ] **Step 3: Implement** in `page.html` (design 8.8; layout is the builder's):
  - Summary: the candle tiles as today (labelled "Candles"); below them `#oi-summary` with the labelled row "Open interest" and the tiles `total` and the seven states; disabled: one line "Open interest is not enabled in this deployment"; status `unknown` or a missing `open_interest` block: the visible lines "Open interest could not be judged for any symbol" and "Open interest is not reported by the tool" respectively (never green tiles).
  - Problem list: a symbol is listed when its candle state **or** its open interest state is a problem state (`ORDER` index before `ok`); columns: candle state, open interest state, symbol, reasons, each reason prefixed "candles:" or "open interest:" (the `unrecoverable` information line is shown for any symbol that has it, listed or in the ok group); order by the worse of the two states (`ORDER` index), then the candle state, then symbol; the "ok" group holds symbols where **both** are `ok`, labelled "(candles and open interest)".
  - Heatmap: two labelled strips per symbol on the same horizontal scale, candles above open interest, each with its own "history since" date (the open interest one labelled "first stored row"); tooltips name the hour and the state in words (the open interest strip uses the same `window.start + i·bucketMs`, because bucket `k` is the same hour); `null` strip → the hatch; the legend gains `r` ("no longer recoverable") and states that `_` of the open interest strip reads "before the first stored row".
  - `#refresh-notice`: one line each for `refresh.deadline_hit` (candles) and `refresh.oi_deadline_hit` (open interest) with the counts `candle_not_asked` / `oi_not_asked` (the open interest keys are absent in an older tool: show the candle line only). `#oi-notice`: the one sentence for `disabled`, the warning for `unknown` ("Open interest could not be judged for any symbol"; every symbol is then listed as a problem, and the page says so), and "Open interest is not reported by the tool" when the block is absent.
  - The "ok" group lists symbol names as today, each followed by its `unrecoverable` note when it has one.
  - The pure functions above the `// --- DOM below ---` marker carry all decisions; the code below the marker only builds elements.
  - `COLORS` gains `r`; colors never carry meaning alone (text tags next to every state).
- [ ] **Step 4: Run** the new file and `uv run pytest tests/tools -q`; `make lint typecheck`. Expected PASS.
- [ ] **Step 5: Commit:** `feat: OI-6b the page shows open interest`.

### Task 6: Real API test, manual check, documentation

**Files:** create `tests/tools/symbol_overview/test_oi_against_real_api.py`; modify `README.md`, the design (status and change history), `docs/adr/0003-open-interest.md` (P-16 row), `docs/invariants.md` only if the tool already has rows (grep first; add rows with existing tests only).

- [ ] **Step 1: Integration test** `test_the_tool_against_the_real_api_with_open_interest`, written **after** Tasks 1 to 5 (it passes on its first run; it is a check, not a red-green step), modelled on `test_against_real_api.py` (read it first; same pattern for starting the API app on a real socket and lifting the guard). Five symbols, seeded with the arithmetic that fixes their states at `as_of` (`A`, the clock the test passes to `create_app`): *full* rows from before `S'` up to the newest expected slot `T_e` (state `ok`, strip all `.`); *hole* the same with one recent run of missing rows (`holes`, `m`); *unrecoverable* the same with a hole older than `A - H` (`ok` with reason `unrecoverable`, strip has `r`); *young* rows only from three days before `A` up to `T_e` (`ok`, the strip starts with `_`); *none* no open interest row (`no_data`). The candle fields of every symbol equal those of the same run against an app created with `open_interest=None` (`disabled`, zero open interest calls).
- [ ] **Step 2: Manual check** (documented in the README, performed once with output recorded in the ledger): start the real API on a generated database (`tests/api/bench_server.py` machinery or `quasar-api serve` with a test config), run `python -m tools.symbol_overview --api-url … ` and open the page with the browser tool if available; look at the five symbol classes and a disabled deployment. If no browser is available, say so; **do not claim the page was seen**. The decision logic of the page is covered by the Node tests of Task 5; layout, colors and tooltips are covered by nothing automatic, and the final report says exactly that.
- [ ] **Step 3: README** (the tool's section): what the second strip and row mean (`_` = before the first stored row, `r` = no longer recoverable, the "first stored row" is not "collection start"), that open interest `ok` does not say the history is long enough, the new refresh keys, the unchanged candle behavior, the limits (loopback measurement only; the tunnel prediction AC7 is open until `L_tunnel` is supplied), and the manual check recipe.
- [ ] **Step 4: Design and ADR:** design status line (OI-6b implemented on the same branch as OI-6a: the user decided not to split; G1 numbers in section 14; G2 still to be measured with the real refresh), section 13 change-history row; ADR-0003 P-16 row OI-6a/OI-6b both implemented.
- [ ] **Step 5: Measure G2:** run the tool's real refresh against the generated 300 and 600 symbol databases and record wall time, memory of the tool (AC8 ≤ 256 MiB, `resource.getrusage` or `/usr/bin/time -l`) and the page body size in design section 14 "G2". A script in the scratchpad is enough (nothing new in the repo); if it shows a failure of AC1 or AC8, stop and report.
- [ ] **Step 6: `make check`** with the real output; test counts before and after; `uv run pytest tests/api/test_overview_bench_model.py tests/api/test_overview_bench_smoke.py -q` shown separately.
- [ ] **Step 7: Commit:** `docs: OI-6b README, design status and G2 measurements`.
