# ADR-0001: Binance ingestion fidelity and closed-candle rules

- **Status:** Proposed (awaiting user approval; S3 starts only after approval)
- **Date:** 2026-10-22
- **Scope:** Binance USDT-M perpetuals, 5m candles, OHLCV + `turnover`.
- **Spec:** `docs/superpowers/specs/2026-10-20-market-data-v1-design.md` (§6.1–6.3, D4)
- **Plan:** `docs/superpowers/plans/2026-10-20-slices-s1-s4.md` (Tasks 3–4)

This ADR has three labelled parts. **Decisions** were given by the user and are quoted, not re-opened. **Proposals** are derived from the evidence and need approval. **Evidence** holds measured results with sample sizes and dates; nothing from the Decisions is presented as measured.

## Context

The service must store exact decimal OHLCV, persist only confirmed closed candles, never silently overwrite conflicting data and fail closed when data or time is untrusted. Four things were unknown before the spike: where exact decimals come from, when a candle may be treated as closed, what Binance's quote-asset volume means, and how venue symbols map to stored symbols. Scripts `spikes/capture_binance.py` and `spikes/analyze_binance.py` are **throwaway** (keeping them as product code is a new request). Fixtures are in `tests/fixtures/binance/`.

## Part 1 — Decisions (given by the user)

| ID | Decision |
|---|---|
| D-A | Canonical OHLCV and `turnover` come from the CCXT implicit raw endpoint (`fapiPublicGetKlines`). Unified `fetch_ohlcv` is used in the spike only for comparison. Both spike requests must be verified to target the same Binance USDT-M market, instrument and fixed time range. |
| D-B | Intended meaning of `turnover`: actual traded volume in the quote currency for the interval. Raw index 7 maps directly to `turnover` if documentation confirms it. `turnover` is never derived by computation. |
| D-C | Publication delay: 5 s as a configurable initial value. |
| D-D | Eligibility uses a conservative lower bound of server time: `open_time + timeframe + publication_delay <= server_time_lower_bound`; RTT, age and uncertainty are recorded and limited (configurable); untrusted time means no new candle is confirmed, re-synchronize, report degraded state. RTT, age and elapsed time use monotonic clocks only; the wall-clock offset is diagnostic. |
| D-E | A just-closed candle that is not yet available: retry and report coverage; never store the forming candle and never fabricate a substitute. |
| D-F | Recheck of the same fixed closed range after at least 1 h, compared by candle identity on canonical numeric values. Equality is partial evidence only. A difference never causes a silent overwrite. |
| D-G | Decimal text: explicit ASCII grammar, bounded exponent, limits on input length and plain-result size, no rounding. Unexpected formats are investigated, not accepted automatically. |
| D-H | `venue` identifies exchange and market segment. `symbol` is stored in an explicitly chosen format; `market_id` ↔ symbol mapping must be lossless. No narrow regex built from BTC and one cheap instrument. |

**Approved during S2 (2026-10-22, by the user):** index 7 maps to `turnover` on the basis of the evidence in E-6 (see limitation there); the extended boundary probe (10 boundaries, polls to 60 s) was requested after the first smoke test.

## Part 2 — Proposals (derived from evidence; need approval)

**P-1 Decimal source (a).** Persist the raw endpoint strings as canonical decimal text. Unified values are never used for storage. In the sample, unified output matched raw numerically (E-2), but **absence of differences in a sample is not evidence that float output is generally lossless**; persisting raw strings rests on exactness by construction.

**P-2 Field mapping (b).** Raw kline index → field: 0 `open_time` (ms), 1 open, 2 high, 3 low, 4 close, 5 volume (base asset, see limitation), 6 `closeTime` (ms, inclusive), 7 `turnover` (quote asset volume), 8 trades, 9–10 taker buy volumes, 11 unused. Only 0–5 and 7 enter v1.
*Limitation:* the official endpoint page for `/fapi/v1/klines` could not be retrieved (it is client-rendered). Index 5/7 labels come from the official `binance/binance-public-data` README (USD-M `/fapi/v1/klines` columns `Volume` and `Quote asset volume`) and the spot documentation; the base-asset unit of index 5 for futures is not stated in what was read. The `turnover/volume` sanity check (E-6) is supplementary, not proof. This is the weakest part of the ADR and is approved by the user on this basis; re-verify when the endpoint page becomes readable.

**P-3 Closed-candle eligibility (c).** A candle is eligible iff `open_time + timeframe + publication_delay <= server_lower_bound(m_now)`. `closeTime == open_time + timeframe − 1` held on every observed row (E-4), so the boundary is exclusive at `open_time + timeframe`; equality at the boundary is eligible.
**Amendment to D-C, needs approval:** the observed data does not support 5 s. A just-closed candle is present at 0.2 s but its values keep changing after the boundary: the 5 s poll differed from the final value in 1 of 13 boundaries, and the last observed change was between the 5 s and 8 s polls (E-5). Proposal: initial `publication_delay` of **15 s**, configurable. This is a margin over the largest observed settling time (8 s), **not a proven bound**: 13 boundaries on one instrument cannot show a tail. The soak test (S23) must re-measure it.

**P-4 Server-time lower bound (c).** Defined entirely on monotonic time. For an observation with monotonic send time `m0`, receive time `m1` and server reading `S` (ms), at later `m_now`:
`age = m_now − m1`, `server_lower_bound(m_now) = S + age × (1 − max_drift_ppm / 1e6) − precision_margin_ms`.
Assumptions: (A1) `S` is the server clock at some instant between `m0` and `m1`, so true server time at `m1` is at least `S`; (A2) the monotonic clock does not jump; (A3) relative rate error is at most `max_drift_ppm` (covered by the drift reserve); (A4) `precision_margin_ms >= 0` covers clock resolution and unmodelled error. W-6 (server time never regresses) is checked separately.
**Reject the observation (time untrusted)** when: no observation yet or the request failed; `rtt_ms > max_rtt_ms`; `age > max_age_ms`; `uncertainty_ms > max_uncertainty_ms` (`uncertainty_ms = rtt_ms`); `S` is smaller than the previous accepted `S`; or `|wall_elapsed − monotonic_elapsed| > max_clock_divergence_ms` since the observation (a rejection gate only, never part of the bound). While untrusted: no new candle is confirmed, re-sync is retried with bounded backoff, a degraded state is reported, stored candles are unaffected.
Proposed initial values (all configurable, justified by E-4): `max_rtt_ms = 2000` (observed max 760), `max_uncertainty_ms = 2000`, `max_age_ms = 60000`, `max_drift_ppm = 500` (observed ±148 ppm over ≥ 30 min spans with a 200–524 ppm measurement error bound, so 500 is not tighter than what the data can resolve), `precision_margin_ms = 50`, `max_clock_divergence_ms = 100` (observed 5.4 ms for continuous pairs; 376 s across a suspend).
The system-suspend case occurred during the spike (E-4): the monotonic clock stopped while the wall clock advanced. Under P-4 the divergence gate rejects such an observation, and the bound would only err on the low (conservative) side.

**P-5 Missing just-closed candle (d).** Retry and report coverage. Never store the forming candle or a substitute (D-E). The probe never saw a missing row after 0.2 s (E-5), which does not establish that it cannot happen.

**P-6 Temporal eligibility is not immutability (e).** Eligibility says only that the candle is old enough to treat as closed. Binance values for a closed candle were observed to change up to about 8 s after close (E-5); no guarantee is established that history is never revised later (the recheck found 0 differences after 67 min, partial evidence only, E-7). Any later difference feeds S8 conflict handling and is never silently overwritten.

**P-7 Decimal grammar and limits (f).** All 2424 observed raw strings are plain decimals matching `^(0|[1-9][0-9]*)(\.[0-9]+)?$` (no exponent, no `+`, no leading zeros), max length 15 and max 14 digits, max 7 fractional digits (E-3). The Task 6 limits (input ≤ 128, mantissa ≤ 96 digits, |exponent| ≤ 32, result ≤ 96 digits) fit with large margin; W-2 holds for this sample. 2041 of 2424 strings carry trailing fractional zeros as padding (e.g. `0.2344000`, `81619.00`), so canonicalization must define the stored form of equal values: proposal is to remove insignificant fractional trailing zeros while preserving integer digits (`"1000.00"` → `"1000"`), no rounding. The grammar is not extended.

**P-8 Venue and symbol (g).** Venue `binance-usdm` (`<exchange>-<segment>`). Stored symbol is the CCXT unified symbol (`BASE/USDT:USDT`). The survey shows a bijection between 882 unified symbols and 882 market ids, and every id equals `BASE + "USDT"`.
**Contradicts W-4:** the survey contains non-ASCII symbols (`币安人生/USDT:USDT`, `哈基米/USDT:USDT`, …), so "printable ASCII" is false. Proposal: stored symbol is a non-empty string of 1–64 characters with no whitespace or control characters (Unicode allowed); max observed length 23. A single check showed Binance accepts a non-ASCII `symbol` in a `/fapi/v1/klines` query (HTTP 200, one row returned, 2026-10-22). Tasks 6–7 must use this rule instead of ASCII.

**P-9 Endpoint scope (h).** The implicit raw endpoint works and no fallback is needed, so direct HTTP is not proposed (it would need a new ADR). Binance's changelog states `/fapi/v1/klines` now also accepts COIN-M symbols (CM-UM integration, effective 2026-06-30). Mitigation already in the fixture tests: requests must target `/fapi/`, carry the expected `symbol`, and the market flags must show `swap`, `linear`, `settle == USDT`.

## Consequences

- If P-3 is accepted, confirmation of a candle is delayed by 15 s instead of 5 s; for 5m candles this is small.
- Task 6–7 inputs change: canonical zero-stripping (P-7) and the broad symbol rule (P-8). Tasks 6–7 are updated before S3 starts.
- S8 must handle the case where Binance revises a closed candle after it was stored.
- The `turnover` mapping rests on README column labels plus a sanity check (P-2); re-verify against the endpoint page when available.
- Open measurement debt: one instrument, 13 boundaries, one time of day, one machine and network; S23 soak test must re-measure settling time, RTT and drift.

## Part 3 — Evidence

Measured 2026-10-22 (Europe/Prague) with CCXT 4.5.89, Python 3.12.15, from one macOS machine. Sample sizes: 2 instruments × 100 closed 5m rows (BTC/USDT:USDT, 0G/USDT:USDT), 2 forming rows, 100-row recheck, 13 boundary probes (3 smoke + 10 extended), 139 time observations, 882 surveyed markets.

- **E-1 Documentation read:** `binance/binance-public-data` README (raw.githubusercontent.com, 2026-10-22): USD-M `/fapi/v1/klines` columns `Open time, Open, High, Low, Close, Volume, Close time, Quote asset volume, Number of trades, Taker buy base asset volume, Taker buy quote asset volume, Ignore`. Binance developer docs `llms-full.txt` (2026-10-22): `GET /fapi/v1/klines` "Klines are uniquely identified by their open time"; changelog note C.3 on UM/CM symbols. The endpoint's own detail page was not retrievable.
- **E-2 … E-8:** the complete output of `spikes/analyze_binance.py` follows, unedited. Item numbers equal the evidence numbers (E-2 = item 2, …); item 1 is the identity check. Item 5 pools both probes.

```text

=== 1. Identity, market, completeness (raw vs unified) ===
btcusdt (BTC/USDT:USDT, id=BTCUSDT): rows raw=100 unified=100 same_open_times=True contiguous=True max_relative_diff=0.000E+3
smallprice (0G/USDT:USDT, id=0GUSDT): rows raw=100 unified=100 same_open_times=True contiguous=True max_relative_diff=0.000E+3

=== 2. Numeric loss: Decimal(raw) vs Decimal(str(unified)) (comparison only) ===
btcusdt: values=500 identical_text=93 formatting_only=407 numeric_differences=0
smallprice: values=500 identical_text=0 formatting_only=500 numeric_differences=0

=== 3. Observed text forms in raw numeric strings (indices 1-5, 7) ===
sample size: 2424 strings
with exponent: 0
leading '+': 0
leading zeros (e.g. '00'): 0
max length: 15
max digit count: 14
max fractional digits: 7
outside plain-decimal grammar or > 128 chars: 0 []
mantissa digits > 96: 0
trailing-zero padded (fraction ends in '0'): 2041

=== 4. Time semantics ===
btcusdt: closeTime - (open_time + 300000) distinct values over 100 rows: [-1]
smallprice: closeTime - (open_time + 300000) distinct values over 100 rows: [-1]
forming: last row closeTime=1792677299999 server_time=1792677035725 closeTime>=server_time=True; previous row closed=True
all captures: n=139 rtt_ms min/median/max=264.8/344.0/759.9 offset_estimate_ms (wall, diagnostic) min/median/max=-241.0/-13.0/93.5
server time regressions between successive observations (by monotonic order): 0
pairs >= 60 s apart: 8962; wall-vs-monotonic divergence > 1000 ms: 3024
   divergence of rejected pairs (ms) min/max: 376274/376281
   consistent with a system suspend: the monotonic clock stopped while wall clock and server time advanced (inferred, not observed directly)
accepted pairs: n=5938 divergence ms min/max=-4.3/5.4
   rate difference ppm: min=-1753 p05=-217 median=+0 p95=+245 max=+1831
   pairs >= 30 min apart: n=1015 rate difference ppm min/max=-124/+148; measurement error bound ppm min/max=200/524
   all accepted: largest measurement error bound = 6228 ppm (shorter spans are bounded by RTT)

=== 5. Boundary probes (smoke tests) ===
btcusdt_5m_boundary_probe: boundaries=3 polls/boundary=7 last offset=13 s
   boundary 1792677300000: first present at 0.2 s, distinct values=4, changes between consecutive polls=4, value equals final from poll at 5 s
   boundary 1792677600000: first present at 0.2 s, distinct values=3, changes between consecutive polls=4, value equals final from poll at 8 s
   boundary 1792677900000: first present at 0.2 s, distinct values=2, changes between consecutive polls=1, value equals final from poll at 3 s
btcusdt_5m_boundary_probe: n=21 rtt_ms min/median/max=264.8/304.9/703.3 offset_estimate_ms (wall, diagnostic) min/median/max=-180.5/0.0/93.5
btcusdt_5m_boundary_probe_extended: boundaries=10 polls/boundary=11 last offset=60 s
   boundary 1792678800000: first present at 0.2 s, distinct values=4, changes between consecutive polls=4, value equals final from poll at 5 s
   boundary 1792679100000: first present at 0.2 s, distinct values=3, changes between consecutive polls=2, value equals final from poll at 3 s
   boundary 1792679400000: first present at 0.2 s, distinct values=2, changes between consecutive polls=1, value equals final from poll at 1 s
   boundary 1792679700000: first present at 0.2 s, distinct values=3, changes between consecutive polls=3, value equals final from poll at 3 s
   boundary 1792680000000: first present at 0.2 s, distinct values=3, changes between consecutive polls=2, value equals final from poll at 2 s
   boundary 1792680300000: first present at 0.2 s, distinct values=2, changes between consecutive polls=1, value equals final from poll at 5 s
   boundary 1792680600000: first present at 0.2 s, distinct values=3, changes between consecutive polls=3, value equals final from poll at 3 s
   boundary 1792680900000: first present at 0.2 s, distinct values=3, changes between consecutive polls=2, value equals final from poll at 3 s
   boundary 1792681200000: first present at 0.2 s, distinct values=3, changes between consecutive polls=2, value equals final from poll at 3 s
   boundary 1792681500000: first present at 0.2 s, distinct values=2, changes between consecutive polls=1, value equals final from poll at 3 s
btcusdt_5m_boundary_probe_extended: n=110 rtt_ms min/median/max=265.7/346.0/577.2 offset_estimate_ms (wall, diagnostic) min/median/max=-136.0/-15.2/81.5

settled-from offset over 13 boundaries (sorted): [1, 2, 3, 3, 3, 3, 3, 3, 3, 5, 5, 5, 8]
   boundaries not yet settled at the 3 s poll: 4/13
   boundaries not yet settled at the 5 s poll: 1/13
   boundaries not yet settled at the 8 s poll: 0/13
   boundaries not yet settled at the 13 s poll: 0/13
   boundaries not yet settled at the 20 s poll: 0/13

=== 6. Turnover/volume sanity (supplementary, not proof of semantics) ===
btcusdt: non-zero-volume rows checked=100, outside [low-tick, high+tick]=0
smallprice: non-zero-volume rows checked=100, outside [low-tick, high+tick]=0

=== 7. Recheck: first vs recheck capture (btcusdt, canonical numeric values) ===
rows compared: 100/100, seconds since first capture: 4044
numeric differences: 0 []

=== 8. Market survey (USDT-M perpetuals) ===
captured_at_wall_ms=1792677032539 ccxt=4.5.89
markets=882 unique_symbols=882 unique_ids=882 bijection=True
excluded_counts: {'settle_not_usdt': 45, 'not_swap': 4}
inactive: 134
symbol character set: /0123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ了人哈基安币我来牛生米虾踏马龙 | max symbol length: 23
id character set: 0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ了人哈基安币我来牛生米虾踏马龙 | max id length: 17
symbols not matching BASE/USDT:USDT: 0 []
non-ASCII or non-printable: ['哈基米/USDT:USDT', '币安人生/USDT:USDT', '我踏马来了/USDT:USDT', '牛来/USDT:USDT', '龙虾/USDT:USDT', '哈基米USDT', '币安人生USDT', '我踏马来了USDT', '牛来USDT', '龙虾USDT']
id != BASE+'USDT' for 0 markets: []
```
