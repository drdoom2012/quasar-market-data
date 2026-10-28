# S18 `GET /v1/indicators` (RSI) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `GET /v1/indicators` serves RSI (and, by the same request and response shape, the S19 indicators later) for one series and a window of at most 2016 slots, with a value or a reason per point and a strict `latest`.

**Architecture:** Pure domain code parses and validates the `indicators` parameter, defines each indicator (parameters, outputs, `warmup_slots`), splits the read range into segments and decides every point; it receives the computation as an injected function. One API module is the only importer of TA-Lib and numpy and supplies that function. An API reader reads the closes and slot facts of one read range in one read transaction; the handler resolves the window, evaluates each indicator on its own read range and renders the body.

**Tech Stack:** Python 3.12, FastAPI, stdlib `sqlite3`, TA-Lib (Python wrapper and C library), numpy; pytest, mypy strict, ruff, import-linter.

**Spec:** `docs/superpowers/specs/2026-10-28-s18-indicators-design.md` (all of it; section numbers below are its sections). Also `docs/contracts/api-v1.md` (sections 1–4, 5.6, 6, 6.1.2, 6.1.5), `docs/invariants.md` (INV-014, INV-044).

## Facts checked (2026-10-28, read in the code)

- `api/params.py::_decode` uses `parse_qsl` with the default `&` separator: `;` is not a separator and a pair splits at its first `=`, so `indicators=rsi:period=14;rsi:period=7` reaches the handler whole. `_parse` already rejects unknown, repeated and empty parameters; `_check_window` has the 10 000-slot maximum hard-coded.
- `domain/span_coverage.py::span_slot_states` yields the clock-free states per slot; `domain/timed_coverage.py::classify_span_at` adds the clock split but only returns counts and runs. No function yields the timed state per slot yet.
- `domain/schedule.py::newest_expected_open_ms(now_ms, timeframe, publication_delay_ms)` returns the newest slot with `expected_at <= now_ms`, or `None`.
- `domain/timestamps.py`: `MIN_TIMESTAMP_MS = 946_684_800_000` (`T_min`), divisible by 300 000.
- `tests/test_domain_purity.py` allows only `__future__`, `collections`, `dataclasses`, `decimal`, `enum`, `re`, `typing` in `domain`; float finiteness checks therefore live in the API (no `math` in domain).
- `domain is pure` already forbids `talib`; there is no contract yet that keeps `talib` or `numpy` inside one API module. mypy is strict; `ccxt` has an `ignore_missing_imports` override.
- `tests/api/seed.py::insert_candle` writes deterministic closes (`80000 + n % 5000`), which rise almost monotonically and would make RSI 100; varied closes need a seed helper.
- TA-Lib and numpy are not installed. Next invariant ID: INV-058.

## Global Constraints

- Window at most **2016 slots**, for an explicit and a defaulted `end`; a defaulted `end` is never capped (spec 5.1, 5.5).
- `indicators`: 1..10 items, `;` between items, `name:key=value,…`, every parameter required, integer values `^[0-9]{1,16}$` (spec 5.1).
- RSI: `period` 2..100, `warmup_slots = 10 × period`, one output `rsi` (spec 5.1, 5.4).
- Read range per indicator `R_i = [max(start − W_i × timeframe, T_min), end)`; the database is read once for the union (spec 5.3).
- A non-present slot ends a segment; the computation is called once per segment; a point needs `k ≥ W_i` (spec 5.3).
- Reasons, first match: `conflicted`, `unavailable`, `missing`; `not_yet_expected`, `due`, `stale`; `warmup` (spec 5.2).
- Values are float64 serialized as the shortest round-trip decimal, positional, no trailing zero, `-0.0` as `0`; NaN or infinity at an emitted point is a `500` (spec 5.6).
- TA-Lib global state (`set_unstable_period`, compatibility) is never changed (spec 5.6).
- Order of checks: parameters `400`, clock `500`, defaulted-window `400`, readiness `503`, series `404` (contract section 2 and 6.1.5).
- One read transaction per request, read-only connection, SQLite off the event loop (contract 5.6, INV-014).
- No schema change, no new error code, no change to an accepted ADR.

## Review Focus

1. **Unusual `indicators` text:** a trailing `;`, an empty item (`;;`), `RSI` in capitals, a space, `period=014` next to `period=14` → `400` for the first four; the last pair is the same normalized id `rsi:period=14` and is a duplicate `400`. (Task 1.)
2. **Conflict or unavailable records after the newest stored candle** inside the window → those points are `conflicted` / `unavailable`, never `due` or `stale` (stored facts win over the clock). (Task 2.)
3. **Host clock behind the stored data** → the defaulted `end` ends before the newest stored candle; `latest` is a present historical point, the answer is `200`, no error. (Task 5.)
4. **A window entirely before the oldest stored candle** → `200`, every point `missing`, `anchor_open_time_ms: null`, not `404`. (Task 5.)
5. **The largest request:** 2016 slots, ten RSI(100) variants (`period` 91..100) → the read range is 3016 slots, the response lists 2016 points per indicator and completes. (Task 5.)

## File structure

| File | Responsibility |
|---|---|
| `src/quasar_market_data/domain/indicators.py` (new) | Indicator definitions, `IndicatorSpec`, the `indicators` grammar. |
| `src/quasar_market_data/domain/timed_coverage.py` (modify) | Add `timed_slot_states`; `classify_span_at` uses it. |
| `src/quasar_market_data/domain/indicator_series.py` (new) | Read range, segments, warm-up, anchor, points. |
| `src/quasar_market_data/api/indicator_engine.py` (new) | The only importer of `talib` and `numpy`: the compute function per spec. |
| `src/quasar_market_data/api/float_text.py` (new) | float64 → contract text. |
| `src/quasar_market_data/api/params.py` (modify) | `IndicatorsRequest`, `parse_indicators_request`, `indicators_window_end`. |
| `src/quasar_market_data/api/indicator_reader.py` (new) | The read transaction for one request. |
| `src/quasar_market_data/api/indicator_results.py` (new) | Per spec: states, closes, `evaluate`. |
| `src/quasar_market_data/api/database.py`, `api/app.py` (modify) | Glue and the handler and body. |
| `tests/indicator_data.py` (new) | Deterministic generated closes (canonical decimal text). |

---

### Task 0: Contract section 6.3 and the dependency check (HARD STOPs)

**Files:**
- Modify: `docs/contracts/api-v1.md` (status line, section 6 reserved row, new section 6.3, section 9 decisions)

- [ ] **Step 1: Write section 6.3** from spec 5.1–5.7: request table, `indicators` grammar and normalized id, RSI parameters, the reasons table with its order, read range with the `T_min` clamp, segments and warm-up, `latest` (the window's last point, **not the current market value**; the defaulted `end`; the `400` instead of a capped default), the response shape (the example marked as an abbreviated illustration), numbers (float64, not exact decimals; reproducible only for the same build; no whole-history promise, spec 5.4), errors and their order. Change the section 6 row for `/v1/indicators` to "Specified in section 6.3 below". Add decisions I1–I6 to section 9: I1 one `indicators` parameter, not repeated; I2 RSI `period` 2..100 and at most 10 indicators; I3 `warmup_slots = 10 × period`; I4 window 2016 slots, defaulted `end` never capped; I5 value text format; I6 `latest` is the window's last point.
- [ ] **Step 2: HARD STOP 1.** Show section 6.3 to the user. Tasks 1–5 start only after the user approves it; record the approval in the status line (date, "decisions I1–I6 as proposed" or as changed).
- [ ] **Step 3: HARD STOP 2.** Without changing `pyproject.toml` or `uv.lock`, run `uv run --with TA-Lib --with numpy python -c "import talib, numpy; print(talib.__version__, talib.__ta_version__, numpy.__version__)"`. Report the output to the user. If the import fails (no wheel with the bundled C library for this platform), stop and report what the error says and what the install would need (for example `brew install ta-lib`); Task 3 waits for the user's decision.
- [ ] **Step 4: Commit** `docs: specify GET /v1/indicators (contract section 6.3)` after the approval.

### Task 1: Indicator definitions and the `indicators` grammar

**Files:**
- Create: `src/quasar_market_data/domain/indicators.py`
- Test: `tests/domain/test_indicators.py`

**Interfaces:**
- Produces:
  - `MAX_INDICATORS: Final = 10`
  - `class IndicatorSyntaxError(ValueError)` — its message is the `400` message.
  - `@dataclass(frozen=True) class ParamRange: name: str; minimum: int; maximum: int`
  - `@dataclass(frozen=True) class IndicatorDefinition: name: str; params: tuple[ParamRange, ...]; outputs: tuple[str, ...]; warmup: Callable[[Mapping[str, int]], int]`
  - `DEFINITIONS: Final[Mapping[str, IndicatorDefinition]]` with exactly `"rsi"`: params `(ParamRange("period", 2, 100),)`, outputs `("rsi",)`, warmup `10 × period`.
  - `@dataclass(frozen=True) class IndicatorSpec: name: str; params: tuple[tuple[str, int], ...]` (definition order), with properties `id -> str` (`"rsi:period=14"`), `outputs -> tuple[str, ...]`, `warmup_slots -> int`, and `param(name: str) -> int`.
  - `parse_indicators(text: str) -> tuple[IndicatorSpec, ...]` (request order).

- [ ] **Step 1: Write the failing tests**

```python
def test_parses_two_rsi_specs_in_request_order():
    specs = parse_indicators("rsi:period=14;rsi:period=7")
    assert [s.id for s in specs] == ["rsi:period=14", "rsi:period=7"]
    assert specs[0].warmup_slots == 140 and specs[0].outputs == ("rsi",) and specs[0].param("period") == 14


def test_leading_zero_normalizes_and_duplicates_are_rejected():
    assert parse_indicators("rsi:period=014")[0].id == "rsi:period=14"
    with pytest.raises(IndicatorSyntaxError):
        parse_indicators("rsi:period=14;rsi:period=014")


@pytest.mark.parametrize(
    "text",
    [
        "",
        ";",
        "rsi:period=14;",
        ";rsi:period=14",
        "rsi:period=14;;rsi:period=7",
        "RSI:period=14",
        "rsi",
        "rsi:",
        "rsi:period",
        "rsi:period=",
        "rsi:period=1",
        "rsi:period=101",
        "rsi:period=14,period=14",
        "rsi:period=14,length=3",
        "rsi:length=14",
        "ema:period=14",
        "rsi:period=+14",
        "rsi:period=1_4",
        "rsi:period=1.0",
        "rsi:period= 14",
        "rsi :period=14",
        "rsi:period=١٤",
        "rsi:period=12345678901234567",
    ],
)
def test_rejects(text):
    with pytest.raises(IndicatorSyntaxError):
        parse_indicators(text)


def test_rejects_more_than_ten_and_accepts_ten():
    ten = ";".join(f"rsi:period={p}" for p in range(2, 12))
    assert len(parse_indicators(ten)) == 10
    with pytest.raises(IndicatorSyntaxError):
        parse_indicators(ten + ";rsi:period=12")


def test_bounds_are_inclusive():
    assert [s.warmup_slots for s in parse_indicators("rsi:period=2;rsi:period=100")] == [20, 1000]
```

- [ ] **Step 2: Run** `uv run pytest tests/domain/test_indicators.py -v` → FAIL (module missing).
- [ ] **Step 3: Implement** `domain/indicators.py`. Split on `;`, then the first `:`, then `,`, then the first `=`; integers with `re.fullmatch(r"[0-9]{1,16}", value)` and `int(value)`. Messages name the offending item.
- [ ] **Step 4: Run** the test file and `uv run pytest tests/test_domain_purity.py` → PASS.
- [ ] **Step 5: Commit** `feat(domain): indicator definitions and the indicators grammar (S18)`.

### Task 2: Per-slot timed states, segments, warm-up and points

**Files:**
- Modify: `src/quasar_market_data/domain/timed_coverage.py`
- Create: `src/quasar_market_data/domain/indicator_series.py`
- Test: `tests/domain/test_timed_coverage.py` (add), `tests/domain/test_indicator_series.py`

**Interfaces:**
- Consumes: `SlotState`, `ExpectationRule` (existing).
- Produces:
  - `timed_slot_states(*, start, end, timeframe_ms, candles, conflicts, unavailable, newest_open_time_ms, as_of_ms, rule) -> tuple[tuple[int, SlotState], ...]` in `timed_coverage.py`, same keyword arguments, validation and precedence as `classify_span_at`, which is rewritten to count over it.
  - In `indicator_series.py`:
    - `type PointReason = Literal["conflicted", "unavailable", "missing", "not_yet_expected", "due", "stale", "warmup"]`
    - `@dataclass(frozen=True) class IndicatorPoint: open_time_ms: int; values: tuple[float, ...] | None; reason: PointReason | None`
    - `@dataclass(frozen=True) class IndicatorSeries: anchor_open_time_ms: int | None; points: tuple[IndicatorPoint, ...]`
    - `type Compute = Callable[[Sequence[float]], Sequence[Sequence[float]]]` — one sequence per output, each as long as the input.
    - `read_start_ms(start: int, warmup_slots: int, timeframe_ms: int) -> int` = `max(start − warmup_slots × timeframe_ms, MIN_TIMESTAMP_MS)`.
    - `evaluate(*, start: int, end: int, timeframe_ms: int, warmup_slots: int, states: Sequence[tuple[int, SlotState]], closes: Mapping[int, float], compute: Compute, output_count: int) -> IndicatorSeries`. `states` must cover exactly `[read_start_ms(start, warmup_slots, timeframe_ms), end)` in order, `closes` must hold exactly the `present` slots; otherwise `ValueError`. A `compute` result with the wrong number of outputs or lengths is a `ValueError`.

- [ ] **Step 1: Pin the current `classify_span_at` before the refactor.** Add to `test_timed_coverage.py` regression tests that compare the **whole** `TimedCoverage` (all seven counts, `slots`, `stored_ahead_of_as_of`, and `runs` as an exact tuple of `GapRun(state, start_ms, end_ms)` in order) with hand-written expected values. `TF = 300_000`, delay `d = 15_000`, lag `L = 60_000`, `n` the newest stored open time, `x = n + 2 × TF + d` the `expected_at` of the first slot after `n`:
  - `test_regression_stored_states_and_runs` — a span of 12 slots: present, present, conflict record on a present slot, present, both a candle and an unavailable record (→ `conflicted`), unavailable only, missing, missing, present (`n`), then three slots after `n`. Expected runs: `conflicted [s+2, s+3)`, `conflicted [s+4, s+5)`, `unavailable [s+5, s+6)`, `missing [s+6, s+8)` (two slots merged into one run), then, at `as_of = x + TF + L`, `stale [s+9, s+10)`, `due [s+10, s+11)`, `not_yet_expected [s+11, s+12)`; present slots never appear in `runs`; two adjacent `conflicted` slots separated by a present one are two runs.
  - `test_regression_slots_before_the_oldest_candle_are_missing` — a span starting 3 slots before the oldest stored candle: one `missing` run of 3.
  - `test_regression_clock_boundaries` — parametrized over `as_of` for the first slot after `n`: `x − 1` → `not_yet_expected`; `x` → `due`; `x + L` → `due`; `x + L + 1` → `stale`; and with three slots after `n` at `as_of = x + TF + L` (the second slot's last `due` instant; `TF > L`, so the third is not yet expected): runs `stale [n+TF, n+2TF)`, `due [n+2TF, n+3TF)`, `not_yet_expected [n+3TF, n+4TF)` in that order.
  - `test_regression_records_after_the_newest_candle_win_over_the_clock` (Review Focus 2) — a conflict record and an unavailable record on two slots after `n`, `as_of` far later: `conflicted` and `unavailable`, never `stale`; the slot between them is `stale`.
  - `test_regression_stored_ahead_of_as_of` — `as_of` before `x`: `stored_ahead_of_as_of is True`, every slot after `n` is `not_yet_expected`, `due == stale == 0`.

  Run `uv run pytest tests/domain/test_timed_coverage.py -v` → **PASS on the unchanged code** (these pin today's semantics; if one fails, the expected value is wrong, not the code: fix the test, never `classify_span_at`).

- [ ] **Step 2: Write the failing tests.** In `test_timed_coverage.py`: `test_timed_slot_states_match_the_regression_cases` — for every case of Step 1, `timed_slot_states` returns one `(open_time, state)` per slot in ascending order, and the runs and counts rebuilt from it equal Step 1's expected values.

  In `test_indicator_series.py`, with `TF = 300_000`, `S` an aligned start far above `T_min`, `W = 140`, and a recording fake compute that returns the running sum of its input (so "equal to a direct computation over X" is exact and checkable):

```python
class Recorder:
    def __init__(self):
        self.calls: list[list[float]] = []

    def __call__(self, closes):
        self.calls.append(list(closes))
        return (list(itertools.accumulate(closes)),)
```

  One test per spec 5.3 scenario, named `test_a1_full_history_anchors_w_before_start` … `test_a12_read_range_clamps_at_t_min`, asserting exactly the "Expected" column: anchor, which open times are `warmup` / `missing` / `conflicted`, the first open time with a value, and the value equal to the running sum computed directly over the stated range. A2 asserts `evaluate` results byte-equal to A1's (the extra missing slot is before `read_start_ms`, so the caller does not even pass it: assert `read_start_ms(S, 140, TF) == S - 140 * TF`). A8 builds segments with exactly `W` and `W − 1` present slots before a point. A9 uses a fake compute returning `nan` everywhere and a segment of 14 slots: no point has `values`. A11 asserts for A3–A6 and A12 that every recorded call's input is exactly the closes of one segment, in order (no non-present slot, nothing before the read start). A12: `start = T_min + 50 × TF` → `read_start_ms == T_min`, first value at `start + 90 × TF`; `start = T_min` → first value at `start + 140 × TF`. Plus `test_reasons_follow_slot_states` (each `SlotState` other than `PRESENT` maps to the reason of the same name) and `test_inconsistent_inputs_raise` (states not starting at the read start, a close for a non-present slot, a compute returning a wrong length).

- [ ] **Step 3: Run** `uv run pytest tests/domain/test_timed_coverage.py tests/domain/test_indicator_series.py -v` → the Step 2 tests FAIL (missing names); the Step 1 regression tests still PASS.
- [ ] **Step 4: Implement** `timed_slot_states` (move the loop body of `classify_span_at`; `classify_span_at` keeps its signature, validation and result and only counts and builds runs from it) and `indicator_series.py`. `evaluate` walks `states` once, collects maximal runs of `PRESENT`, calls `compute` once per run with its closes, and emits points only for open times in `[start, end)`: `values` from index `k ≥ warmup_slots` of the run, `warmup` below it, the slot state's value for non-present slots. `anchor_open_time_ms` is the first open time of the run holding the first emitted value, else `None`.
- [ ] **Step 5: Run** `uv run pytest tests/domain tests/api/test_coverage_reader.py tests/api/test_coverage_endpoint.py -q` → PASS: the Step 1 regression tests and every existing timed-coverage and coverage test pass unchanged after the refactor.
- [ ] **Step 6: Commit** `feat(domain): segments, warm-up and indicator points (S18)`.

### Task 3: TA-Lib engine and value text

Starts only after HARD STOP 2 (Task 0 Step 3) is resolved.

**Files:**
- Modify: `pyproject.toml` (dependencies, mypy override, import-linter contract), `uv.lock`
- Create: `src/quasar_market_data/api/indicator_engine.py`, `src/quasar_market_data/api/float_text.py`, `tests/indicator_data.py`, `tests/fixtures/indicators/rsi_golden.json`
- Test: `tests/api/test_indicator_engine.py`, `tests/api/test_float_text.py`, `tests/test_import_contracts_negative.py` (add cases)

**Interfaces:**
- Consumes: `IndicatorSpec` (Task 1), `Compute` (Task 2).
- Produces:
  - `compute_function(spec: IndicatorSpec) -> Compute` in `indicator_engine.py`. RSI: `talib.RSI(numpy.asarray(closes, dtype=numpy.float64), timeperiod=spec.param("period"))`, returned as `(result.tolist(),)`.
  - `float_text(value: float) -> str` in `float_text.py`; `ValueError` for NaN and infinity.
  - `generated_closes(count: int, seed: int) -> list[str]` in `tests/indicator_data.py`: a `random.Random(seed)` walk, canonical decimal text (contract section 1 decimals).

- [ ] **Step 1: Add the dependencies.** `uv add TA-Lib numpy` (exact versions land in `uv.lock`). In `pyproject.toml`: a mypy override `module = ["talib", "talib.*"]`, `ignore_missing_imports = true` (only if TA-Lib ships no `py.typed`), and an import-linter contract `talib and numpy are imported only by the indicator engine` (`type = "forbidden"`, sources `quasar_market_data.api`, `quasar_market_data.collector`, `quasar_market_data.storage`, forbidden `talib`, `numpy`, `ignore_imports = ["quasar_market_data.api.indicator_engine -> talib", "quasar_market_data.api.indicator_engine -> numpy"]`). Add negative cases to `CASES`: `api_importing_talib_fails_the_engine_only_contract` (`api/_probe.py`, `import talib`), `api_importing_numpy_fails_the_engine_only_contract`, `collector_importing_talib_fails_the_engine_only_contract`, `domain_importing_talib_fails_domain_is_pure`, and `api_importing_the_engine_is_allowed` (`import {PACKAGE}.api.indicator_engine`). Add the new contract name to `CONTRACT_NAMES`.
- [ ] **Step 2: Write the failing tests.**
  - `test_float_text.py` (R2): `test_r2_float_text_round_trips_without_exponent`, parametrized over `0.0 → "0"`, `-0.0 → "0"`, `100.0 → "100"`, `1e-07 → "0.0000001"`, `123456789.125 → "123456789.125"`, `0.1 → "0.1"`, `-2.5 → "-2.5"`; for these and `5e-324`, `1/3`, `2/3 * 100`: no `e`/`E`, no trailing fractional `0`, no trailing `.`, and `float(text) == value`; `test_r2_non_finite_raises`: `nan`, `inf`, `-inf` raise `ValueError`.
  - `test_indicator_engine.py`:
    - `test_r4_matches_a_pure_python_wilder_rsi` — an independent reference written in the test, plain Python floats, no numpy:
      - changes `c[j] − c[j−1]`; gain `max(change, 0.0)`, loss `max(−change, 0.0)`;
      - seed at index `p`: `avg_gain = sum(gains[1..p]) / p`, `avg_loss = sum(losses[1..p]) / p`;
      - after it: `avg = (avg × (p − 1) + x) / p` for gain and loss;
      - value `100 × avg_gain / (avg_gain + avg_loss)`, the same algebra as `100 − 100 / (1 + RS)` but defined when `avg_loss == 0`;
      - **explicit zero cases:** `avg_loss == 0` and `avg_gain > 0` → `100.0`; `avg_gain == 0` and `avg_loss > 0` → `0.0`; both zero (constant prices) → the value TA-Lib defines for that case, which Step 4 reads from the TA-Lib source of the locked version (`ta_RSI.c`) and writes into the reference with a comment citing it. The reference never divides by zero.

      Parametrized over periods 2, 14, 100 and these inputs: `generated_closes(600, seed=1)`; strictly rising closes (zero losses); strictly falling closes (zero gains); 300 constant closes (both zero); 200 moving closes followed by 300 constant ones (averages decay towards zero but stay positive); strictly alternating up and down. Assert at every index `≥ period` that both are finite and `abs(engine − reference) < 1e-9`, and in the zero cases exact equality (`100.0`, `0.0`, the constant-case value).
    - `test_r4_tolerance_is_measured` — the same inputs; computes the maximum absolute difference and asserts it is `≤ 1e-11` (two orders of magnitude under the tolerance). **Before fixing the tolerance, Step 5 runs this and records the measured maximum in the test's docstring.** Expected size: the smoothing is a contraction, so rounding does not accumulate and differences should be a few ULPs of values below 100 (about `1e-13`). If the measured maximum is above `1e-11`, the first suspect is an algorithm mismatch (seed, operation order, the zero cases), not the tolerance: compare index by index and fix the reference. Only a difference explained by operation order may raise the tolerance, and then to the measured maximum times 100, with the measurement and the explanation in the docstring.
    - `test_r3_golden_strings` — RSI(14) and RSI(2) on `generated_closes(400, seed=7)`, indices `≥ 10 × period`, `float_text` of each value equals `tests/fixtures/indicators/rsi_golden.json` (generate the file once with the engine and commit it; the test docstring says a TA-Lib or numpy upgrade that changes a digit fails here on purpose).
    - `test_r5_constant_closes` — 300 equal closes, RSI(14): every value from index 140 equals the constant-case value of R4 exactly, and `float_text` of it is pinned as a string. If TA-Lib defines it as `0` (a flat market reading as maximally oversold), report it to the user with the test result; it is visible in the contract and not changed here.
    - `test_short_input_gives_no_finite_value_before_period` — 10 closes, RSI(14): every output is NaN, no exception (spec A9 at the engine level).
    - `test_c1_warmup_approximation` — `generated_closes(5000, seed=3)`, periods 2, 14, 100: RSI over the last `10 × period + 1` closes versus RSI over all 5000 at the last index differs by less than `1e-2` (the docstring says it is an observation, not a guarantee; record the measured maximum in the docstring).
    - `test_never_changes_talib_global_state` — an AST scan of `indicator_engine.py` finds no `set_unstable_period` or `set_compatibility`.
- [ ] **Step 3: Run** `uv run pytest tests/api/test_float_text.py tests/api/test_indicator_engine.py tests/test_import_contracts_negative.py -v` → FAIL.
- [ ] **Step 4: Implement** `float_text` (`Decimal(repr(value))` formatted with `"f"`, then trimmed; `math.isfinite` first) and `indicator_engine.py`; read the constant-case rule in the locked TA-Lib's `ta_RSI.c` and put it into the R4 reference.
- [ ] **Step 5: Run** the three test files and `make imports typecheck` → PASS. Record the measured maximum of `test_r4_tolerance_is_measured` in its docstring and in the commit message.
- [ ] **Step 6: Commit** `feat(api): TA-Lib indicator engine and value text (S18)`.

### Task 4: Request parsing, window end and the reader

**Files:**
- Modify: `src/quasar_market_data/api/params.py`, `src/quasar_market_data/api/database.py`, `tests/api/seed.py`
- Create: `src/quasar_market_data/api/indicator_reader.py`
- Test: `tests/api/test_params.py` (add), `tests/api/test_indicator_reader.py`

**Interfaces:**
- Consumes: `parse_indicators`, `IndicatorSyntaxError`, `IndicatorSpec` (Task 1); `read_start_ms` (Task 2).
- Produces:
  - `MAX_INDICATOR_WINDOW_SLOTS: Final = 2016`
  - `@dataclass(frozen=True) class IndicatorsRequest: venue: str; symbol: str; timeframe: Timeframe; start: int; end: int | None; indicators: tuple[IndicatorSpec, ...]`
  - `parse_indicators_request(raw_query: bytes) -> IndicatorsRequest` — names `venue, symbol, timeframe, start, end, indicators`, required all but `end`; an `IndicatorSyntaxError` becomes `invalid_parameter(str(error), parameter="indicators", received=value)`; an explicit `end` is checked by `_check_window` with a maximum argument (default 10 000, here 2016).
  - `indicators_window_end(request: IndicatorsRequest, *, as_of_ms: int, publication_delay_ms: int) -> int` — explicit `end` unchanged; otherwise `newest_expected_open_ms(...) + tf`, or `start + tf` when that is `None` or `<= start`; more than 2016 slots → `invalid_parameter("the window up to the newest expected slot exceeds 2016 slots; pass an explicit end", parameter="start", received=str(start), maximum=2016)`.
  - `@dataclass(frozen=True) class IndicatorInputs: venue: str; symbol: str; timeframe: str; start: int; end: int; read_start: int; oldest_open_time_ms: int; newest_open_time_ms: int; closes: Mapping[int, str]; conflicts: frozenset[int]; unavailable: frozenset[int]`
  - `read_indicator_inputs(conn: sqlite3.Connection, request: IndicatorsRequest, *, end: int, read_start: int) -> IndicatorInputs` — `BEGIN`, `verify_schema`, the two-seek bounds, `UnknownSeries`, then `open_time_ms, close` of `candles` and the open times of `candle_conflicts` and `unavailable_candles` in `[read_start, end)`, `ROLLBACK` in `finally` (the `coverage_reader` pattern).
  - `read_indicators(db_path: Path, request: IndicatorsRequest, *, end: int, read_start: int, open_connection: ConnectionFactory = open_reader) -> IndicatorInputs` in `database.py`; `_with_reader`'s request type gains `IndicatorsRequest`.
  - `seed.insert_candle(..., close: str | None = None)`: when given, it replaces the generated close (and `open`/`high`/`low` are set so the row validates: `high = max`, `low = min` of open and close).

- [ ] **Step 1: Write the failing tests.**
  - `test_params.py`: a valid request parses; `indicators` missing, empty, repeated → `400` with `parameter`; each Task 1 rejection surfaces as `400 parameter="indicators"`; the percent-encoded form `indicators=rsi%3Aperiod%3D14%3Brsi%3Aperiod%3D7` parses to the same ids as the plain form; explicit window of 2016 slots accepted, 2017 → `400 maximum=2016`; `indicators_window_end`: default with a current clock (newest expected + tf), default `<= start` → `start + tf` (L8), default beyond 2016 → `400 parameter="start" maximum=2016` (L4), clock before the first expected slot → `start + tf`.
  - `test_indicator_reader.py`: `test_reads_exactly_the_read_range` (rows at `read_start − TF` and at `end` exist and are not returned; a `set_trace_callback` shows every candle, conflict and unavailable statement bound to `[read_start, end)`); `test_one_transaction` (trace shows `BEGIN` first and `ROLLBACK` last); `test_unknown_series_raises`; `test_closes_are_the_stored_text`.
- [ ] **Step 2: Run** `uv run pytest tests/api/test_params.py tests/api/test_indicator_reader.py -v` → FAIL.
- [ ] **Step 3: Implement** the interfaces above.
- [ ] **Step 4: Run** `uv run pytest tests/api -q` → PASS (existing params tests unchanged by the `_check_window` maximum argument).
- [ ] **Step 5: Commit** `feat(api): parse indicator requests and read their inputs (S18)`.

### Task 5: The endpoint, end-to-end tests and documentation

**Files:**
- Create: `src/quasar_market_data/api/indicator_results.py`
- Create: `tests/api/bench_indicators.py`, `tests/api/test_indicators_bench_smoke.py`
- Modify: `src/quasar_market_data/api/app.py`, `Makefile` (target `bench-indicators`), `docs/invariants.md`, `README.md`, `docs/contracts/api-v1.md` (status line only), `docs/follow-ups.md` (only if something is left open)
- Test: `tests/api/test_indicators_endpoint.py`

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces:
  - `indicator_results(inputs: IndicatorInputs, specs: Sequence[IndicatorSpec], *, as_of_ms: int, rule: ExpectationRule) -> tuple[IndicatorSeries, ...]` — per spec: its `read_start_ms`, `timed_slot_states` over `[that, inputs.end)` with the facts filtered to that range, `closes` as `float(text)` of the present slots, `evaluate(..., compute=compute_function(spec), output_count=len(spec.outputs))`.
  - Handler `GET /v1/indicators` in `app.py`: `parse_indicators_request` → `ensure_timestamp_ms(now_ms(), "as_of_ms")` → `indicators_window_end` → `read_start = min(read_start_ms(...))` over the specs → `run_in_threadpool(read_indicators, ...)` → `run_in_threadpool(indicator_results, ...)` → `_json(200, _indicators_body(...))`.
  - `_indicators_body` with keys in spec 5.7 order: `venue, symbol, timeframe, start, end, as_of_ms, rule, series, indicators`; each indicator `id, name, params, outputs, warmup_slots, anchor_open_time_ms, latest, points`; each point `open_time_ms, values, reason`; `values` maps output names to `float_text(v)`. A `ValueError` from `float_text` is not caught (it ends as `500`).

- [ ] **Step 1: Write the failing tests** in `test_indicators_endpoint.py`, against `create_app(db, now_ms=…)` with a seeded database (`insert_candle(..., close=…)` from `generated_closes`):
  - L1–L8 of spec 5.5 as `test_l1_…` … `test_l8_…`, asserting the "Expected" column (L4: `400`, `details.maximum == 2016`; L5: an explicit past `end` with a clock a month later gives a value and `reason: null` on `latest`).
  - `test_a10_indicators_do_not_influence_each_other` — RSI(14) and RSI(100) together, history from `start − 2000` slots: each indicator's `points` byte-equal (JSON-serialized) to the same request with that indicator alone.
  - `test_r1_same_request_same_bytes` — two calls, fixed clock: identical response bodies.
  - `test_c2_moving_start_changes_values_only_within_tolerance` — a long present history; `start` and `start + TF`: overlapping values differ by less than `1e-2`; each request repeated is byte-identical.
  - `test_values_equal_the_engine_on_the_segment` — one window with a gap: emitted values equal `float_text` of `compute_function(spec)` called directly on the segment's closes.
  - Review Focus 3, 4, 5: `test_clock_behind_stored_data_ends_the_window_before_the_newest_candle`, `test_window_before_the_oldest_candle_is_all_missing`, `test_largest_request` (asserts 2016 points × 10 indicators and, by a trace callback, that the candle read starts at `start − 1000 × TF`).
  - Errors: `400` before a broken database (`parameter="indicators"`), clock out of range → `500` before the database, missing file → `503 database_missing`, unknown series → `404 unknown_series`, `HEAD` → `405`, `/v1/indicators/` → `404 not_found`, `Cache-Control: no-store`.
  - `test_a_non_finite_value_is_a_500` — monkeypatch `compute_function` to return `inf`: `500 internal_error`.
- [ ] **Step 2: Run** `uv run pytest tests/api/test_indicators_endpoint.py -v` → FAIL.
- [ ] **Step 3: Implement** `indicator_results.py`, the handler and `_indicators_body`.
- [ ] **Step 4: Benchmark of the largest request** (informative; no acceptance criterion and no HARD STOP). `tests/api/bench_indicators.py`, in the style of `tests/api/bench_health.py`: a fresh temp directory, a generated database of 50 series × 4000 present 5m candles (closes from `generated_closes`), and the largest request against one series: 2016 slots, `indicators=rsi:period=91;…;rsi:period=100` (read range 3016 slots). Options `--repeat N` (default 5) and `--series N` (default 50). For each repeat it times, with `time.perf_counter`, (a) `read_indicators` (database read), (b) `indicator_results` (TA-Lib), (c) `_indicators_body` plus `json.dumps` exactly as `_json` encodes (serialization), and (d) the whole request through `TestClient` with a fixed clock; it prints min, median and max per phase, the body size in bytes, and the environment: `platform.platform()`, `platform.machine()`, Python, `sqlite3.sqlite_version`, `talib.__version__`, `talib.__ta_version__`, `numpy.__version__`, and "page cache not dropped". Makefile target `bench-indicators: uv run python -m tests.api.bench_indicators` (not part of `make check`). `test_indicators_bench_smoke.py` runs it with `--repeat 1 --series 1` and asserts that it exits `0` and prints the four phases (a functional check, a few seconds). Run `make bench-indicators` once and record the printed environment and numbers in the README paragraph of Step 5 and in the commit message.
- [ ] **Step 5: Documentation.**
  - `docs/invariants.md`: INV-058 to INV-062, the five rows of spec section 6, status `ENFORCED`, evidence the test names from Tasks 2, 3 and 5 that are their negative tests: INV-058 `test_indicator_series.py::test_a2_…` to `test_a6_…`, `test_a11_…`, `test_a12_…`; INV-059 `test_a3_…`, `test_a6_…` to `test_a9_…`; INV-060 `test_indicators_endpoint.py::test_l2_…`, `test_l3_…`, `test_l4_…`, `test_l6_…`; INV-061 `test_a2_…`, `test_indicators_endpoint.py::test_a10_indicators_do_not_influence_each_other`, `test_r1_same_request_same_bytes`, `test_indicator_engine.py::test_r3_golden_strings`; INV-062 `test_float_text.py::test_r2_float_text_round_trips_without_exponent`, `test_r2_non_finite_raises`, `test_a9_…`, `test_indicators_endpoint.py::test_a_non_finite_value_is_a_500`. Use the full names as written; `tests/test_invariant_register.py` checks that each exists. Each row's "not covered" text includes spec 5.4 (no whole-history equality) and 5.6 (no reproducibility across builds) where relevant. Add the new import-linter contract to INV-044's evidence cell only if it names it; otherwise leave INV-044 unchanged.
  - `README.md`, section `quasar-api serve`: add `GET /v1/indicators` to the served endpoints and one paragraph: the `indicators` syntax, RSI 2..100, the 2016-slot window, `latest` is the window's last point (omit `end` for the newest expected slot), values are float64 text reproducible only for the same build; TA-Lib's C library is required (how it was installed in Task 0 Step 3); the `make bench-indicators` result of Step 4 with its environment. Add `make bench-indicators` to the README command table.
  - `docs/contracts/api-v1.md` status line: "section 6.3 implemented by S18".
- [ ] **Step 6: Run** `make check` → every step PASS. Run the full suite twice if a backfill test fails with "the simulation is stuck" (F-19), and report it if it does.
- [ ] **Step 7: Commit** `feat: GET /v1/indicators with RSI (S18)`.

---

## Residual risks

1. Values are reproducible only for the same build (TA-Lib, numpy, C library, platform); R3 makes an upgrade visible, not harmless.
2. The warm-up tolerance of C1 and C2 is measured on generated data, not proven; real series with long flat stretches may converge differently.
3. CPU cost of ten indicators over 3016 slots runs in the thread pool; there is no rate limit (contract 5.6, accepted risk). `make bench-indicators` measures one request on a warm cache; concurrency and a cold disk are not measured.
4. TA-Lib's value for constant closes (R5) may be `0`, which reads as oversold for a flat market; it is reported, not changed, in S18.
