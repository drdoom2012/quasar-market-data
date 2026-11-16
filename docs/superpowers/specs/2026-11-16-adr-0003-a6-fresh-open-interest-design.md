# ADR-0003 Amendment A-6 (proposal): shared retry reserve and an open interest symbol list

- **Status:** proposal, revision 3. Revision 2 applied an independent review. Revision 3 (2026-11-16) applies the
  user's decisions: D-1, D-3, D-4 and D-5 approved as proposed; D-2 revised (retired-series masking resolved before the
  first production release, additively, section 2.4); latency validated under reserve exhaustion and limiter
  contention, measured and estimated figures separated (section 2.6); an additive API field for the open interest
  universe (section 2.7); cadence tests over every budget 100..400; atomic reserve accounting. Constraints the user
  restated: the 40 % budget cap (60 % of the IP limit left to others), the backfill guarantee, deterministic
  configuration and the existing schema stay. Revision 4 (2026-11-16) applies an independent review of revision 3 and
  of the plan. Failure duration is now in the latency model, and slow failures, not the reserve, decide R1 under
  errors. A new pace rule (D-7) stops a slow round's pace from propagating through the limiter. The missing cases
  (untrusted time, `429`, writer wait) are listed. The user decided D-5 (API field), D-6 (no timeout change, measure) and
D-7 (adopt) on 2026-11-16; no decision is pending. Nothing is implemented;
  the implementation plan is
  `docs/superpowers/plans/2026-11-16-adr-0003-a6-fresh-open-interest.md`.
- **Date:** 2026-11-16.
- **Trigger:** change request `docs/requests/2026-11-16-quasar-fresh-open-interest.md` from Sentaris Engine (R1: cadence
  `c = 1`, a row stored by `T + 600 s` in an ideal run; R2: keep `rule.cadence` and `rule.expected_after_ms`).
- **User decisions so far (2026-11-16):** nothing else on the Collector's IP calls `/futures/data/*`; the chosen way
  is option (a) of the request (a shared retry reserve) **plus an explicitly configured symbol subset**. The budget
  share of O-2 (at most 400, 40 %) does **not** change.
- **Affects:** ADR-0003 P-10 (cadence formula, allocation table, planned latency), P-12 (backfill share figures),
  P-13 (new configuration key, largest `S`), P-15 (tests), Consequences, O-5; `docs/invariants.md`; README (the
  `[open_interest]` example, the `oi-round` log line, the overview section); contract `docs/contracts/api-v1.md`
  sections 6.4.2, 6.5.7 and 6.5.8 (two additive fields; the value of `configured` changes, section 3); both shipped
  configuration files; the API wiring of `configured`; the symbol overview tool (section 2.5).
- **Does not affect:** the schema (no migration), the row identity, eligibility, slot states, `readable_by_ms`, the
  endpoints and their fields, the candle path, funding rate (its capacity stays on the venue symbols),
  `ExtensionPause`, the budget range `[100, 400]`, `fetch-oi-once` (it takes `--symbol` and never reads the list; it
  can create series outside the list, as it can today).

## 1. Why the current rules cannot meet R1

1. **Budget.** Today `c(S, b)` is the smallest `c` with `2 × ceil(S / c) + 2 ≤ floor(3b / 4)`: one request **and one
   reserved retry per symbol**. At `b = 400` that allows `c = 1` up to `S = 149`. The shipped files configure 522
   symbols, so `c = 4` (`expected_after_ms = 1 400 000`). Measured on the local Collector (`data/quasar.db`, last
   2 days, 522 series): 12 % of rows were stored within 600 s of their period end.
2. **522 at `c = 1` is out of reach at 40 %.** The rounds alone need 522 starts per 5 min (52 % of the documented
   1 000). No split of `b ≤ 400` reaches it; only raising O-2 would, which the user did not choose.
3. **The planned latency ignores the round's own duration.** P-10 says "request and write time are ignored". A
   symbol's row is stored when its request finishes, and symbols are asked in sorted order, so the same symbols are
   always at the end of a round. Measured on the local Collector (`c = 4`, rounds of 130–131 symbols, 78 rounds in
   the last 24 h):
   - the first row of a round is typically written 0.7–1.3 s after the wake time;
   - the rows of one round span **21.4–30.8 s** (mean 24.5 s), about 4.2–6 symbols per second with
     `MAX_IN_FLIGHT = 2`;
   - two rounds started late: one by 29.5 s (a host stall; the candle round was late by about 25 s too) and one by
     80.6 s (the first round after a restart);
   - about 1.8 % of rows were not yet published when their symbol was asked and took the next request.

   Linear extrapolation (an estimate, not a measurement): a round of `n` symbols ends about `n / 4.2` s after the wake
   at worst; 59 s for 248, 48 s for 200, 124 s for 522.

   R1 needs the row of period end `e` stored by `e + 600 s` (the consumer evaluates 20 s past a boundary, which is
   extra slack, not counted here). A row becomes readable 2 s after its stamp (`READABLE_BOUND_MS`,
   `domain/readability.py`). With `c = 1` a row published **after** its symbol's request is stored by the next round
   at `e + 500 s + p + 2 s`, where `p` is the symbol's position in the round. At `n = 522` that is about
   `e + 626 s`: too late even with an unlimited budget. A smaller round is therefore needed for the timing, not only
   for the budget.

## 2. Proposal

### 2.1 Shared retry reserve (P-10)

Replace the per-symbol retry reserve by a shared one of at most `R = 50` retries per round:

```
reserve(n) = min(R, n)                with n = ceil(S / c), R = 50
round_cost(S, c) = n + reserve(n) + 2
c(S, b) = the smallest c >= 1 with round_cost(S, c) <= steady(b),  steady(b) = floor(3b / 4) (unchanged)
```

- **Never worse than today.** `n + min(50, n) + 2 ≤ 2n + 2`, so for every `(S, b)` the new `c` is at most the old
  one, every configuration accepted today stays accepted, and `c` changes only where `n > 50`.
- **Round rule.** A symbol whose request fails gets its one retry (same `RETRY_DELAY_MS`, same deadline rule) only
  if it obtains a **retry ticket** from the round; the round hands out at most `reserve(n)` tickets. Otherwise it is
  not retried and waits for its next request (`c` periods later; with `c = 1` the overlap
  `[B − 10 min, B]` still contains `B` in the next two rounds) or the backfill. What uses the reserve is exactly what
  uses a retry today: a request error, `SourceError`, a parse error and a write failure including `busy`
  (`_write_problem`). A `429` pause, a refusal and an absent row use none.
- **Atomic accounting.** The tickets are a counter owned by one round's state (`_RoundState`), never shared between
  rounds or tasks. A ticket is taken by one synchronous call, `try_take_retry() -> bool`, that checks and increments
  with no `await` between the two; it runs inside the attempt's in-flight slot, after the pause and stop checks and
  before the request. The round runs on one event loop (`asyncio.run` of the open interest task), so concurrent
  symbol coroutines cannot interleave inside the call and the count can never exceed `reserve(n)`. A ticket is
  **never returned**: a retry cancelled at the deadline after taking its ticket, but before its request starts in the
  limiter, still counts. That errs towards fewer retries, never more, so `starts ≤ round_cost` holds without a race.
  Any later change that moves the round onto threads must replace the counter with a lock; a test pins the
  single-loop assumption (section 5).
- **Reporting.** A symbol left without a retry ends with a new `OpenInterestSymbolOutcome` value
  `retry_reserve_exhausted`; the `oi-round` log line gains its count (README documents the line). In a partial
  outage the reserve favours symbols early in sorted order; that is accepted and stated.
- **Backfill.** The share stays `b − round_cost(S, c)`, at least `b − steady(b)` (≥ 25) in every accepted case, so
  P-12's guarantee holds. Backfill-only-from-leftovers (option (b) of the request) is **not** proposed. The backfill
  gate (`backfill_may_start`) now leaves `reserve(n) + 2` headroom for the next round instead of `n + 2`. When the
  previous round used its whole reserve, the next round's starts are paced by the previous round's start times; the
  tests cover this (section 5).
- **Constant, not configured.** `R = 50` is a domain constant beside `MAX_CADENCE`, so the Collector and the API keep
  deriving the same `c` from the same file. Rationale: retries follow request errors only, which are rare in an
  ideal run; under a broad outage 50 retries in 5 minutes do not help, and the overlap fetches the rows again.
- **Largest accepted `S`.** `CadenceError` names `12 × (steady − 52)` when `steady ≥ 102` (`n ≥ 50` at `c = 12`),
  otherwise `12 × ((steady − 2) // 2)` as today: 2 976 at 400, 1 176 at 200, 432 at 100. P-13's "at most 1 788
  symbols at 400" becomes 2 976.

Resulting cadence (new rule; today's in brackets):

| `b` | `c = 1` up to `S` | Max `S` (`c = 12`) | `S = 200` | `S = 522` | `S = 600` |
|---|---|---|---|---|---|
| 400 | 248 (149) | 2 976 (1 788) | `c = 1`, backfill 148 (`c = 2`) | `c = 3`, backfill 174 (`c = 4`) | `c = 3`, backfill 148 (`c = 5`) |
| 200 | 98 (74) | 1 176 (888) | `c = 3`, backfill 81 (`c = 3`) | `c = 6`, backfill 61 (`c = 8`) | `c = 7`, backfill 62 (`c = 9`) |
| 100 | 36 (36) | 432 (432) | `c = 6`, backfill 30 (`c = 6`) | refused (refused) | refused (refused) |

(Checked by two independent throwaway scripts over `S = 1..3999`; the implementation's tests re-derive it. The new
`c` is never above the old one for `S = 1..2999` and `b` in `{100, 200, 400}`.) P-12's catch-up bound for 600
symbols changes from "5.4 hours at the 158 share" to about 5.7 hours at the 148 share.

**Simpler alternative, not proposed:** a subset of at most 149 symbols needs no P-10 change. Only `S` from 150 to
248 needs the shared reserve. It is proposed because it costs a counter in the round and gives the operator room;
if the user prefers no P-10 change, 2.1 is dropped and the subset is capped at 149.

### 2.2 Open interest symbol list (P-13)

`[open_interest]` gains the key `symbols` (form: decision D-1):

```toml
[open_interest]
symbols = "all"                      # or a non-empty list, e.g. ["BTC/USDT:USDT", "ETH/USDT:USDT"]
```

- `"all"`: every symbol of the venue, today's behaviour. A list: exactly those symbols.
- Validation at start (config error, exit code 2, the existing S20 style naming the key): the value is the string
  `"all"` or a non-empty list of strings; no duplicates; every listed symbol is a symbol of the configured venue
  (exact, case-sensitive match), because open interest is never collected for an instrument without candles. Any
  other value (another string, an empty list, a number) is an error. Checked also when `enabled = false`, as the
  cadence already is today (`settings.py`) and as A-4 does for `backfill_days`. Consequence: removing a symbol from
  the venue list requires removing it from this list too, or the processes do not start.
- `S` in P-10 becomes **the number of open interest symbols** (`len(symbols)`, or the venue's count for `"all"`).
  Change points: the Collector's `_open_interest_run` (`collector/cli.py`, today `venue.symbols`), the API's
  `rule_from_settings` (`api/open_interest_rule.py`); coverage and health take `S` only through the rule.
- The list is stated in the file, never derived at run time (the request's condition; also determinism of P-10/P-12).
  Its order does not matter (`symbols_for_round` sorts).
- P-13's sentence "there is no second symbol list" is replaced by this rule.

### 2.3 Planned latency (P-10 Timing)

The definition gains the round position `p` (time from the round's wake to the symbol's stored row) instead of
"request and write time are ignored":

- publication within `round_offset_ms` and before the symbol's request: `(c − 1) × 5 min + 200 s + p`;
- later: `c × 5 min + 200 s + p`.

The row is readable 2 s after it is stored. `p` is bounded by the round deadline (285 s after the wake). An **ideal
run** now also assumes the round starts at its wake time; the two late starts of section 1 are outside it. In an
ideal run `p` is about `n / 4.2` s at worst (measurement of section 1, point 3). No latency is guaranteed, as today.

`expected_after_ms` stays `c × 300 000 + round_offset_ms` (contract field unchanged). It separates
`not_yet_expected` from `due`; `lag_threshold_ms` absorbs `p` **only while `p ≤ lag_threshold_ms`** (60 000 in both
shipped files). At `S = 248`, `p ≈ 59 s` sits on that edge, so a late-published row would briefly count as `stale` in
health. This is a second reason to keep `S` well below 248.

For the proposed shipped subset (`S = 200`, decision D-3): `c = 1`, `p ≈ 48 s`. Rows published within 200 s (about
98 % in the measurement) are stored by about `e + 250 s`; late-published rows by about `e + 550 s`, 50 s inside R1.
A round that starts 30 s late still fits (`p ≈ 78 s`, `e + 580 s`); the 80 s start after a restart does not
(`e + 630 s`), and those rows fail closed in the consumer (`series_stale`).

### 2.4 Symbols removed from collection, and health

Series of symbols that leave the list stay stored (data is never deleted). They are no longer requested, the backfill
plans holes only for listed symbols (P-12 "per configured series"), and:

- `GET /v1/open-interest` and `/coverage` keep answering `200` for them (rows stored); slots after their newest row
  become `due`, then `stale`, judged with the list's `rule`. That is honest: nobody collects them.
- `/health/ingest` `open_interest` counts them (`scope: "stored_series"`) and gives them `configured: false`. Their
  `lag_slots` grows without limit, and `behind` is sorted stale first, then by `lag_slots` descending, at most 50
  entries. **With many retired series this hides real problems:** the 50 listed entries are all retired, a configured
  series that falls behind is never listed, `stale` and `due` mix retired and configured series, and `status` stays
  `behind`. That is the existing contract rule for retired series (6.5.8), the same as for candles; at this scale it
  defeats the purpose of the object.
- **Resolution (D-2, revised by the user): an additive `collected` object** inside `/health/ingest`
  `open_interest`, shipped in A-6, so the first production release already has it. Every existing field keeps its
  meaning (`scope: "stored_series"`, the counts, `status`, `behind`). With `symbols = "all"` an A-6 build gives the
  same values for them as a pre-A-6 build reading the same file without the key (a pre-A-6 build refuses the key).
  `configured` changes only with a list (section 3). No stored row is touched.

  ```json
  "collected": {
    "scope": "open_interest_symbols",
    "symbol_count": 200,
    "never_stored": 0,
    "status": "current",
    "series_count": 200, "current": 200, "due": 0, "stale": 0, "unjudged_series": 0,
    "conflicted_series": 0,
    "behind": [],
    "truncated": false
  }
  ```

  - It judges exactly the series `(venue, symbol, configured period)` whose `(venue, symbol)` is an open interest
    symbol of the file, with the same per-series rule, the same `status` precedence, the same `behind` order and the
    same limit of 50 as the outer object. A retired series is never in it, so it cannot fill `behind` or move
    `status`. `conflicted_series` counts only these series.
  - `symbol_count` is the number of open interest symbols of the file; `never_stored` those of them with no stored
    row, the open interest counterpart of the candle `universe` block (they are not in `series_count`).
    `collected.series_count = current + due + stale + unjudged_series` over the listed series of the configured
    period (a listed series is unjudged only when its newest `time_ms` is unaligned). `series_count + never_stored =
    symbol_count` always holds, because each listed symbol has at most one series of the configured period. A
    series of another period of a listed symbol is not in `collected` (it stays counted as `unjudged_series` in the
    outer object, as today), and that symbol counts as `never_stored` unless it also has a configured-period series.
  - With `"all"`, `collected` equals the outer counts only when every stored series has the configured venue, a
    venue symbol and the configured period; otherwise the difference is exactly those series.
  - `status` adds no new value. `no_series` when `series_count` is 0, then the outer precedence. `never_stored` does
    not change `status`, exactly as the candle `universe` does not change candle status; a consumer that needs
    "every listed symbol stored" reads `never_stored`.
  - Computed in the same read transaction and at the same `as_of_ms` as the outer object, from the same facts (no
    second database read). The key is present whenever `open_interest` is an object.
- The outer fields keep masking after a list shrinks. That is now documented in 6.5.8 with a pointer to `collected`,
  and is the honest reading of "every stored series".

### 2.5 Symbol overview tool

`tools/symbol_overview` asks open interest coverage for every candle instrument. With a list of 200, the 322 unlisted
symbols answer `404 unknown_series`, which the tool shows as `no_data`, its worst state; symbols with rows that left
the list show as `stale`, forever. The approved D-5 was "read the file". Revision 3 adds the API field of 2.7, and the
tool today takes **no** configuration file (it reads only the API, `tools/symbol_overview/fetch.py`). So the plan
uses the API field: the tool reads `open_interest_period` from `/v1/instruments`, which it already fetches, and shows
`null` symbols as "not collected" without asking open interest coverage for them. This replaces D-5's mechanism
(same outcome, no new tool option, one source of truth with the API). **It needs the user's confirmation.** If the
user keeps the file, the tool gains a `--config` option instead.

### 2.6 R1 latency under retry reserve exhaustion and limiter contention

Scope: the shipped subset, `S = 200`, `b = 400`, so `c = 1`, `n = 200`, `reserve = 50`, `round_cost = 252`, backfill
share 148. R1 bound: row of period end `e` readable by `e + 600 s`. "Readable" is the stamp plus 2 s.

**Measured** (local Collector `data/quasar.db`, `c = 4`, `n ≈ 131`, 78 rounds in 24 h, 2026-11-15/16; not at
`n = 200`, not at `c = 1`):

| Quantity | Value |
|---|---|
| First row after the wake | typically 0.7–1.3 s |
| Span of one round's rows | 21.4–30.8 s, mean 24.5 s (≈ 4.2–6 symbols/s) |
| Late round starts | +29.5 s (host stall), +80.6 s (first round after a restart) |
| Rows not yet published at their request | ≈ 1.8 % |
| Publication delay (E-5, 18 rows) | 29.4–186.8 s |

**Estimated** (derived from the measured rate by linear extrapolation and from the code; not measured). Model: a
successful request holds an in-flight slot for 0.48 s (the measured 4.2 symbols/s at 2 slots); a failed request holds
it for `f`. Every attempt, retry included, runs inside the round's semaphore. The adapter sets no CCXT timeout, so a
timed-out request holds its slot for CCXT's default `f = 10 s` (`ccxt.async_support.binanceusdm().timeout = 10000`).
A connection refusal or an HTTP error answer is fast (`f ≈ 0.5 s`). With `F` failing first attempts, `r = min(F, 50)`
retries, all slow in the worst case, the last symbol's position is about

`p(F, f) ≈ ((n − F) × 0.48 s + (F + r) × f) / 2`, capped by the round deadline (285 s after the wake).

There are two paths. A row published **before** its symbol's request in round `B` is readable by `e + 200 s + p + 2 s`.
A row published **after** it (≈ 1.8 % measured) is readable by `e + 500 s + p' + 2 s`, where `p'` is the position in
the next round. R1 therefore needs `p ≤ 396 s` on the first path, which always holds within the deadline, and
`p' ≤ 98 s` on the second.

| Scenario | Stored-and-readable by | R1 |
|---|---|---|
| Ideal, published before the request (`p ≤ 48 s`) | ≤ `e + 250 s` | met |
| Ideal, published after the request | ≤ `e + 550 s` | met |
| `F` fast failures (`f ≈ 0.5 s`), any `F ≤ 200`, reserve exhausted or not | `p ≤ 63 s`: ≤ `e + 265 s` / `e + 565 s` | met |
| Slow failures in the round that serves the late path, `f = 10 s` (default timeout) | met while `F ≤ 5` (`p' ≈ 97 s`); `F = 10` gives `p' ≈ 146 s`, `e + 648 s` | **missed** for late-published rows of symbols after the failures in sorted order; early rows met |
| Same with `f = 3 s` (only if D-6 sets a timeout) | met while `F ≤ 18` | missed above |
| Slow failures, `F ≥ 60` at `f = 10 s` | the round reaches its deadline; unfinished symbols end `deadline`, their rows come from the next round (`e + 500 s + p'`) | early rows met if the next round is healthy; late rows missed |
| Reserve exhausted, the unretried symbol's next request succeeds | its row comes from the next round (overlap `[B − 10 min, B]`): `e + 500 s + p'` | met while `p' ≤ 98 s` |
| Exhausted, and the next request also fails | `e + 800 s + p` | **missed**, fail closed (`series_stale`) |
| Limiter contention: backfill saturated, previous round on schedule **and on pace** (A-6 rule below) | our k-th start waits at most until the previous round's k-th start leaves the window: `p' ≤ 60 s + 5 s` | met (≤ `e + 567 s`) |
| Previous round late (> 5 s) or slow (span above the pace limit) | the backfill gate counts that round in full and keeps `round_cost` free, so there is no contention; a late start adds its own delay | met up to a late start of about 50 s (38 s with a full reserve of fast retries); the observed 30 s stall fits, the 80.6 s start after a restart misses |
| The round's server-time call fails (round `untrusted`, nothing stored) | rows come from the next round | early rows met (`e + 550 s`); late rows missed |
| A `429` pause (≥ 300 s, shared with funding) | at least one round lost | **missed** for the pause; fail closed |
| An open interest write waiting for candle writes while it holds its slot (`async_open_interest_writer.py`, P-9) | bounded only by the round deadline (e.g. candle backfill after a restart) | **not bounded** by A-6; missed when the wait is long |

Health side effect: `expected_after_ms + lag_threshold_ms = 560 s`. A late-path row readable after `e + 560 s` (for
example `e + 562 s` with retries) shows its series as `stale` in `/health/ingest` for those seconds. That is a
correct reading of the rule, not an error.

**A-6 pacing rule (correction found by the second review).** Today the backfill gate leaves out the previous round's
starts when that round **began** on schedule (`_previous_round_if_on_schedule`, which checks only the span's first
start, with a 5 s tolerance). Suppose the previous round began on time but its starts were spread out, for example
by timeouts, and the following idle window then filled the backfill share. The window is full at the wake, and our
k-th start must wait for the previous round's k-th start to expire. Our round then copies the previous round's slow
pace on a healthy network. The next round copies ours in turn, for as long as the catch-up keeps the backfill
saturated. A-6 therefore counts the previous round in full also when its span exceeds the **pace limit**. The span
is `_last_round_span`, from the round's start to its end, as recorded today. The pace limit is `pace_limit_ms(n) = ceil(n / MAX_IN_FLIGHT) × 600 ms`. That is 60 s at `n = 200` and 39.6 s at
`n = 131`, against the measured maximum of 30.8 s. This extends the existing exception for a late previous round to a
slow one. **Backfill effect:** after a late or slow round the next idle window can get no backfill. The per-5-minute
allocation of P-10 (`b − round_cost` ≥ `b − steady(b)`) is unchanged and is delivered after every on-schedule,
on-pace round, exactly as it is after every on-schedule round today. The guarantee is the same as today's. The
exception now also covers slow rounds.

**Verification in A-6 (turns the estimates into measurements):**

1. A deterministic simulation test on the fake clock (`tests/collector/test_open_interest_latency.py`). It runs the
   real `OpenInterestTask` and the real `OpenInterestBudget` (the rig counts the budget in the source), with a fake
   source: 0.48 s per success, a separate duration per failure (0.5 s and 10 s), per-symbol failure scripts and
   scripted publication times, at `S = 200`, `b = 400`. Each line of the table above gets a test asserting its bound
   (or, for a "missed" line, that the row is absent before the stated time). The late path is exercised for the
   **last** symbol in sorted order, with publication between `e + 260 s` and `e + 490 s`. It is a measurement of the
   code on a model of the network, labelled as such.
2. After deployment, a soak measurement from production: `readable_by_ms − time_ms` per row over 24 h. It gives the
   share within 600 s, and per round the first and last stamp after the wake. It is recorded in the ADR's evidence
   (new E-7) as **measured**. It is not a release gate (no latency is guaranteed, P-10).

### 2.7 Discovering the open interest universe (additive)

`GET /v1/instruments` gains one field per entry:

```json
{ "venue": "binance-usdm", "symbol": "BTC/USDT:USDT", "timeframe": "5m", "newest_open_time_ms": 1792673100000,
  "open_interest_period": "5m" }
```

- `open_interest_period` is the configured open interest period (`"5m"`) when `(venue, symbol)` is an open interest
  symbol of the file and `[open_interest]` is enabled, otherwise `null`. It is the same for every timeframe entry of
  a symbol.
- Additive under the contract's compatibility rule (new fields may be added within `/v1`). The order, the entries and
  `newest_open_time_ms` do not change. It needs no database read: it is taken from the file the API read at start,
  like the rest of `/v1/instruments`.
- A consumer (Sentaris) discovers the universe with one call it may already make. A `404 unknown_series` from
  `/v1/open-interest` for a symbol with `open_interest_period: null` then means "not collected", not "broken".
- Not chosen: a new endpoint (one more path for a list that already has a home), or the list inside `rule` (repeated
  on every open interest page).

## 3. Contract changes (no new field; one value changes)

- 6.5.7, `rule`: "`S` the number of symbols of the configured venue" becomes "`S` the number of open interest symbols
  of the configuration (`[open_interest].symbols`; every symbol of the venue for `"all"`)". The contract states only
  `c` and `expected_after_ms`; their formulas do not change.
- 6.5.7, "With c > 1 … up to `c x 5 min + round_offset_ms`": becomes "up to `c x 5 min + round_offset_ms` plus the
  symbol's position in its round (bounded by the round deadline)".
- 6.5.8, `configured`: "whether `(venue, symbol)` is in the configuration file with the configured open interest
  period" becomes "whether `(venue, symbol)` is an open interest symbol of the configuration file
  (`[open_interest].symbols`) with the configured open interest period". **This changes values consumers see:** with a
  list, a series of a venue symbol outside the list is `configured: false` where it is `true` today. Today the API
  computes `configured` from the candle universe (`api/app.py`, `_open_interest_health`). `create_app` gets the open
  interest symbol set as a **required** parameter, so no caller can silently get an empty set. The period part of
  the sentence needs no code: a series of another period is counted as unjudged before `behind` is built, so it never
  reaches the flag. The implementation adds no period check, and no test for one, because the behaviour cannot be
  observed.
- R2 of the request is met as is: `rule.cadence` and `rule.expected_after_ms` stay.
- 6.4.2 (additive): `open_interest_period` per `/v1/instruments` entry (section 2.7).
- 6.5.8 (additive): the `collected` object (section 2.4), and a sentence that the outer fields judge every stored
  series and can be dominated by retired ones, with a pointer to `collected`.
- The contract's status line records these as an additive amendment (A-6) approved by the user.

## 4. Decisions

| ID | Question | Decision |
|---|---|---|
| D-1 | Form of the key | **approved (2026-11-16):** required `symbols = "all" \| [...]`; the shipped files add it; a file without it exits with code 2 |
| D-2 | Retired series masking open interest health | **revised by the user (2026-11-16):** resolved before the first production release, existing semantics and data kept: the additive `collected` object (2.4) |
| D-3 | Shipped subset | **approved (2026-11-16):** 200 symbols, chosen once by 24 h quote volume at authoring time (public ticker, no credentials), written into both files, never derived at run time |
| D-4 | `R` | **approved (2026-11-16):** `R = 50` |
| D-5 | Overview tool | **approved (2026-11-16)** as "read the file", then **changed by the user (2026-11-16)** to the API field of 2.7 (2.5) |
| D-6 | An explicit request timeout for the open interest client (today CCXT's default 10 s; 2.6 shows that slow failures, not the reserve, decide R1 under errors) | **approved (2026-11-16):** no change in A-6. A shorter timeout also applies to 500-row backfill pages, whose duration is not measured, and three failed pages suspend a series' catch-up until restart (P-12). Revisit after the soak measurement, which A-6 extends with per-request duration (p50/p99/max for success and failure) in the hourly open interest measurement line |
| D-7 | The pace rule of 2.6 (count a slow previous round in full) | **approved (2026-11-16):** adopt; it extends today's late-round exception, keeps the P-10 allocation, and stops a slow round's pace from propagating through a catch-up |

## 5. Tests and invariants

**Tests (P-15 additions and changes):**

- Cadence, fixed points: the table of 2.1; `c(10, 100) = 1`, `c(36, 100) = 1`, `c(37, 100) = 2`, `c(432, 100) = 12`,
  433 at 100 refused (unchanged); `c(600, 200)` changes from 9 to 7 and `c(522, 400)` from 4 to 3; the `CadenceError`
  message names 2 976 at 400, 1 176 at 200 and 432 at 100.
- Cadence, exhaustive, **every integer budget `b` in `100..400`** and every `S` from 1 to `largest(b) + 1`
  (≤ 2 977): (1) `c` is minimal: `round_cost(S, c) ≤ steady(b)` and, for `c > 1`, `round_cost(S, c − 1) > steady(b)`;
  (2) `1 ≤ c ≤ 12`, and `CadenceError` exactly when `S > largest(b)`, with `largest(b)` the closed form of 2.1;
  (3) the new `c` is at most the legacy `c` (the old formula kept in the test as the oracle), so every configuration
  accepted today stays accepted; (4) the backfill share ≥ `b − steady(b)` ≥ 25; (5) `round_cost + share = b`;
  (6) `c` is non-decreasing in `S` for fixed `b` and non-increasing in `b` for fixed `S`. Pure integer code, about
  0.49 million `(S, b)` pairs (a plain loop took 0.56 s). It stays in the default suite, because it is the ENFORCED
  evidence of the cadence invariant and `make check` must run it. If it is slow, the loop is optimised; it is not
  moved out of the suite.
- Atomic reserve: 200 symbol coroutines fail at the same instant on the fake clock, with `MAX_IN_FLIGHT = 2` and a
  source that yields (`await asyncio.sleep(0)`) between the limiter grant and the answer. Exactly 50 tickets are
  taken, at most 50 retry requests reach the source, and 150 symbols end `retry_reserve_exhausted`. Unit tests of the
  counter: `try_take_retry` returns `True` 50 times, then `False`, and never decrements; a new round starts at 0; a
  ticket taken by a retry cancelled at the deadline stays taken; `try_take_retry` is not a coroutine function
  (`inspect.iscoroutinefunction` is false), which pins that no `await` can enter it.
- Latency simulation (2.6, verification 1): each line of the table, with its bound or its "absent before" assertion.
- Retry reserve: with `n = 200` and 60 injected request errors on chosen symbols, exactly 50 retries start and 10
  symbols end `retry_reserve_exhausted`; with 10 errors all 10 are retried; a `429` pause, a refusal and an absent row
  take no ticket; a write `busy` error takes one; the `oi-round` line reports `retry_reserve_exhausted=10`.
- Pacing (the budget counted in the source): with the backfill share saturated and the previous round on schedule
  and on pace, the next round's starts wait at most 5 s in the limiter; with the previous round slow (span above
  `pace_limit_ms(n)`), the backfill leaves `round_cost` free and the next round's starts never wait; a slow round
  followed by a healthy network does not make the round after it slow (no propagation); `pace_limit_ms(200) =
  60 000`, `pace_limit_ms(131) = 39 600`.
- Measurement line: the hourly open interest measurement line carries per-request duration p50/p99/max for
  successes and failures (D-6).
- Config: `symbols` missing, `""`, `"ALL"`, `[]`, `[1]`, a duplicate, a symbol not in the venue → config errors naming
  the key; `"all"` and a list accepted; checked also with `enabled = false`; the cadence error uses the list's `S`.
- Agreement: the Collector and the API derive the same `c` and `expected_after_ms` from a file with a list.
- Subset: only listed symbols are requested in rounds and backfill; an unlisted venue symbol with stored rows answers
  `200` with `due`/`stale` after its newest row and appears in `behind` with `configured: false`. The negative
  evidence that unlisted symbols are never requested is the CLI wiring test (the task only knows its config).
- Health `collected`: 200 listed series and 322 retired, stale series → the outer object unchanged (`behind` full
  of retired series, `status: "behind"`), `collected.status: "current"`; one listed series stale → it is the first
  entry of `collected.behind` and `collected.status: "behind"`; a listed symbol with no row → `never_stored: 1`,
  `series_count + never_stored = symbol_count`; a conflict on a retired series counts only in the outer object; with
  `symbols = "all"` the outer fields are byte-identical to a golden captured from `main` on the same seeded database,
  the same `as_of_ms` and the same file without the key (golden comparison); with `"all"`, `collected` equals the
  outer counts when every stored series has the configured venue, a venue symbol and the configured period.
- Instruments: `open_interest_period` is `"5m"` for listed symbols on every timeframe entry, `null` for the others,
  `null` everywhere when `[open_interest]` is disabled or absent; the other fields and the order are unchanged.
- Overview (after D-5 is confirmed): an instrument with `open_interest_period: null` shows "not collected", is not
  ranked or coloured as a problem on the dashboard, and gets no open interest coverage request; the candle summary's
  state keys are unchanged; the symbol detail view shows "not collected" instead of handling `unknown_series`.

**`docs/invariants.md`:**

- new: a round starts at most `reserve(n)` retries;
- new: rounds and the backfill request only the open interest symbols of the configuration;
- new: retry tickets are taken atomically on the round's event loop and never returned;
- new: `collected` judges only listed series, and the outer `open_interest` fields do not change with A-6;
- new: `open_interest_period` of `/v1/instruments` comes from the file, never from the database;
- new: the backfill gate counts the previous round in full when it was late or slower than `pace_limit_ms(n)`;
- INV-087 and INV-090 (configuration validation and the derived cadence) extended to the new key and the new formula.

## 6. Deployment and compatibility

- No schema change, no migration. Both processes must read the same file (the existing `sha256` check); until both
  are restarted the states shift as described in 6.5.7.
- With D-1 as proposed, a configuration file that enables or contains `[open_interest]` and lacks `symbols` stops
  both processes at start (exit code 2). Both shipped files add the key.
- README: the `[open_interest]` example gains `symbols`; the `oi-round` line gains the reserve count; the overview
  section describes "not collected".
- Release: with the first open interest release (together with funding rate), so production never holds retired
  series (2.4).

## 7. Answer to the request's questions (draft, for the reply to Sentaris)

1. Nothing else on the Collector's IP calls `/futures/data/*`.
2. Option (a) plus a configured subset; (b) and (c) are not taken.
3. At `b = 400` the largest `S` at `c = 1` is **248** (ADR-0003 P-10 after A-6); Quasar ships a subset of 200 (D-3)
   to keep the late-publication path at about `e + 550 s` (estimated, section 2.6; measured after deployment).
   The contract does not state `S`; `rule.cadence` shows it, and `/v1/instruments` names the collected symbols
   (`open_interest_period`, section 2.7). Even with an unlimited budget, 522 symbols in
   one round would store a late-published row only about 626 s after its period end (section 1, point 3), so a subset
   is needed for the timing as well. Rows of a round that starts late (a host stall, the first round after a restart)
   can miss 600 s; the consumer fails closed on them, as the request expects.
