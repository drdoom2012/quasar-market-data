# S19 — EMA, MACD, ATR, ADX and Bollinger Bands on `GET /v1/indicators` — Design

- **Status:** Draft for approval (2026-10-28). The user skipped the questions and asked for a design in the S18 manner,
  so every choice below that is not inherited from S18 is a **designer's call** and can be overturned. Revision 2
  (the same day, the user's minimal review): seed decay is separated from numerical convergence and the TA-Lib probe is
  recorded (5.2.1); the benchmark covers concurrent requests and `/health` responsiveness (section 6). No change to
  the API, the indicators, the formulas or J1–J7.
- **Inputs:** spec `2026-10-20-market-data-v1-design.md` (S19, D6), architecture §8, S18 design
  `2026-10-28-s18-indicators-design.md`, contract `docs/contracts/api-v1.md` section 6.3 (decisions I1–I6),
  `docs/invariants.md` (INV-058..062), `docs/follow-ups.md` (F-20, F-21), the S18 code (`domain/indicators.py`,
  `domain/indicator_series.py`, `api/indicator_engine.py`, `api/indicator_reader.py`, `api/indicator_results.py`).
- **Marking:** "the user" marks what the user decided (here: only what was decided for S18 and applies unchanged);
  "designer's call" marks a choice made without the user.

## 1. Intent

S18 fixed the request and response shape of `/v1/indicators` for every indicator and implemented RSI (the user: "the
query principles are the same for every indicator"). S19 adds the remaining indicators of the v1 spec, EMA, MACD, ATR,
ADX and Bollinger Bands, **inside that shape**, so Quasar Core can ask for any mix of them in one request and read a
value it can trust or a reason there is none.

Nothing about segments, reasons, `latest`, the window, number formatting or errors changes. What changes is the list
of indicators, their parameters and warm-ups, the inputs (ATR and ADX need high and low, not only the close) and the
bound on the read.

## 2. Goals

- Five new indicator names on `GET /v1/indicators`: `ema`, `macd`, `atr`, `adx`, `bbands`, each with required integer
  parameters, fixed outputs and a `warmup_slots` formula (5.1).
- One stated warm-up criterion for every smoothed indicator, the one RSI already meets, checked by a test for every
  allowed parameter value (5.2).
- Indicators that need high and low read them from the same present slots as the close; segments and reasons are
  unchanged (5.3).
- RSI responses stay byte-identical to S18 (5.6).
- Contract section 6.3 amended and approved before implementation (HARD STOP 1, section 6).

## 3. Non-goals

- Changing the request or response shape, the reasons, the segment rules, `latest`, the 2016-slot window or the
  10-indicator limit.
- Non-integer parameters (a Bollinger multiplier of `2.5`): the contract's integer rule stays (5.1, decision J4). A
  decimal parameter can be added later without breaking anything.
- Choosing the moving-average type of MACD or Bollinger Bands, or exposing TA-Lib's `matype`; other indicators
  (SMA, Stochastic, VWAP, OBV ...).
- Deciding follow-up F-20 (RSI's 0/0 case). S19 states that ADX has the same kind of case and inherits whatever the
  user decides for F-20 (5.5).
- Caching, persistence, other timeframes, several symbols per request (as S18).

## 4. Anti-goals (failure even if it works)

- Everything in S18 section 4 holds for every new indicator: no value across a non-present slot or from outside the
  read range, none before warm-up, `latest` never an older value, one gap never voids the response, no indicator
  influenced by another one of the request, no `4xx`/`5xx` for a data-quality situation.
- An RSI value that changes because S19 widened the reader or the engine.
- A high or low from a slot whose close was not used (for example a conflicted slot's stored high).
- A warm-up formula that is "about right" for common parameters but too short for an allowed corner (`ema:period=2`,
  `macd:fast=99,slow=100,signal=50`).

## 5. Decided behaviour

### 5.1 Indicators, parameters, outputs

All parameters are required integers (section 1 integer rule, no defaults, decision I2). Ranges are inclusive.

| Name | Parameters (key order of the id) | Outputs (key order of `values`) | `warmup_slots` | TA-Lib call |
|---|---|---|---|---|
| `rsi` (S18, unchanged) | `period` 2..100 | `rsi` | `10 × period` | `RSI(close, period)` |
| `ema` | `period` 2..200 | `ema` | `6 × period` | `EMA(close, period)` |
| `macd` | `fast` 2..100, `slow` 3..200, `signal` 2..50; `fast < slow` | `macd`, `signal`, `histogram` | `6 × (slow + signal)` | `MACD(close, fast, slow, signal)` |
| `atr` | `period` 2..100 | `atr` | `10 × period` | `ATR(high, low, close, period)` |
| `adx` | `period` 2..100 | `adx`, `plus_di`, `minus_di` | `13 × period` | `ADX`, `PLUS_DI`, `MINUS_DI` (high, low, close, period) |
| `bbands` | `period` 2..200, `stddev` 1..5 | `upper`, `middle`, `lower` | `period − 1` | `BBANDS(close, period, stddev, stddev, matype=SMA)` |

Ids: `ema:period=200`, `macd:fast=12,slow=26,signal=9`, `atr:period=14`, `adx:period=14`,
`bbands:period=20,stddev=2`.

Notes (designer's calls unless stated):

- **MACD `fast < slow`.** TA-Lib silently swaps the two when `slow < fast`; the API refuses it instead (`400`,
  `details.parameter` `indicators`), so one MACD has one id. `fast = slow` is also refused. This is the first rule
  between two parameters; the contract's list of `400` causes gets "a constraint between parameters of one indicator".
- **MACD uses EMAs** (TA-Lib's `MACD`, not `MACDEXT`), seeded as TA-Lib seeds them. `histogram = macd − signal` is
  TA-Lib's third output, not recomputed.
- **ADX outputs `plus_di` and `minus_di`** (decision J5): the ADX says how strong a trend is, the two DI say which way,
  and they share the period and the inputs. Three TA-Lib calls on the same segment.
- **Bollinger Bands:** middle band = simple moving average of `period` closes; `upper`/`lower` = middle ±
  `stddev` × the **population** standard deviation of the same closes (TA-Lib's definition). One `stddev` for both
  bands. Integers 1..5 only (decision J4).
- **Bounds.** EMA and Bollinger go to 200 so `ema:period=200` and long bands fit; the MACD bounds keep its warm-up
  at most 1500. Every allowed `warmup_slots` is at most **1500** (`macd:fast=…,slow=200,signal=50`).
- **Signs.** MACD, its signal and histogram can be negative (S18 already allows a leading `-`). Every other output is
  non-negative except the Bollinger lower band, which can be negative near a zero price.

### 5.2 Warm-up criterion

**One criterion for every indicator with memory** (designer's call, J3): at the first emitted point of a segment, the
total weight that the **seeds** of the computation (initial averages TA-Lib computes once and then smooths) still
have in the output is at most `e^−9` (about `1.2 × 10^−4`), **counting every path**: a seed that reaches the output
through a second smoother (the +DM/−DM/TR seeds through the DX values that ADX averages; the slow EMA seed through
the MACD values the signal line averages) counts with its linearised weight on that path. That is exactly what `10 × period` gives RSI in S18 (5.4 there),
so S19 makes RSI's rule the general rule instead of inventing new ones. As in S18 it bounds a weight, not the
difference in output units, and no value is promised to equal a whole-history computation.

| Indicator | Seeds and their decay | Why the formula meets `e^−9` |
|---|---|---|
| `rsi` | Wilder averages, factor `1 − 1/p`, first output at index `p` | `(1 − 1/p)^(9p) ≤ e^−9` |
| `ema` | SMA seed at index `p − 1`, factor `1 − α`, `α = 2/(p+1)` | `(1 − α)^(5p+1) ≤ e^−9` for every `p ≥ 2` (for `p ≥ 7` from `−ln(1−α) ≥ α`; `p = 2..6` checked exactly) |
| `macd` | EMA seeds of `fast` and `slow` aligned at index `slow − 1` (checked on the locked build); signal EMA seed at `slow + signal − 2` | Direct slow seed `r_s^(6(s+g)−s+1)`, plus the signal seed and the slow seed passed through the signal EMA: total at most `e^−9.43` over all allowed `(slow, signal)` (computed); the fast EMA decays faster than the slow one |
| `atr` | Wilder average of TR seeded at index `p` | as RSI |
| `adx` | Wilder sums of +DM, −DM, TR seeded at index `p − 1`; ADX seed (mean of `p` DX) at `2p − 1` | With `r = 1 − 1/p` and `T = warmup_slots`: ADX seed `r^(T−2p+1)` plus the DM/TR seeds through DX `((T−2p+1)/p)·r^(T−p+1)`. At `13p` the total is at most `e^−9.45`; `11p` would leave `e^−7.6`, `12p` `e^−8.5` (computed for `p = 2..100`) |
| `bbands` | none: each value is a function of the last `period` closes | `period − 1` is TA-Lib's own lookback; **not an approximation** |

Test **W1** (pure, domain): for every allowed parameter value of `ema`, `atr`, `adx`, `rsi`, and for every
`(slow, signal)` of `macd` (the faster `fast` does not raise the bound), the logarithm of the total seed weight of the
table above, all paths included, at the formula's `warmup_slots` is `≤ −9`. A formula change that breaks the
criterion for one corner fails the test. W1 tests the formulas against the criterion; C1 (5.6) observes TA-Lib.

Bollinger Bands are exact in principle, but TA-Lib keeps running sums, so the last digits of a value can depend on
where its segment starts (float rounding only). The contract says so; 5.6 states what is reproducible.

**Read bound.** The widest read range is `2016 + 1500 = 3516` slots (S18: `3016`). The contract's "at most
`2016 + 1000`" becomes "`2016 + 1500`".

#### 5.2.1 Seed decay is proven; numerical convergence is only observed

The two are different statements and the design keeps them apart:

- **Proven (W1):** the weight of the seeds in the output, all paths counted, is at most `e^−9`. For ADX the paths
  through DX are a **linearisation**: DI and DX are ratios (`DM/TR`, `|+DI − −DI| / (+DI + −DI)`), so the effect of a
  seed on the output is not its weight. W1 is a statement about the formulas, not an accuracy guarantee.
- **Not claimed:** any bound on the difference between an emitted value and a whole-history computation, in output
  units, for any indicator. The difference is roughly the seed weight **times the size of the pre-anchor state
  relative to the current one** (an old high-volatility average against today's small ranges), and for RSI, DI and
  ADX it is unbounded where the 0/0 case of F-20 is near.

**Probe (designer's run on the locked build, ta-lib 0.8.2 / C 0.8.1, generated data, not a test).** For each point
`t` of 2600..6000 (step 37), the value computed from `t − warmup_slots` against the value computed from slot 0;
largest absolute difference. Price-level indicators on a 60 000 price level.

| Data | RSI (2 / 100) | ADX (14 / 100) | ±DI (14 / 100) | ATR (100) | EMA (200) | MACD (100,200,50) | BBANDS |
|---|---|---|---|---|---|---|---|
| Random walk | 0.0004 / 0.001 | 0.001 / 0.0005 | 5e−5 / 3e−5 | 0.0004 | 0.009 | 0.0005 | ≤ 4e−10 |
| Tiny TR (price 1e−5, moves 1e−12) | 0.0006 / 0.001 | 0.001 / 0.0007 | 5e−5 / 3e−5 | 1e−17 | 2e−16 | 2e−17 | ≤ 4e−20 |
| Sudden trend reversal | 7e−8 / 0.004 | 0.002 / 0.005 | 5e−5 / 2e−4 | 9e−5 | 0.04 | 0.003 | ≤ 2e−7 |
| Volatility drops 1000× | 0.009 / **0.5** | **0.13 / 0.13** | 0.02 / 0.02 | 0.02 (**3.6 %** relative) | 0.07 | 0.005 | ≤ 1e−8 |
| Flat after a move (F-20) | **8 / 54** | **5.7 / 9.4** | **20–25** (ADX(2): 100) | 0.002 | 0.007 | 0.0004 | ≤ 5e−10 |

Also probed: ADX, DI, ATR and RSI with true ranges of `1e−13`, `1e−15` and `1e−17` on a `1e−5` price give ordinary
values (no zero clamp, no NaN); the extreme parameters (`ema:period=2`, `macd:fast=2,slow=3,signal=2`,
`macd:fast=99,slow=100,signal=50`, `bbands:period=2,stddev=1`, `adx:period=2`) give no NaN after the warm-up.

**Conclusions.** ADX behaves like RSI, not worse: small differences on ordinary data, about `0.1` points after a
1000× volatility collapse, and the F-20 split (`0` against a frozen value) on a flat run. No finding is specific to
the warm-up formulas: a longer warm-up shrinks the volatility-collapse case by `e^−1` per extra `period` and does
nothing for the flat case. **The formulas of 5.1 are kept.** Two consequences stay inside the planned scope:

1. The contract 6.3.6 amendment (HARD STOP 1) words the observation per data shape instead of S18's single "observed
   below 0.01 RSI points": RSI(100) already differs by `0.5` after a volatility collapse, on the S18 code.
2. C1 (5.6) becomes the probe above as committed observation tests (5.6).

### 5.3 Inputs: high, low and close of present slots

- The reader reads `open_time_ms, high, low, close` of the read range for every request (one query, same rows, same
  primary-key range as S18; designer's call: no per-indicator column choice). Each is converted with `float(text)`
  for the computation only, as the close is in S18.
- **A slot is an input only when it is `present`.** A conflicted slot keeps its stored candle in the database, but
  neither its close nor its high nor its low reaches any computation; that is S18's segment rule applied to three
  columns instead of one.
- The compute function receives one segment as three aligned sequences `(high, low, close)` and returns one sequence
  per output, as long as the segment. RSI, EMA, MACD and Bollinger Bands use only `close`. `evaluate` stays
  indifferent to what the values mean (it does not inspect them; finiteness stays the adapter's/formatter's job).
- ATR's true range and ADX's directional movement use the previous slot's values. Inside a segment the previous slot
  is always the previous present slot, so no value of a slot outside the segment is used. The first slot of a segment
  has no true range; TA-Lib's lookback covers it, and the warm-up covers the lookback.
- **No re-validation of stored candles.** The Collector validates OHLC before writing; the API does not check
  `low ≤ close ≤ high` again (designer's call; the candle invariants and the migration's `CHECK`s own this).

### 5.4 Reasons, segments, `latest`, numbers, errors

Unchanged from contract 6.3.2–6.3.7 and S18 5.2–5.7, for every indicator. In particular:

- `values` holds one entry per output in the output order of 5.1; `outputs` lists them. All outputs of one point have
  values together, or `values` is `null` with one reason.
- A NaN or infinity in **any** output at a point past warm-up is `500 internal_error`. Test per indicator that no NaN
  is reachable after the warm-up of 5.1 (TA-Lib's own lookbacks — `p` for RSI and ATR, `p − 1` for EMA, `s + g − 2`
  for MACD, `2p − 1` for ADX, `p − 1` for Bollinger — are all `≤ warmup_slots`).

### 5.5 Flat markets and F-20

ADX and the DI behave like RSI in F-20 (observed by the builder check on the locked build): when the **whole**
computation range from the anchor is flat (every high = low = previous close), ADX, both DI and ATR read `0`; when
the market turns flat **after moving**, the smoothed DM and TR shrink at the same rate, so the DI stay frozen (with
`p = 14` over 100 flat slots, `+DI` 13.338 and `−DI` 18.306) and ADX drifts towards a constant DX. One slot can
therefore read `0` or a frozen value depending on `start`. ATR `0` over a flat run is a true statement. This is
**characterized, not designed** (designer's call): tests pin both cases the way
`test_flat_range_after_movement_reads_zero_only_when_the_whole_range_is_flat` pins RSI, the contract keeps
"a flat market gives the value TA-Lib defines for it", and F-20 is extended to name ADX/DI. If the user decides F-20
as "null with a reason", that decision covers ADX/DI too. Bollinger Bands over a flat run have `upper = middle =
lower` (deviation 0, possibly TA-Lib's zero clamp of a tiny negative variance; pinned by a test).

### 5.6 Reproducibility and RSI stability

- S18 5.6 applies to every indicator: byte-identical `values` for the same effective window, id, rows of its read
  range and build. The rows of the read range now include high and low.
- **RSI byte-identical to S18:** the committed golden strings and the expected bodies and assertions of every
  existing endpoint test stay unchanged and pass. Test helpers and the domain/adapter test calls may be adapted to
  a new compute signature (section 8); what may not change is an expected value. A new endpoint test asserts that the same RSI request against a database whose candles differ only
  in high and low gives a byte-identical RSI (RSI does not read them).
- **Golden R3** gains one entry per new indicator with two parameter sets each (one short, one at the upper bound),
  computed on a committed generated OHLC fixture (high ≥ max(open, close), low ≤ min(open, close)).
- **Reference R4** per indicator: an independent pure-Python implementation in the tests (EMA with SMA seed, MACD
  from those EMAs, Wilder TR/ATR, Wilder +DM/−DM/DI/DX/ADX, SMA ± population deviation) agrees with the adapter on
  the same segments within a relative `10^−9` (absolute `10^−9` for values near zero). It catches a wrong TA-Lib
  argument order (high/low swapped, nbdevup vs nbdevdn) or slicing independently of the golden.
- **Convergence C1** per smoothed indicator (observation, not a guarantee, as S18): the data shapes of 5.2.1 (random
  walk, tiny TR, sudden trend reversal, 1000× volatility collapse) at the smallest and largest allowed parameters, the
  emitted value against a computation from the first slot. Tolerances are the measured values with a margin, stated
  **relative to the price level** for `ema`, `macd`, `atr` and in points for RSI, ADX and DI; they detect a
  regression (an anchor shorter than `warmup_slots`), they are not a contract. The flat-after-move shape is not a
  tolerance test: it is the F-20 characterization of 5.5.

### 5.7 Example of a mixed response (abbreviated, computed by nobody)

```json
{
  "id": "macd:fast=12,slow=26,signal=9", "name": "macd",
  "params": { "fast": 12, "slow": 26, "signal": 9 },
  "outputs": ["macd", "signal", "histogram"],
  "warmup_slots": 210, "anchor_open_time_ms": 1792580400000,
  "latest": { "open_time_ms": 1792673100000,
              "values": { "macd": "-12.40391", "signal": "-9.8812", "histogram": "-2.52271" }, "reason": null },
  "points": [ … ]
}
```

## 6. Constraints, gates and invariants

**Module boundaries** (unchanged pattern). Definitions, ranges, the MACD constraint, outputs and warm-up formulas
are pure domain code (`domain/indicators.py`); segments stay in `domain/indicator_series.py`; TA-Lib stays in
`api/indicator_engine.py` only (existing import-linter contract). The reader reads two more columns; no schema change.

**Request cost.** Read up to 3516 slots (one series, primary key). A request of ten three-output indicators makes
the body about three times S18's largest (about 60 000 formatted values, about 5 MB), and F-21 already measured the
S18 body build at 27–41 ms **on the event loop**. Designer's call (J7): S19 moves the body build and `json.dumps` of
`/v1/indicators` into the thread pool, the remedy F-21 names, because S19 is what triples it; the other endpoints are
not touched. `make bench-indicators` gains, besides the existing in-process phases:

- **A worst-case profile:** ten indicators at the largest warm-ups, mixed three-output ones (read range 3516 slots).
- **Concurrent requests:** `quasar-api serve` started as a subprocess on the generated database (local port, no
  network beyond loopback), then 1, 2, 4 and 8 concurrent worst-case requests through `httpx` (already a dev
  dependency). Per level: request latency min/median/max and the response size.
- **`/health` responsiveness:** while each level runs, a probe calls `GET /health` every 10 ms; its latency
  min/median/max and the number of probes above 100 ms. Every run measures the final implementation, with J7; no
  switch to turn J7 off is added to the code. The S18 figures in the README (body and JSON 27–41 ms on the event loop)
  are an orientation reference only, not a baseline measured by this benchmark.
- **CPU and memory where it is cheap:** the server subprocess's user and system CPU time and its peak RSS from
  `resource.getrusage(RUSAGE_CHILDREN)` after it exits, per run. Nothing finer (no profiler, no sampling).

The README records the numbers and the environment. No pass/fail criterion, no rate limiter, no new infrastructure
or dependency and no new HARD STOP, as in S18; the numbers are reported to the user with the implementation, and
F-21 is updated with what is now measured and what still is not (cold disk, other machines).

**Dependencies.** None new (TA-Lib and numpy from S18).

**Approval gates.**

1. **HARD STOP 1: contract section 6.3 amendment**, written from this design and approved by the user before any
   implementation task: the status line; 6.3.1 one row per indicator (parameters, ranges, outputs, `warmup_slots`)
   and the MACD constraint as a `400` cause; 6.3.3 the read bound `2016 + 1500`; 6.3.6 inputs (close, high and low),
   the warm-up criterion in general terms (a seed weight, not an accuracy bound), the observed differences per data
   shape instead of the single "below 0.01 RSI points" (5.2.1), Bollinger not an approximation but
   rounding-dependent, the flat-market sentence extended to ADX/DI; traceability row; decisions table "S19 decisions" J1–J7 below.
2. No other hard stop: no new dependency, no schema change.

The plan treats J1–J7 as approved together with the contract amendment. If the user changes a J value at HARD STOP 1,
the design and the plan are revised before any implementation task.

**Decisions to approve with the contract (designer's proposals):**

| # | Question | Proposal | Alternative not proposed, and its cost |
|---|---|---|---|
| J1 | Names and outputs? | `ema`, `macd` (`macd`, `signal`, `histogram`), `atr`, `adx` (`adx`, `plus_di`, `minus_di`), `bbands` (`upper`, `middle`, `lower`). | Long names (`bollinger`): readable, but diverge from TA-Lib names that consumers search for. |
| J2 | Ranges? | Table 5.1; MACD `fast < slow` or `400`. | Accept `slow < fast` and let TA-Lib swap: two ids for one indicator. |
| J3 | Warm-up? | One criterion (total seed weight over all paths `≤ e^−9`, RSI's), per-indicator integer formulas (ADX `13 × period`), verified for every allowed value; read bound `2016 + 1500`. | One factor `10 × longest period` for all: simpler to state, but EMA(200) and MACD would read 2000–2500 extra slots for no gain; or keep the bound at 1000 by shrinking ranges: no EMA(200). |
| J4 | Bollinger multiplier? | Integer `stddev` 1..5, both bands; population deviation, SMA middle. | Decimal parameter (`2.5`): needs a decimal grammar and normalization (`2.50` vs `2.5`) in the contract; can be added later without breaking. |
| J5 | ADX outputs? | `adx` with `plus_di` and `minus_di`. | ADX only: smaller body, but Core cannot tell trend direction without a second indicator name. |
| J6 | Flat markets (ADX/DI 0/0)? | Characterize TA-Lib's value, extend F-20; the F-20 decision applies to all indicators. | Decide F-20 now inside S19: widens S19 into a contract change of S18's reasons. |
| J7 | Event-loop cost of a bigger body? | Build and encode the `/v1/indicators` body in the thread pool. | Leave it (F-21): a 5 MB body built on the loop stalls `/health` for roughly 100 ms (estimate, unmeasured). |

**Invariants.** No new invariant: INV-058..062 are stated for "indicator" and hold unchanged. Their evidence gains the
S19 tests (W1, the per-indicator segment/warm-up/latest tests, R3, R4, the RSI-unchanged test), and INV-059's text
names the formulas of 5.1 instead of RSI's only. Their "Not covered" cells gain: equality with a whole-history value
for every smoothed indicator, with no bound in output units (W1 proves seed decay only, 5.2.1); Bollinger digits that
depend on the segment start.

## 7. Tests (summary for the plan)

| Area | Tests |
|---|---|
| Grammar | Each new id normalizes (`ema:period=0200` → `ema:period=200`); bounds inclusive for every key; each key missing, repeated, unknown; `macd` with `fast = slow` and `fast > slow` is `400`; mixed `rsi;ema;macd;atr;adx;bbands` in one request; the 10-indicator limit with three-output indicators. |
| Warm-up | W1 (criterion for every allowed value); C1 observation tests on the data shapes of 5.2.1; per indicator, the S18 segment scenarios A3, A4, A6, A8 with that indicator's `warmup_slots`; the read range of the largest warm-up (1500) is pinned. |
| Inputs | A conflicted slot's high/low never reaches ATR/ADX (spy on the adapter records three aligned sequences per call, none spanning a non-present slot); high/low changes leave RSI, EMA, MACD and Bollinger byte-identical. |
| Engine | R3 golden per indicator; R4 reference per indicator; no NaN after warm-up at the minimum parameters on a minimal segment; flat-run characterization for ADX/DI and Bollinger. |
| Endpoint | One mixed request: each indicator's values byte-identical to the same indicator requested alone (A10 generalized); `latest` for a three-output indicator under `due`/`stale`; the body is built off the event loop (J7). |
| Engine (extremes) | No NaN after warm-up at every extreme parameter of 5.2.1 and with true ranges down to `1e−17`. |
| Bench | `make bench-indicators`: worst-case profile, 1/2/4/8 concurrent requests, `/health` probe latency (final implementation with J7, no switch), CPU time and peak RSS; numbers in the README; the existing bench smoke test covers the new profile on a tiny database. |

## 8. Left to the builder

- The shape of the compute input (three sequences or one tuple per slot) and how `IndicatorDefinition` carries the
  MACD constraint, as long as the domain stays free of TA-Lib.
- The OHLC fixture generator and the exact C1 tolerances once measured.
- Whether the reference implementations share helpers (Wilder smoothing used by RSI, ATR and ADX).
- How the thread-pool move of J7 is tested (for example, an event-loop responsiveness test or a check that the body
  is built inside `run_in_threadpool`).
