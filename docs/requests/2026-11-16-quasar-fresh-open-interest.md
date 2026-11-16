# Change request: fresh open interest for every configured symbol

- **From:** Sentaris Engine (consumer of the Quasar Market Data API v1). **To:** Quasar Market Data. **Date:** 2026-11-16.
- **Status:** open, awaiting Quasar's answer.
- **Affects:** ADR-0003 P-10 (budget, cadence, timing) and its open decisions O-2 and O-5; the shipped `config/quasar.toml` and `config/quasar.compose.toml` (`[open_interest]`); possibly the contract `docs/contracts/api-v1.md` section 6.5 (stated latency).
- **Compatibility:** no field, endpoint or meaning of a row changes. The request is about how soon a row is stored, not about what it holds.

## 1. Why

The engine's first strategy, `ocb` `1.0.0` (Sentaris Engine `docs/strategies/ocb.md`), reads `5m` candles and `5m` open interest. It declares a tolerance of 600 000 ms for both. Under the engine's sufficiency rule (Sentaris Engine domain document, section 6.14), every open interest row whose period end `e` satisfies `e + 600 000 ≤ as_of` must be readable at `as_of`. If one is not, the pair fails with `series_stale` and the strategy is not evaluated. The engine's live schedule runs at 20 s past every `5m` boundary.

With the shipped configuration (522 symbols on `binance-usdm`, `budget_per_5min = 400`), `open_interest_cadence(522, 400) = 4`. Each symbol is asked every 20 minutes, and `expected_after_ms = 4 × 300 000 + 200 000 = 1 400 000`. In an ideal run, the newest row of a symbol is usually older than 600 s at a given `as_of`. So almost every `ocb` evaluation over real Quasar data ends in `series_stale`.

When ADR-0003 accepted a planned latency of up to ~28 minutes (O-5), no consumer needed fresher open interest. `ocb` is the first that does. The engine will not loosen its tolerance to fit the current cadence: a tolerance above 600 s changes what the strategy measures, and the strategy specification fixes it (`ocb` R3, R4).

## 2. What is requested

### R1. Cadence 1 for every configured symbol

For every symbol configured on `binance-usdm` with open interest enabled, each symbol is asked in every period (`c = 1`). Today that holds only up to `S = 149` at `b = 400` (ADR-0003 P-10 table).

The outcome the engine needs: in an ideal run (P-10 "Planned latency"), the row of the period ending at `T` is stored and readable no later than `T + 600 000 ms`, with a margin Quasar considers safe. With `c = 1` and `round_offset_ms = 200 000`, the planned latency is 200 s for publication within the round offset and 500 s for later publication, which fits.

The request is for the shipped universe of 522 symbols. If Quasar meets it for fewer, please state the largest `S` for which `c = 1` holds under the new rules, so that the engine's operator can size its own universe.

### R2. The cadence stays visible

`GET /v1/open-interest/coverage` already exposes `rule.cadence` and `rule.expected_after_ms`. Please keep them, so that a consumer can see when the configuration falls back to `c > 1`. The engine uses no other signal of this.

## 3. Ways Quasar might meet R1

Quasar chooses. The arithmetic below uses P-10's formulas and is a rough estimate, not a measurement.

| Option | Change | `c = 1` holds up to about |
|---|---|---|
| Today | `steady(b) = floor(3b/4)`, retry reserve of one per symbol: `2S + 2 ≤ steady(b)` | 149 at `b = 400` |
| (a) Shared retry reserve | Replace the per-symbol reserve with a small shared one `R` (retries happen only after a request error, P-10): `S + 2 + R ≤ steady(b)` | 248 at `b = 400`, `R = 50` |
| (b) Backfill from leftovers | Rounds already pre-empt backfill (OI-4). Do not reserve a quarter for it in steady state; it gets what rounds leave: `S + 2 + R ≤ b` | 348 at `b = 400`, `R = 50` |
| (c) Larger budget share | Revisit O-2 (40 % of the documented 1 000 per 5 min, 60 % left for other users of the IP) | 522 with (a) and (b) needs `b ≥ 574`; with (a) alone, `b ≥ 766` |

The engine's view on each point, for Quasar to weigh:

- **(a) and (b)** keep the budget share unchanged and change only how it is split. Together they do not reach 522 at `b = 400`.
- **(c)** is the only option that reaches 522. Whether the IP has other users of `/futures/data/*` is an operational fact Quasar knows and the engine does not. Whether a `418` caused by open interest would also ban klines is not known (P-10 point 3). The engine does not ask Quasar to accept that risk without measuring it.
- **A subset** is acceptable to the engine if R1 cannot hold for 522: open interest at `c = 1` for a configured subset of symbols (for example the most liquid), with candles for all. The engine's operator then restricts `ocb` to that subset. Any subset must be stated in the configuration, not derived at run time.

## 4. Not requested

- Sampling `/fapi/v1/openInterest` instead of the history endpoint. ADR-0003 P-1 rejected it for good reasons (no backfill, no recheck, unfillable holes), and the engine agrees.
- Any change of a row's meaning, of `readable_by_ms`, or of coverage states.
- A guaranteed latency. P-10 states that no latency is guaranteed. The engine fails closed on a late row (`series_stale`) and needs only the planned latency of R1 in an ideal run.

## 5. What the engine does meanwhile

Nothing changes in the engine. An `ocb` pair on a symbol at `c > 1` fails with `series_stale` as the engine's failure, never as "no signal". The engine does not tune any tolerance to Quasar's current cadence.

## 6. Questions for Quasar

1. Does anything else on the Collector's IP call `/futures/data/*`? (This decides how far O-2 can move.)
2. Which of options (a) to (c), or a subset, does Quasar prefer?
3. If R1 is met, what is the largest `S` at `c = 1`, and does the contract or ADR state it?
