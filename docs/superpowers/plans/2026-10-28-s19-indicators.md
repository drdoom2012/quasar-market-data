# S19 EMA, MACD, ATR, ADX and Bollinger Bands Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `GET /v1/indicators` serves `ema`, `macd`, `atr`, `adx` and `bbands` next to `rsi`, in the S18 request and response shape, with RSI responses byte-identical to S18.

**Architecture:** The domain gains five definitions (parameters, outputs, warm-up formulas, the MACD constraint) and evaluates segments of `(high, low, close)` instead of closes; the reader reads high and low with the close; the TA-Lib adapter supplies one compute function per indicator. The `/v1/indicators` body is built and encoded in the thread pool (J7). The benchmark gains a worst-case profile, concurrent requests against a served process and a `/health` probe.

**Tech Stack:** Python 3.12, FastAPI, stdlib `sqlite3`, TA-Lib 0.8.2 (C 0.8.1), numpy; pytest, mypy strict, ruff, import-linter; `httpx` (existing dev dependency) for the benchmark.

**Spec:** `docs/superpowers/specs/2026-10-28-s19-indicators-design.md` (revision 2; section numbers below are its sections). Also `docs/contracts/api-v1.md` section 6.3, `docs/invariants.md` INV-058..062, `docs/follow-ups.md` F-20, F-21, and the S18 plan `docs/superpowers/plans/2026-10-28-s18-indicators.md` for the existing tests named below.

## Facts checked (2026-10-28, read in the code)

- `domain/indicators.py`: `DEFINITIONS` maps a name to `IndicatorDefinition(name, params: tuple[ParamRange, ...], outputs, warmup)`; `_parse_item` checks keys, duplicates, the integer rule and ranges; there is no hook for a rule between parameters.
- `domain/indicator_series.py`: `Compute = Callable[[Sequence[float]], Sequence[Sequence[float]]]`; `evaluate(..., closes: Mapping[int, float], compute, output_count)` checks that `closes` holds exactly the present slots.
- `api/indicator_reader.py`: `IndicatorInputs.closes: Mapping[int, str]` from `SELECT open_time_ms, close`; `api/indicator_results.py` converts with `float(text)`; `api/indicator_engine.py::compute_function(spec)` handles `rsi` only.
- `api/app.py` handler: read and `indicator_results` run in `run_in_threadpool`; `_json(200, _indicators_body(...))` runs on the event loop (F-21).
- `tests/api/seed.py::insert_candle(close=...)` sets `high = max(open, close)`, `low = min(open, close)`: endpoint tests already have consistent OHLC. `tests/api/bench_indicators.py::build_database` writes `open = high = low = close`: ATR/ADX would be zero there, so the bench needs generated bars.
- `tests/indicator_data.py::generated_closes(count, seed)` feeds `tests/fixtures/indicators/rsi_golden.json` (`test_r3_golden_strings`); it must not change.
- `tests/test_domain_purity.py` forbids `math` in `domain`; W1 lives in `tests/domain` and may use `math.log1p`.
- `tests/test_invariant_register.py` requires every ENFORCED evidence entry to be an existing `tests/...::test_*`.
- `pyproject.toml` import-linter: `talib` and `numpy` only in `quasar_market_data.api.indicator_engine` (tests and the bench may import them).
- `tests/api/test_indicators_bench_smoke.py` asserts `read_range_slots == 3016` and the four phase names.
- **TA-Lib `MACD` seeding, observed on the locked build** (designer's probe on a seeded random walk, `(fast, slow, signal)` = `(2,3,2)`, `(12,26,9)`, `(5,35,5)`, `(99,100,50)`, `(100,200,50)`; the C source was not consulted), `k = 2/(period+1)` throughout:
  - the slow EMA is seeded with the SMA of `close[0 .. slow−1]`, first value at index `slow − 1`;
  - the fast EMA is seeded **late**, with the SMA of `close[slow−fast .. slow−1]`, first value also at index `slow − 1`, not with the SMA of `close[0 .. fast−1]`;
  - `macd = fast − slow` from index `slow − 1`; the signal EMA is seeded with the SMA of `macd[slow−1 .. slow+signal−2]`; the first emitted index of all three outputs is `slow + signal − 2` (the lookback), `histogram = macd − signal`.
  - This reference agrees with TA-Lib within `3.1e−14` absolute (float summation order; never exactly equal on every index) at every index from the lookback on. Two independently seeded EMAs (`EMA(fast) − EMA(slow)` from index 0) do **not**: they differ by `0.016` to `1.03` at the first output, so that is not the reference.
- `tests/test_api_process.py::launch` starts the venv's `quasar-api` console script directly with `subprocess.Popen` (one Python process, no `uv` wrapper), so the child's PID is the server's.

## Global Constraints

- Indicators, parameters, outputs and `warmup_slots` exactly as spec table 5.1: `rsi` `period` 2..100 → `10 × period`; `ema` `period` 2..200 → `6 × period`; `macd` `fast` 2..100, `slow` 3..200, `signal` 2..50, `fast < slow` → `6 × (slow + signal)`; `atr` `period` 2..100 → `10 × period`; `adx` `period` 2..100 → `13 × period`; `bbands` `period` 2..200, `stddev` 1..5 → `period − 1`.
- Output order: `macd` (`macd`, `signal`, `histogram`), `adx` (`adx`, `plus_di`, `minus_di`), `bbands` (`upper`, `middle`, `lower`); single-output indicators use their name.
- TA-Lib calls: `RSI`, `EMA`, `MACD` (not `MACDEXT`), `ATR(high, low, close)`, `ADX`/`PLUS_DI`/`MINUS_DI(high, low, close)`, `BBANDS(close, period, stddev, stddev, matype=0)`. TA-Lib global state never changed.
- Widest read range `2016 + 1500 = 3516` slots.
- Only `present` slots are inputs, for high, low and close alike (spec 5.3). No re-validation of OHLC.
- Unchanged from S18: window 2016, 1..10 indicators, reasons and their order, segments, `latest`, number text, error order, one read transaction, no schema change, no new dependency.
- RSI: committed golden strings and the expected bodies and assertions of existing tests do not change; helpers and call sites may be adapted (spec 5.6).
- No switch to turn J7 off; no rate limiter; no new HARD STOP besides HARD STOP 1.

## Review Focus

1. **MACD with `fast ≥ slow`** (`macd:fast=26,slow=12,signal=9`, `macd:fast=12,slow=12,signal=9`) → `400` `details.parameter` `indicators`, never a silently swapped MACD. (Task 1.)
2. **A conflicted slot whose stored high or low is extreme** (for example a stored high 10× the close) → ATR/ADX points around it are `conflicted` or `warmup`, and no compute call ever sees that high. (Task 3.)
3. **The smallest segments at extreme parameters** (`adx:period=2` on 26 present slots, `macd:fast=2,slow=3,signal=2` on 30, `bbands:period=200,stddev=5` on 199) → the first point past warm-up has finite values in every output, earlier points are `warmup`. (Task 4.)
4. **A request mixing all six names at the largest warm-ups, ten items** → read range 3516 slots, every indicator byte-identical to the same item requested alone. (Task 6.)
5. **A non-finite value in the second or third output only** (for example `histogram`) → `500 internal_error`, not a partially filled `values`. (Task 6.)

## File structure

| File | Responsibility |
|---|---|
| `src/quasar_market_data/domain/indicators.py` (modify) | Five definitions; `IndicatorDefinition.check`; the MACD rule in the grammar. |
| `src/quasar_market_data/domain/indicator_series.py` (modify) | `Prices`, `SegmentPrices`, `Compute` over `(high, low, close)`; `evaluate(prices=...)`. |
| `src/quasar_market_data/api/indicator_reader.py` (modify) | Read `high, low, close`; `IndicatorInputs.prices`. |
| `src/quasar_market_data/api/indicator_results.py` (modify) | `float(text)` per column; pass `prices`. |
| `src/quasar_market_data/api/indicator_engine.py` (modify) | Compute functions for the five indicators. |
| `src/quasar_market_data/api/app.py` (modify) | J7: body build and encoding in the thread pool. |
| `tests/indicator_data.py` (modify) | `generated_bars`, `shaped_bars`. |
| `tests/fixtures/indicators/s19_golden.json` (new) | Golden strings of the new indicators. |
| `tests/api/indicator_reference.py` (new) | Pure-Python reference implementations (R4). |
| `tests/api/test_indicator_convergence.py` (new) | C1 observations and flat-run characterization. |
| `tests/api/bench_indicators.py`, `Makefile`, `README.md` (modify) | Profiles, concurrency, `/health` probe, CPU and RSS. |
| `docs/contracts/api-v1.md`, `docs/invariants.md`, `docs/follow-ups.md` (modify) | Contract amendment, invariant text and evidence, F-20/F-21. |

---

### Task 0: Contract section 6.3 amendment (HARD STOP 1)

**Files:**
- Modify: `docs/contracts/api-v1.md` (sections 6.3 status line, 6.3.1, 6.3.3, 6.3.6, 6.3.7 if needed, 8, 9)

- [ ] **Step 1: Write the amendment** from spec 5.1, 5.2, 5.2.1, 5.3 and 5.5, nothing else:
  - Status line: "S19 adds `ema`, `macd`, `atr`, `adx`, `bbands`" with the design path; the S18 approval stays.
  - 6.3.1: one bullet per indicator with parameters, ranges, outputs in order and `warmup_slots` (Global Constraints values), the TA-Lib definition where it matters (MACD on EMAs; Bollinger SMA middle and **population** deviation, one `stddev` for both bands). Add "a constraint between parameters of one indicator (`macd`: `fast < slow`)" to the list of `400` causes.
  - 6.3.3: "at most `2016 + 1000` slots" → "at most `2016 + 1500` slots".
  - 6.3.6: inputs are the stored closes, highs and lows of present slots converted to float64; the warm-up paragraph in general terms: `warmup_slots` makes the remaining weight of the initial averages at most about `e^-9`, a weight and **not an accuracy bound**; the observation per data shape from spec 5.2.1 (ordinary data: around `0.001` points for RSI and ADX; after a 1000× volatility collapse up to `0.5` RSI points, `0.13` ADX points, `3.6 %` of ATR; a flat run after a move: the F-20 split) replaces "observed below 0.01 RSI points"; Bollinger Bands depend only on the last `period` closes but their last digits can depend on the segment start; the flat-market sentence names RSI, ADX and both DI.
  - Section 8: one traceability row "S19 indicators" → the S19 design, decisions J1–J7.
  - Section 9: table "S19 decisions" J1–J7 copied from spec section 6, marked "proposed".
- [ ] **Step 2: HARD STOP 1.** Show the diff to the user. Tasks 1–7 start only after approval; then set the status line and the J table to "approved on <date>, as proposed" (or as changed; a changed J value sends the design and this plan back for revision first).
- [ ] **Step 3: Commit** `docs: specify the S19 indicators (contract section 6.3)`.

### Task 1: Definitions, the MACD constraint and W1

**Files:**
- Modify: `src/quasar_market_data/domain/indicators.py`
- Test: `tests/domain/test_indicators.py`, `tests/domain/test_indicator_warmup.py` (new)

**Interfaces:**
- Produces: `DEFINITIONS` with keys `rsi, ema, macd, atr, adx, bbands`; `IndicatorDefinition.check: Callable[[Mapping[str, int]], str | None]` (default returns `None`; a message makes `_parse_item` raise `IndicatorSyntaxError`); `IndicatorSpec` unchanged.

- [ ] **Step 1: Write the failing tests** in `tests/domain/test_indicators.py`:

```python
@pytest.mark.parametrize(("text", "spec_id", "warmup", "outputs"), [
    ("ema:period=0200", "ema:period=200", 1200, ("ema",)),
    ("macd:signal=9,slow=26,fast=12", "macd:fast=12,slow=26,signal=9", 210, ("macd", "signal", "histogram")),
    ("atr:period=14", "atr:period=14", 140, ("atr",)),
    ("adx:period=14", "adx:period=14", 182, ("adx", "plus_di", "minus_di")),
    ("bbands:stddev=2,period=20", "bbands:period=20,stddev=2", 19, ("upper", "middle", "lower")),
])
def test_new_indicators_normalize(text, spec_id, warmup, outputs): ...

def test_bounds_are_inclusive_for_every_key():
    # each key at its minimum and maximum parses; one below / above is IndicatorSyntaxError
    # largest warm-up: macd:fast=100,slow=200,signal=50 -> 1500; adx:period=100 -> 1300; ema:period=200 -> 1200

@pytest.mark.parametrize("text", [
    "macd:fast=26,slow=12,signal=9", "macd:fast=12,slow=12,signal=9", "macd:fast=12,slow=26",
    "bbands:period=20", "bbands:period=20,stddev=0", "bbands:period=20,stddev=6", "ema:period=1",
    "ema:period=201", "adx:period=101", "atr:length=14", "bbands:period=20,stddev=2.5", "EMA:period=14",
])
def test_rejects_new(text): ...

def test_mixed_ten_items_with_three_output_indicators_parse_in_order(): ...

def test_every_allowed_warmup_is_at_most_1500(): ...  # enumerate all ranges (macd: slow, signal only)
```

- [ ] **Step 2: Write W1** in `tests/domain/test_indicator_warmup.py`: `test_w1_seed_weight_is_at_most_e_minus_9_for_every_allowed_value`, computing with `math.log1p` exactly the expressions of spec 5.2 at `spec.warmup_slots` (built through `parse_indicators`, so the test checks the formulas the code uses):
  - `rsi`, `atr`: `(W − p)·log1p(−1/p)`;
  - `ema`: `(W − p + 1)·log1p(−2/(p+1))`;
  - `adx`: `log(r^(W−2p+1) + ((W−2p+1)/p)·r^(W−p+1))`, `r = 1 − 1/p`;
  - `macd` for every `(slow, signal)` with `fast = 2`: `log(r_s^(W−s+1) + Σ_{j=s+g−1..W} α_g·r_g^(W−j)·r_s^(j−s+1) + r_g^(W−s−g+2))`, `α_g = 2/(g+1)`, `r_g = 1 − α_g`, `r_s = 1 − 2/(s+1)`.
  Each must be `≤ −9`. Add `test_w1_detects_a_shorter_formula`: the ADX expression at `12 × p` exceeds `−9` for some `p` (the test proves itself sensitive).
- [ ] **Step 3: Run** `uv run pytest tests/domain/test_indicators.py tests/domain/test_indicator_warmup.py -v` → FAIL (unsupported indicator).
- [ ] **Step 4: Implement** the five `DEFINITIONS` entries and `check` (MACD: `"fast must be less than slow"` when `fast >= slow`), called in `_parse_item` after all parameters are known.
- [ ] **Step 5: Run** the same command plus `uv run pytest tests/domain` → PASS (the existing `test_rejects` case `"ema:period=14"` now parses: replace that one case with `"sma:period=14"`, an unsupported name; no other existing expectation changes).
- [ ] **Step 6: Commit** `feat: define the S19 indicators and their warm-ups`.

### Task 2: Segments over high, low and close

**Files:**
- Modify: `src/quasar_market_data/domain/indicator_series.py`
- Test: `tests/domain/test_indicator_series.py`

**Interfaces:**
- Produces:
  - `type Prices = tuple[float, float, float]` (high, low, close).
  - `@dataclass(frozen=True) class SegmentPrices: high: tuple[float, ...]; low: tuple[float, ...]; close: tuple[float, ...]` (equal lengths).
  - `type Compute = Callable[[SegmentPrices], Sequence[Sequence[float]]]`.
  - `evaluate(*, start, end, timeframe_ms, warmup_slots, states, prices: Mapping[int, Prices], compute, output_count) -> IndicatorSeries` (`closes` renamed to `prices`; checks and results otherwise unchanged).

- [ ] **Step 1: Adapt the existing tests to the new signature only.** `Recorder.__call__(self, segment: SegmentPrices)` records `list(segment.close)` and returns the same running sum of `segment.close`; every `closes=` becomes `prices=` with `(c, c, c)` tuples (helper `def as_prices(closes: Mapping[int, float]) -> dict[int, Prices]` in the test module). No expected value changes.
- [ ] **Step 2: Write the failing tests:** `test_compute_receives_aligned_high_low_close_of_the_segment` (distinct high, low, close per slot; a recorder captures each `SegmentPrices`; for A4's layout, each call's three tuples equal the stored values of exactly that segment, in order) and `test_prices_must_hold_exactly_the_present_slots` (extra or missing key → `ValueError`, as for closes today).
- [ ] **Step 3: Run** `uv run pytest tests/domain/test_indicator_series.py -v` → the new tests FAIL.
- [ ] **Step 4: Implement** the types and the rename; `evaluate` builds one `SegmentPrices` per segment.
- [ ] **Step 5: Run** `uv run pytest tests/domain -v` → PASS. (The API modules still pass `closes=`; Task 3 fixes them, so do not run the full suite yet.)
- [ ] **Step 6: Commit** together with Task 3 (one commit leaves the tree green).

### Task 3: Read high and low; RSI stays byte-identical

**Files:**
- Modify: `src/quasar_market_data/api/indicator_reader.py`, `src/quasar_market_data/api/indicator_results.py`, `src/quasar_market_data/api/indicator_engine.py` (RSI only: read `segment.close`), `tests/api/bench_indicators.py` (the `IndicatorInputs` field rename only)
- Test: `tests/api/test_indicator_reader.py`, `tests/api/test_indicators_endpoint.py`, `tests/api/test_indicator_engine.py` (helper adaptation)

**Interfaces:**
- Consumes: Task 2 `Prices`, `SegmentPrices`, `evaluate(prices=...)`.
- Produces: `IndicatorInputs.prices: Mapping[int, tuple[str, str, str]]` (high, low, close text) replacing `closes`; the statement `SELECT open_time_ms, high, low, close FROM candles WHERE ... AND open_time_ms >= ? AND open_time_ms < ?` (same bounds as S18).

- [ ] **Step 1: Adapt helpers:** `rsi_engine` in `test_indicator_engine.py` calls `compute(SegmentPrices(c, c, c))` with `c = tuple(closes)`; reader tests read `inputs.prices[t][2]` where they read `closes[t]`. No expected value changes.
- [ ] **Step 2: Write the failing tests:**
  - `test_indicator_reader.py::test_reads_high_low_close_of_the_read_range` (values equal the seeded text; still no row before `read_start`).
  - `test_indicators_endpoint.py::test_rsi_ignores_high_and_low`: two databases with the same closes, one with `high`/`low` from `insert_candle(close=...)` and one with every `high` raised by `1000` and every `low` lowered by `1`; RSI(14) bodies byte-identical.
  - `test_indicators_endpoint.py::test_a_conflicted_slot_never_feeds_high_or_low` (request `rsi:period=14`: RSI's compute receives the whole `SegmentPrices`, so the spy sees high and low before any HLC indicator exists): seed a conflicted slot at `s + 200` whose stored `high` is `10×` the close (direct `UPDATE candles`); monkeypatch `compute_function` to wrap the real one and record every `SegmentPrices`; assert no recorded segment contains that high and no segment spans `s + 200`.
- [ ] **Step 3: Run** `uv run pytest tests/api/test_indicator_reader.py tests/api/test_indicators_endpoint.py -v` → new tests FAIL.
- [ ] **Step 4: Implement:** reader selects the three columns; `indicator_results` passes `{t: (float(h), float(l), float(c))}` for present slots; RSI's compute reads `segment.close`.
- [ ] **Step 5: Run** `make check` → PASS, and `git diff --stat tests/fixtures` is empty (golden untouched).
- [ ] **Step 6: Commit** `refactor: evaluate indicator segments over high, low and close` (Tasks 2 and 3).

### Task 4: TA-Lib functions for the five indicators, golden and references

**Files:**
- Modify: `src/quasar_market_data/api/indicator_engine.py`, `tests/indicator_data.py`
- Create: `tests/api/indicator_reference.py`, `tests/fixtures/indicators/s19_golden.json`
- Test: `tests/api/test_indicator_engine.py`

**Interfaces:**
- Consumes: `SegmentPrices`; `IndicatorSpec.param(name)`.
- Produces: `compute_function(spec)` for every name of `DEFINITIONS`; `generated_bars(count: int, seed: int) -> list[tuple[str, str, str, str]]` (open, high, low, close canonical text, `high ≥ max(open, close)`, `low ≤ min(open, close)`, `low ≥ 0.01`; closes equal `generated_closes(count, seed)`, open is the previous close, wicks from the same seeded `random.Random`); references `ema_ref`, `macd_ref`, `atr_ref`, `adx_ref` (returning `adx, plus_di, minus_di`), `bbands_ref` in `tests/api/indicator_reference.py`, plain Python floats, NaN before each TA-Lib lookback.

- [ ] **Step 1: Write the failing tests** in `test_indicator_engine.py`:
  - `test_r4_<name>_matches_the_reference` for each new name, at the minimum, a typical (`ema` 21, `macd` 12/26/9, `atr`/`adx` 14, `bbands` 20/2) and the maximum parameters, on `generated_bars(2000, seed=11)` and on the existing `rising`, `falling`, `alternating` shapes: every index past the TA-Lib lookback agrees within `1e-9 × max(1, |reference|)`. Add `test_r4_tolerance_is_measured` style docstrings with the measured maximum.
  - `test_r4_argument_order_is_detected`: the ATR reference with high and low swapped differs from the engine (so R4 would catch the swap).
  - `macd_ref` implements the seeding of "Facts checked" (late fast seed, signal seeded from `macd[slow−1 ..]`), written from that description and not from the engine. `test_r4_macd_matches_the_reference` additionally covers `(2,3,2)`, `(5,35,5)` and `(99,100,50)` and asserts **from the first emitted index** (`slow + signal − 2`), where seeding differences are largest, with the same `1e-9` relative tolerance as every R4 test: no wider tolerance for MACD. `test_r4_macd_lookback`: the first non-NaN index of every output is `slow + signal − 2` in both engine and reference.
  - `test_r4_macd_rejects_independent_emas`: a reference built from two EMAs seeded at index 0 differs from the engine by more than `1e-3` at the first emitted index for `(12,26,9)` (so R4 is sensitive to the seeding). If the engine ever disagrees with `macd_ref` beyond the tolerance, stop and report the indices and values; do not widen the tolerance.
  - `test_r3_s19_golden_strings`: two parameter sets per name (typical and maximum) on `generated_bars(2000, seed=7)`, strings from `float_text` of every value from `warmup_slots` on, compared with `s19_golden.json`; regenerate the file only deliberately (as `rsi_golden.json`).
  - `test_no_nan_after_warmup_at_extreme_parameters`: for each extreme of spec 5.2.1 and Review Focus 3, a segment of exactly `warmup_slots + 1` bars gives finite values in every output at the last index.
  - `test_macd_histogram_is_talibs_third_output` and `test_bbands_bands_are_symmetric_around_the_middle` (`upper − middle == middle − lower` within `1e-9` relative).
- [ ] **Step 2: Run** `uv run pytest tests/api/test_indicator_engine.py -v` → FAIL (`no engine function`).
- [ ] **Step 3: Implement** `compute_function` branches with the calls of Global Constraints, each converting `SegmentPrices` tuples with `numpy.asarray(..., dtype=numpy.float64)` and returning one list per output in the defined order. Write the references and `generated_bars`; generate `s19_golden.json` with a one-off script (not committed) and inspect it before committing.
- [ ] **Step 4: Run** `uv run pytest tests/api/test_indicator_engine.py -v` and `make check` → PASS; `rsi_golden.json` unchanged.
- [ ] **Step 5: Commit** `feat: compute EMA, MACD, ATR, ADX and Bollinger Bands`.

### Task 5: Convergence observations and flat-run characterization

**Files:**
- Modify: `tests/indicator_data.py`
- Create: `tests/api/test_indicator_convergence.py`
- Modify: `docs/follow-ups.md` (F-20)

**Interfaces:**
- Consumes: `compute_function`, `SegmentPrices`.
- Produces: `shaped_bars(shape: Literal["walk", "tiny_tr", "trend_reversal", "volatility_collapse", "flat_after_move"], count: int, seed: int) -> SegmentPrices` (floats, seeded `random.Random`; the shapes of spec 5.2.1: walk at a 60 000 level, price `1e-5` with moves `1e-12`, a constant up-drift then down-drift, step size and wick dropping 1000× at the midpoint, a move then `high = low = close` constant from one third).

- [ ] **Step 1: Write the tests:**
  - `test_c1_<name>_anchored_matches_whole_history` for `rsi`, `ema`, `macd`, `atr`, `adx` at the minimum and maximum parameters, on `walk`, `tiny_tr`, `trend_reversal`, `volatility_collapse` (6000 bars; points 2600..6000 step 37): the value computed from `t − warmup_slots` against the value from bar 0. Tolerance per (name, shape) = the measured maximum × 2, rounded up to one significant digit, in points for RSI/ADX/DI and relative to the close for `ema`, `macd`, `atr`; the docstring records the measured values and the spec 5.2.1 table as the expected order of magnitude. If a measurement differs from spec 5.2.1 by more than 10×, stop and report it before choosing a tolerance.
  - `test_c1_bbands_depends_only_on_the_last_period_closes`: anchored against whole history within `1e-9` relative on all four shapes.
  - `test_flat_run_adx_and_di_read_zero_only_when_the_whole_range_is_flat` (characterization, like the RSI test of F-20): `flat_after_move`, `adx:period=14`; anchored inside the flat run → `0` for `adx`, `plus_di`, `minus_di`; anchored before it → the frozen non-zero DI (record the observed values as constants with an "OBSERVED on the locked build" comment).
  - `test_flat_run_atr_is_zero_and_bbands_collapse`: ATR `0` over a flat range from the anchor; `upper == middle == lower` there.
  - `test_tiny_true_ranges_give_ordinary_values`: `adx`, DI, `atr`, `rsi` with true ranges of `1e-13`, `1e-15`, `1e-17` on a `1e-5` price: all finite, ADX within `(0, 100)`.
- [ ] **Step 2: Run** `uv run pytest tests/api/test_indicator_convergence.py -v` → PASS once tolerances are measured (these tests observe; they need no production change).
- [ ] **Step 3: F-20:** extend the entry: ADX and both DI show the same split (cite the characterization test), and the user's F-20 decision applies to every indicator.
- [ ] **Step 4: Commit** `test: observe S19 warm-up convergence and flat runs`.

### Task 6: Endpoint, J7 and documentation

**Files:**
- Modify: `src/quasar_market_data/api/app.py`, `README.md`, `docs/invariants.md`
- Test: `tests/api/test_indicators_endpoint.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `_indicators_response(inputs, specs, series, as_of_ms, rule) -> Response` in `app.py` (returns `_json(200, _indicators_body(...))`), called through `run_in_threadpool`.

- [ ] **Step 1: Write the failing tests:**
  - `test_mixed_request_each_indicator_equals_itself_alone`: `indicators=rsi:period=100;ema:period=200;macd:fast=100,slow=200,signal=50;atr:period=100;adx:period=100;bbands:period=200,stddev=5;ema:period=2;macd:fast=12,slow=26,signal=9;adx:period=14;bbands:period=20,stddev=2` over 2016 slots with present history from `s − 1600`; the traced read starts at `s − 1500 slots`; each indicator's JSON equals the same item requested alone.
  - `test_three_output_indicator_shape_and_latest`: `macd` and `adx` items have `outputs` and `values` keys in the order of Global Constraints; under L2/L3 conditions `latest` is `{"values": null, "reason": "due"|"stale"}`.
  - `test_s19_segment_scenarios` parametrized over `ema:period=14`, `macd:fast=12,slow=26,signal=9`, `atr:period=14`, `adx:period=14`, `bbands:period=20,stddev=2`: A3, A4, A6 and A8 of the S18 design with that indicator's `warmup_slots` (first value index and reasons exactly as there).
  - `test_a_non_finite_value_in_a_later_output_is_a_500`: monkeypatched compute returns finite `macd` and `signal`, `inf` `histogram` → `500 internal_error`.
  - `test_the_body_is_built_off_the_event_loop`: wrap `app_module._indicators_body`; inside the wrapper `asyncio.get_running_loop()` raises `RuntimeError`.
- [ ] **Step 2: Run** `uv run pytest tests/api/test_indicators_endpoint.py -v` → the J7 test FAILS (the others may pass already; they pin behaviour).
- [ ] **Step 3: Implement** `_indicators_response` and `return await run_in_threadpool(_indicators_response, ...)` in the handler; nothing else in `app.py` changes.
- [ ] **Step 4: Docs.** README indicators paragraph: the six names with parameters and warm-ups, the read bound 3516, the observation per data shape (spec 5.2.1), the flat-run note extended to ADX/DI. `docs/invariants.md`: INV-059 names the formulas of Global Constraints; INV-058..062 evidence adds the Task 1–6 tests (W1, `test_compute_receives_aligned_high_low_close_of_the_segment`, `test_a_conflicted_slot_never_feeds_high_or_low`, `test_rsi_ignores_high_and_low`, `test_s19_segment_scenarios`, `test_mixed_request_each_indicator_equals_itself_alone`, `test_r3_s19_golden_strings`, `test_a_non_finite_value_in_a_later_output_is_a_500`); "Not covered" gains: no bound in output units for any smoothed indicator (W1 proves seed decay only), Bollinger digits depend on the segment start.
- [ ] **Step 5: Run** `make check` → PASS (includes `tests/test_invariant_register.py`).
- [ ] **Step 6: Commit** `feat: serve the S19 indicators on GET /v1/indicators`.

### Task 7: Benchmark: worst case, concurrency, `/health`, CPU and memory

**Files:**
- Modify: `tests/api/bench_indicators.py`, `tests/api/test_indicators_bench_smoke.py`, `README.md`, `docs/follow-ups.md` (F-21)

**Interfaces:**
- Consumes: `generated_bars`; `quasar-api serve --db PATH --host 127.0.0.1 --port N` started as `tests/test_api_process.py` starts it.
- Produces: `main(argv)` with `--repeat`, `--series`, `--profile {s18,s19}` (default `s19`), `--concurrency LIST` (default `1,2,4,8`; `0` skips the served part); printed JSON with keys `profile`, `window_slots`, `read_range_slots`, `phases_ms`, `body_bytes`, `environment`, and `served` (per level: `latency_ms` min/median/max, `body_bytes`, `health_ms` min/median/max, `health_over_100ms`, `server_cpu_user_s`, `server_cpu_system_s`, `server_peak_rss_bytes`) plus `served_note` (the measurement limits below, printed verbatim).

**Memory and CPU measurement (why not `RUSAGE_CHILDREN`).** `getrusage(RUSAGE_CHILDREN).ru_maxrss` is the largest peak among all waited-for children so far, not a sum: a difference of two readings is not one server's peak, and after the first level it can only stay or grow. Instead, each concurrency level starts its **own** server process, and the benchmark reaps it with `os.wait4(proc.pid, 0)`, which returns that one child's `rusage` (Linux and macOS). From it: `ru_utime`, `ru_stime` (that server's CPU over its life) and `ru_maxrss` normalized to bytes (`× 1024` on Linux, as is on macOS, chosen by `sys.platform`). After `wait4`, set `proc.returncode` from the status so `Popen` does not wait again. Limits, stated in `served_note` and the README: the peak covers the server's whole life (start-up, the `/health` readiness polls, the warm-up and all requests of that level), not one request; CPU likewise includes start-up; a server that spawned its own children would not be counted (uvicorn as started here runs one process).

- [ ] **Step 1: Update the smoke test:** `--profile s18 --concurrency 0` asserts today's keys and `read_range_slots == 3016`; `--profile s19 --repeat 1 --series 1 --concurrency 1` asserts `read_range_slots == 3516`, `served["1"]` has every key above, `health_over_100ms` is an integer, `server_peak_rss_bytes > 10_000_000` (a Python process with numpy and TA-Lib loaded; catches a KiB/bytes mix-up), `server_cpu_user_s > 0`, and `served_note` is present. Run it → FAIL.
- [ ] **Step 2: Implement:**
  - `build_database` writes `generated_bars` (so ATR/ADX are not flat); profile `s18` keeps `rsi:period=91..100`; profile `s19` is the ten items of Task 6 Step 1 (read range 3516).
  - Served part: one server subprocess per concurrency level on a free loopback port; wait for `GET /health` `200`; with `httpx.AsyncClient`, `K` concurrent worst-case requests with an explicit `end` (in the generated past), repeated `--repeat` times, while a probe task calls `GET /health` every 10 ms until the requests finish; then stop the server (SIGINT) and reap it with `os.wait4` as described above (timeout: SIGKILL after 5 s, as `tests/test_api_process.py::stop` does; a killed server still reports its `rusage`, and the result records `"stopped": "sigint"` or `"sigkill"`).
  - No switch for J7; the S18 README numbers are quoted only as an orientation reference.
- [ ] **Step 3: Run** `uv run pytest tests/api/test_indicators_bench_smoke.py -v` → PASS; then `make bench-indicators` once.
- [ ] **Step 4: Record** the printed environment and numbers in the README benchmark paragraph (S18 numbers kept as "S18 reference") and update F-21: what is now measured (concurrency 1–8, `/health` latency, per-server CPU and lifetime peak RSS from `wait4`, with J7) and what still is not (per-request memory, cold disk, other machines, more than 8 concurrent requests).
- [ ] **Step 5: Run** `make check` → PASS.
- [ ] **Step 6: Commit** `test: benchmark concurrent S19 indicator requests and /health`. Report the numbers to the user (no criterion, no HARD STOP).

---

## Residual risks

1. Values are reproducible only for the same build; `s19_golden.json` makes a TA-Lib or numpy upgrade visible, not harmless.
2. W1 proves seed decay only. Differences from a whole-history value have no bound in output units; after a volatility collapse they reach `0.1`–`0.5` points (RSI, ADX) and a few percent of ATR, and the F-20 flat-run split applies to RSI, ADX and DI until the user decides F-20.
3. C1 tolerances are measured on generated shapes; real series may differ.
4. Benchmark figures are from one machine on a warm cache; there is still no rate limit (contract 5.6, accepted risk).
