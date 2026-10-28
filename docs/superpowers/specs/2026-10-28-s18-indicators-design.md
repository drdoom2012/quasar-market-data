# S18 — `GET /v1/indicators` with RSI — Design

- **Status:** Draft for approval (brainstormed with the user on 2026-10-28; revision 2 of the same day: segments and
  anchor, `latest`, reproducibility, gates).
- **Inputs:** `docs/architecture.md` §8, spec `2026-10-20-market-data-v1-design.md` (S18, S19, D6), contract
  `docs/contracts/api-v1.md` (sections 1–4, 5.6, 6, 6.1), `docs/invariants.md` (INV-013, INV-014, INV-044).
- **Marking:** "the user" marks what the user decided; "designer's call" marks a choice made without the user, which
  the user can still overturn.

## 1. Intent

Quasar Core reads indicators from this service to evaluate the state of a coin (the user). For one coin it needs the
**latest value** of each indicator and its **history for the trend**, a few days back and **at most one week** (the
user). Core asks for **several indicators in one request** (the user). The query principles are **the same for every
indicator** (the user): S18 fixes the request and response shape for all indicators of S19 (EMA, MACD, ATR, ADX,
Bollinger Bands) and implements only RSI.

Core must be able to tell a value it can trust from no value, and must never get a value computed from missing,
conflicted or stale data presented as if it were sound.

## 2. Goals

- `GET /v1/indicators` for one series (venue, symbol, `5m`), a window of at most 7 days, and 1–10 indicators.
- RSI with `period` as a parameter, 2..100 (the user agreed to the parameter; the bounds are the designer's call).
- Per point and per indicator: a value, or `null` with a reason.
- Per indicator: `latest`, the window's last point (5.5).
- Reproducible values within the scope of 5.6.

## 3. Non-goals

- Indicators other than RSI (S19). The request syntax, response shape and segment and warm-up rules must accommodate
  them without a `/v2`, but nothing of them is implemented.
- Persisting or caching indicator values (spec §3).
- Timeframes other than `5m`, several symbols per request, pagination.
- An RSI equal to the one computed over the whole stored history (5.4).

## 4. Anti-goals (failure even if it works)

- A value computed across a missing, conflicted or unavailable slot, or from a slot outside the indicator's read range.
- A value emitted before its warm-up is complete.
- An older value presented in `latest` when the window's last slot has no sound value.
- One old gap making the whole response unusable (why per-point reasons were chosen over refusing the request).
- An indicator's values changing because another indicator was requested alongside it.
- A `4xx` or `5xx` for a data-quality situation; those are `200` with reasons. Errors stay for bad requests and an
  unreadable database (contract section 2).

## 5. Decided behaviour

### 5.1 Request

```
GET /v1/indicators?venue=binance-usdm&symbol=BTC/USDT:USDT&timeframe=5m&start=…&end=…
    &indicators=rsi:period=14;rsi:period=7
```

| Parameter | Rule |
|---|---|
| `venue`, `symbol`, `timeframe` | As in contract section 5.1. `timeframe` is `5m` only in v1 but stays a parameter (designer's call). |
| `start` | Required, inclusive, aligned, as in section 5.1. |
| `end` | Optional, exclusive, aligned, `> start`. Default in 5.5. |
| Window length | `end - start` at most 2016 slots (7 days of 5m, the user's maximum), for an explicit and for a defaulted `end` alike; longer is `400` with `details.maximum`. A defaulted `end` is never capped (5.5). |
| `indicators` | Required. `name:key=value,key=value;name:key=value…`. 1..10 indicators. Every parameter of an indicator is **required** (no defaults: architecture §8 "explicit parameters"). Unknown name, unknown or missing key, a value outside its range, a repeated key, or the same indicator with the same parameters twice is `400`. Integer values follow the section 1 integer rule. |

**One parameter, not a repeated one (designer's call; the user saw a repeated `indicator=` shape).** Contract
section 1 rejects repeated query parameters for every `/v1` endpoint. A single `indicators` parameter with `;`
between indicators keeps that rule without an exception. The meaning is what the user approved.

The value contains `:`, `=`, `,` and `;`. The current parser (`api/params.py`, `parse_qsl` with the default `&`
separator) splits only at `&` and splits a pair at its first `=`, so the value reaches the handler whole; the
percent-encoded forms of the four characters decode to the same value. A test pins both forms.

**Normalized id.** `name:key=value,…` with keys in the order the indicator defines (RSI: `rsi:period=14`). The response
uses this id; indicators keep request order.

**RSI parameters.** `period`, integer, 2..100. `warmup_slots = 10 × period` (5.4).

### 5.2 Point states and reasons

Every slot of `[start, end)` is a point. For each indicator a point carries either `values` (an object, one entry per
output; RSI has the one output `rsi`) and `reason: null`, or `values: null` and a `reason`. The first matching row
wins:

| Order | Reason | When |
|---|---|---|
| 1 | `conflicted`, `unavailable`, `missing` | The point's own slot has this state (contract section 4, its order). |
| 2 | `not_yet_expected`, `due`, `stale` | The point's own slot is `beyond_stored` (after the newest stored candle), split by the server clock exactly as in section 6.1.2, with the same `rule`. |
| 3 | `warmup` | The point's own slot is `present`, but fewer than `warmup_slots` present slots precede it in its segment (5.3). |
| 4 | — (a value) | Otherwise. |

Rows 1, 3 and 4 depend only on stored rows; only row 2 depends on the clock.

### 5.3 Read range, segments and anchor

Definitions, per indicator `i` with warm-up `W_i = warmup_slots(params_i)`:

1. **Read range** `R_i = [start − W_i × timeframe, end)`. Only slots of `R_i` can influence indicator `i`. Rows before
   `R_i` are never part of its input, even when they were read for another indicator of the same request (the
   database is read once, for `[start − max_i W_i × timeframe, end)`, and each indicator takes its own `R_i` from it).
   **Lower bound:** when `start − W_i × timeframe` is below the minimum valid time `T_min = 946684800000` (contract
   section 1; aligned to 5m), the read range starts at `T_min` instead: `R_i = [max(start − W_i × timeframe, T_min),
   end)`, and the same clamp applies to the single database read. No candle can be stored before `T_min`, so the
   clamped slots are exactly the slots that would have been `missing`; the result equals the unclamped definition and
   no time below `T_min` is ever computed into a query or a response.
2. **Segment** of `R_i`: a maximal run of consecutive `present` slots **inside `R_i`**. Any non-present slot (row 1 or
   2 of 5.2) ends a segment. Slots before the oldest stored candle are `missing` (contract section 4), so the start of
   the stored history is a segment boundary like any gap. A segment that began before `R_i` is cut at `R_i`'s start.
3. **Anchor** of a segment: its first slot. For a segment that was cut at `R_i`'s start the anchor is
   `start − W_i × timeframe`; for every other segment it is the first present slot after a non-present one, which
   depends on the stored data only.
4. **Computation.** The indicator function is called **once per segment**, with that segment's closes from its anchor
   to its last slot in `R_i`, in ascending order. Closes of two segments are never joined into one call, so no
   initialisation (for RSI: the seed averages of TA-Lib's Wilder RSI) ever spans a segment boundary.
5. **Warm-up.** Let `k` be the 0-based index of a present slot in its segment (`k` present slots precede it there). The
   point has a value only if `k ≥ W_i`; otherwise it is `warmup`. For RSI, `W_i = 10 × period ≥ period`, so TA-Lib's
   own first output (index `period`) is always before the first emitted point.
6. **Resumption.** After a non-present slot the next present slot starts a new segment with `k = 0`; values return at
   its `W_i`-th present slot after the gap, never earlier, whatever the earlier segment held.

Consequences: a point at `start` has a value only if all `W_i` slots before it are present; the first segment of a
window can be anchored before `start`; points before `start` are computed but never returned.

`anchor_open_time_ms` in the response (5.7) is the anchor of the segment that contains the first returned point with a
value, or `null` when no returned point has a value.

**Test scenarios** (each with RSI(14), `W = 140`, generated deterministic closes; "values" means the emitted strings):

| # | Stored data relative to `start` (`s`, in slots) | Expected |
|---|---|---|
| A1 | Present from `s − 500` to the end | Anchor `s − 140`; every point has a value; values equal a direct computation over exactly `[s − 140, end)`. |
| A2 | As A1 plus a missing slot at `s − 300` (outside `R`) | Values byte-identical to A1: data before `R` never matters. |
| A3 | As A1 plus a missing slot at `s − 50` (inside the warm-up) | Anchor `s − 49`; points `s .. s + 90` are `warmup`; the first value at `s + 91`, equal to a direct computation over `[s − 49, …)`. |
| A4 | Present history, a missing slot at `s + 200`, present again after it | Point `s + 200` is `missing`; `s + 201 .. s + 340` are `warmup`; values return at `s + 341`, equal to a computation over `[s + 201, …)` alone (byte-identical to the same request on a database where everything before `s + 201` is deleted). |
| A5 | As A4, with a `conflicted` slot instead of the missing one | Same as A4 with reason `conflicted`. |
| A6 | `start` itself missing, present after it | Point `s` is `missing`; the segment starts at `s + 1`; first value at `s + 141`. |
| A7 | Series' oldest candle at `s − 20` | Anchor `s − 20`; first value at `s + 120`; no value earlier. |
| A8 | Boundary: a segment with exactly `W` present slots before a point, and one with `W − 1` | The first point has a value, the second is `warmup`. |
| A9 | A segment shorter than `period + 1` slots | Every point `warmup`; no NaN reaches the response. |
| A10 | RSI(14) and RSI(100) in one request, history present from `s − 2000` | Each indicator's values byte-identical to the same request with that indicator alone. |
| A11 | Any of A3–A6 and A12 | A spy on the adapter records every call: no call's input spans a non-present slot or a slot outside `R_i`. |
| A12 | `start = T_min + 50` slots, present from `T_min`; and `start = T_min` | Read range and anchor `T_min`, no query bound below `T_min`; first value at `s + 90` (and at `s + 140` for `start = T_min`); earlier points `warmup`. |

### 5.4 Warm-up is an approximation

Wilder's RSI carries the influence of its seed through every later value. Starting at the anchor instead of at the
oldest stored candle is an **accepted approximation** (the user): after `W = 10 × period` steps, the seed state's
weight in both Wilder averages is `(1 − 1/period)^(W − period)`, which is at most `e^−9` (about `1.2 × 10^−4`). That
is a statement about the weight of the averages, not a guaranteed bound on the difference in RSI points. **No
promise is made that a value equals the RSI over the whole stored history**, or that two requests with different
`start` give the same value for the same slot; the contract says so.

Tests:

- **C1 (observation, not a guarantee):** on generated data with 5000 present slots, the difference between the
  emitted value and a computation from the first slot is below `10^−2` RSI points for every `period` in
  `{2, 14, 100}`. The tolerance is recorded as measured; the test exists so a regression (for example an anchor
  shorter than `W`) is noticed.
- **C2:** moving `start` by one slot inside a long present history changes overlapping values only within the same
  tolerance, and each of the two requests is reproducible on its own (5.6).

### 5.5 `latest`

`latest` is the **last point of the requested window** (`end − timeframe`), repeated with its `open_time_ms`, `values`
and `reason`. **It is not the current market value.** It is the newest slot that is due only when the window ends
there, which the default `end` arranges:

- **Default `end`:** the end of the newest expected slot at the server clock (the newest slot with
  `expected_at <= as_of_ms`, contract section 6.1.2). If that is `<= start`, the window is the one slot at `start`
  (mirrors section 5.1). If `end − start` would exceed 2016 slots, the answer is `400` with `details.maximum`, never a
  capped window: a capped default would silently make `latest` a historical point.
- **Explicit `end`:** `latest` is whatever slot precedes it. In the past, it is an ordinary historical point: a
  present slot has its value (it is not `stale`, because `stale` applies only to slots after the newest stored
  candle), a gap is `missing`. In the future, it is `not_yet_expected`.

Core reads the current value by omitting `end` and should compare `latest.open_time_ms` with `as_of_ms`; the
response states both. Freshness uses the API's existing `--publication-delay-ms` and `--lag-threshold-ms` and the
same rule as `/v1/coverage` (designer's call, agreed with the user). No field is added for this.

Tests:

| # | Situation | Expected |
|---|---|---|
| L1 | Default `end`, ingest current, last slot present with full warm-up | `latest` has a value, `open_time_ms` is the newest expected slot. |
| L2 | Default `end`, ingest stopped, newest stored candle within the lag threshold | Last point `due`, `latest.values = null`; earlier points still have values. |
| L3 | Default `end`, ingest stopped beyond the threshold | Trailing points `stale`, `latest.reason = "stale"`, never the last stored value. |
| L4 | Default `end`, ingest stopped so long that `end − start` exceeds 2016 slots | `400`, `details.maximum`; no capped window. |
| L5 | Explicit `end` a month in the past, last slot present | `latest` has a value and no clock-based reason, whatever the clock. |
| L6 | Explicit `end` in the past, last slot missing | `latest.reason = "missing"`. |
| L7 | Explicit `end` in the future | Trailing points `not_yet_expected`, as is `latest`. |
| L8 | Default `end <= start` | One point at `start`; `latest` is that point. |

### 5.6 Reproducibility

**What is guaranteed.** For one indicator, the `values` strings and the rows 1, 3 and 4 reasons of 5.2 are
**byte-identical** when all of these are the same: the effective `start` and `end`, the indicator id, the stored rows
in `R_i` (candles, conflict and unavailable records), and the build (the `TA-Lib` and `numpy` versions pinned by
`uv.lock`, the TA-Lib C library and the platform). Nothing else enters the computation: no other indicator of the
request, no row before `R_i`, no clock, no TA-Lib global setting.

**What is not.** The whole response body is not byte-identical across requests: `as_of_ms` and the row 2 reasons
(`not_yet_expected`, `due`, `stale`) depend on the clock, and a defaulted `end` moves with it. Across TA-Lib or numpy
versions, or another platform or C library build, the last digits of a value may change; the contract says results are
reproducible only for the same build. A dependency upgrade is a deliberate change (golden test below).

**Deterministic serialization.**

- Inputs: each stored close (canonical decimal text) is converted with `float(text)`, which is correctly rounded and
  platform-independent.
- Outputs: the shortest decimal that round-trips to the float64 (Python's `repr` digits, which are deterministic),
  written in positional notation: no exponent, no trailing fractional zero, no `.` without digits after it,
  `-0.0` written as `0`. A `-` is possible (MACD in S19). NaN or infinity at a point that passed warm-up is a `500`
  (a bug), never a value.
- JSON: the existing `_json` encoder (fixed separators, no ASCII escaping); keys in the order of 5.7; indicators in
  request order; points ascending by `open_time_ms`; `params` in the indicator's key order.

**TA-Lib global state** (`set_unstable_period`, compatibility mode) is never changed; warm-up is the rule of 5.3, not
TA-Lib's unstable period.

Tests:

- **R1:** the same request twice against the same database with a fixed clock gives byte-identical bodies.
- **R2:** formatting of `0.0`, `-0.0`, `100.0`, `1e-07`, `5e-324`, `123456789.125` and a value with 17 significant
  digits: no exponent, no trailing zero, and `float(text) == value` for each.
- **R3 (golden):** RSI(14) and RSI(2) on a committed generated fixture produce committed exact strings. The test fails
  on a TA-Lib or numpy upgrade that changes a digit; the golden file is then updated deliberately, in its own commit.
- **R4 (reference):** an independent pure-Python Wilder RSI in the tests agrees with the adapter on the same segments
  within `10^−9` RSI points (float rounding only), so a wrong TA-Lib call or slicing is caught independently of R3.
- **R5:** a segment with constant closes (no gains or losses) pins TA-Lib's output for that case, so it is a known
  value and not an accident.
- A10 (5.3) is the test that no other indicator influences a value.

### 5.7 Response `200`

Shape (field names are the designer's proposal; the contract section is approved separately, 6). **The example is an
abbreviated illustration:** its window has 100 slots, but only two `points` are shown and the values are not computed
from real data. A real response lists every slot of `[start, end)`.

```json
{
  "venue": "binance-usdm", "symbol": "BTC/USDT:USDT", "timeframe": "5m",
  "start": 1792643400000, "end": 1792673400000,
  "as_of_ms": 1792673430000,
  "rule": { "publication_delay_ms": 15000, "lag_threshold_ms": 60000 },
  "series": { "oldest_open_time_ms": 1792500000000, "newest_open_time_ms": 1792673100000 },
  "indicators": [
    {
      "id": "rsi:period=14", "name": "rsi", "params": { "period": 14 }, "outputs": ["rsi"],
      "warmup_slots": 140, "anchor_open_time_ms": 1792601400000,
      "latest": { "open_time_ms": 1792673100000, "values": { "rsi": "54.21378915036672" }, "reason": null },
      "points": [
        { "open_time_ms": 1792643400000, "values": { "rsi": "48.9930125837313" }, "reason": null },
        { "open_time_ms": 1792643700000, "values": null, "reason": "missing" }
      ]
    }
  ]
}
```

- `points` covers every slot of `[start, end)` in ascending order, the same open times for every indicator.
- `series` is as in contract section 5.2.
- Errors: as contract section 2, in its order: `400 invalid_parameter` (every `indicators` error with
  `details.parameter = "indicators"`), `404 unknown_series`, `503 not_ready`, `500 internal_error`. A server clock
  outside the valid range is `500`, as in section 6.1.5.

## 6. Constraints, gates and invariants

**Module boundaries.** Indicator definitions (names, parameters, ranges, outputs, `warmup_slots`), id normalization,
read ranges, segments, anchors and point reasons are pure domain code without TA-Lib (the existing `domain is pure`
contract already forbids `talib`). TA-Lib is called from one adapter module in `api`, which receives one segment's
closes as floats and returns floats. A new import-linter contract allows `talib` in that module only (designer's
call, the CCXT pattern). TA-Lib and numpy are native code in the API process; that is consistent with INV-044, whose
scan is name-based and does not cover third-party packages (its "Not covered" cell).

**Database read.** One explicit read transaction per request, on the read-only connection (INV-014, contract 5.6),
off the event loop. The read is bounded by construction: candles of one series in
`[start − max_i W_i × timeframe, end)`, at most `2016 + 1000 = 3016` slots, on the primary key, plus the conflict and
unavailable records of the same range (the same states as section 4). A test pins the read range (no row before it is
read). No benchmark gate is needed (3016 rows against the 10 000-slot reads of `/v1/candles` and `/v1/coverage`);
designer's call.

**Dependencies.** `TA-Lib` and `numpy` become runtime dependencies, pinned in `uv.lock` (spec D6 planned TA-Lib for
this slice and expects the native C library from Homebrew locally and a Docker build step in S21). If the PyPI wheel
does not bundle the C library on the user's platform, the builder reports it before adding the dependency.

**Approval gates.**

1. **HARD STOP 1: contract section 6.3** (`GET /v1/indicators`) is written from this design, including 5.4 (no
   whole-history promise), 5.5 (`latest` is the window's last point) and 5.6 (reproducible only for the same build),
   and the change of the section 6 reserved row. It is approved by the user before any implementation task, as for
   6.1 and 6.2.
2. **HARD STOP 2: the dependency** is added only after the builder confirms the install on the user's machine (see
   above).

**Invariants** (new rows in `docs/invariants.md`, next free IDs from INV-058, ENFORCED only with the named negative
tests of this design):

| Invariant | Evidence (tests of this design) |
|---|---|
| No indicator value is computed from a non-present slot or from a slot outside its read range, and no initialisation spans a segment boundary | A2–A6, A11 |
| No value before `warmup_slots` present slots in its segment | A3, A6–A9 |
| `latest` is the window's last point and never carries an older value | L2, L3, L4, L6 |
| A value depends only on the window, the id, the rows in its read range and the build | A2, A10, R1, R3 |
| Indicator values are float64 results serialized without exponent and round-trip exactly; NaN is never emitted | R2, A9 |

Not covered and stated in the register: equality with a whole-history RSI (5.4), reproducibility across builds (5.6).

## 7. Left to the builder

- Module and function names; whether the reader shares the window selects with the candle and coverage readers
  (follow-up F-9 already notes one duplicate).
- The generator for the deterministic test closes, and the exact tolerances of C1 and C2 once measured.
- The float-formatting function, provided it meets 5.6 and R2.
