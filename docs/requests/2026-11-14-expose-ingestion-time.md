# Change request: expose the ingestion time of every stored row

- **From:** Sentaris Engine (consumer of the Quasar Market Data API v1)
- **Date:** 2026-11-14
- **Affects:** Quasar contract `docs/contracts/api-v1.md` sections 5.2, 6.5.4, 6.7.4, 7 (traceability) and 9 (decision Q5); ADR-0002 P-2, ADR-0003 and ADR-0004 (meaning of `ingested_at_ms`); the Collector's writers.
- **Compatibility:** additive within `/v1` (contract section 1, Compatibility). No field is removed or renamed and no existing meaning changes.

## 1. Why

The engine evaluates market data "as it was available at `as_of`", in live scanning and in historical replay alike. It must never use a row that it could not have read at `as_of`. Today the API cannot say when a row became readable:

- rows carry no ingestion time (decision Q5);
- the API does not know the publication delay the Collector used (section 6.1.4);
- a row collected late or backfilled looks the same as one collected on time, so a replay at a past `as_of` would see rows that a live evaluation at that `as_of` could not see.

The engine has decided (its domain document, section 6.10) that a row is available at `as_of` only when **both** of these hold:

- its completion instant plus a publication delay is at or before `as_of`;
- the instant from which Quasar served the row is at or before `as_of`.

If the API does not give the second instant, the engine treats the row as unavailable and the evaluation fails. Until this change ships, the engine therefore produces no signal over real Quasar data.

## 2. What is requested

### R1. One new field per row, on three endpoints

| Endpoint | Where | New field |
|---|---|---|
| `GET /v1/candles` | each element of `candles` | `ingested_at_ms` |
| `GET /v1/open-interest` | each element of `rows` | `ingested_at_ms` |
| `GET /v1/funding-rates` | each row | `ingested_at_ms` |

- It is an integer of epoch milliseconds (UTC), in the valid time range of section 1.
- It is always present, never `null`. Every stored row has it (the column is `NOT NULL` in all three tables).
- Name: `ingested_at_ms` is the proposal. A different name is fine if the meaning of R2 holds.

### R2. Meaning: the row is readable no later than this instant

This is the core of the request; the field is useless to the engine without it. The contract states:

> A reader that starts a read transaction at or after `ingested_at_ms` (by the Collector host's clock) sees the row.

In other words, the field is an **upper bound** on the instant the row became readable through the API. It must never be earlier than the commit that made the row visible.

**This is not what the column holds today.** The Collector reads `clock.wall_ms()` before it hands the write to the writer (for example `fetch_once.py`, `open_interest_task.py`, `funding_rate_execution.py`). The row commits later, after:

- waiting in the executor queue;
- waiting for the SQLite lock, which can take up to the busy timeout;
- for open interest and funding, earlier transactions of the same batch, since one stamp is shared by several transactions (ADR-0003, "a 500-row backfill page is written as up to 5 transactions").

So the stored value is a lower bound, and the gap to visibility is not bounded by anything stated in the contract.

Quasar chooses how to satisfy R2. Some ways:

- **(a) Stamp inside the transaction.** Take the stamp inside the write transaction, after the write lock is acquired (`BEGIN IMMEDIATE`) and just before `COMMIT`, one stamp per transaction. The contract then states the remaining bound: the time from the stamp to the end of `COMMIT`. To make that bound hold, the transaction is rolled back and retried with a fresh stamp if the stamp is older than the bound when `COMMIT` is reached.
- **(b) Keep the current stamp and enforce a bound.** Keep the stamp where it is, but enforce and document a maximum delay `B` between stamp and commit, with the same rollback-and-restamp rule. Then expose `ingested_at_ms + B`, or expose `B` in the contract, so that the consumer can add it.
- **(c) Another mechanism** with the same guarantee.

Whichever is chosen, the contract must name the bound explicitly. The engine will not guess one.

### R3. Semantics the contract must state

1. **First stored value.** `ingested_at_ms` belongs to the stored row, that is, the first value stored for that identity. A later identical observation (`ON CONFLICT DO NOTHING`) and a later conflict record never change it. This already follows from the immutability triggers (ADR-0002 P-4); the contract should say so.
2. **Backfill and late collection.** A backfilled row, or a row collected late, carries the instant at which *it* was stored, not its open or period time. The contract should state that the field makes late-collected rows recognisable.
3. **Every ingestion path.** The field means the same on every path that writes rows: `run`, its backfill, `fetch-once`, `fetch-open-interest-once`, `fetch-funding-rate-once`, and any future import. If a path cannot satisfy R2, for example an import of history from a file, its rows must be distinguishable, or the path must not exist. A row whose field does not mean R2 must never be served as if it did.
4. **Clock.** The clock is the wall clock of the Collector host. The contract should state this, together with any synchronisation that is assumed (for example NTP) and what, if anything, Quasar records about its offset to the exchange's server time (ADR-0001 D-D records RTT and offset as diagnostics). The engine still has to decide how to handle a clock difference (its OD-6 remainder), and needs this description to do so.
5. **Not part of identity.** The field stays excluded from the "identical duplicate" comparison (ADR-0002 P-2), as today.

### R4. Decision Q5 is reversed

Q5 ("Expose `ingested_at_ms` per candle? No: it is diagnostic") is replaced by a new decision that exposes the field with the meaning of R2 for candles, open interest and funding. The field stops being diagnostic: a consumer's logic depends on it. The ADRs that call it diagnostic (ADR-0002 P-2 and the corresponding text of ADR-0003 and ADR-0004) are amended.

## 3. Tests the change should carry

- Every row of all three endpoints carries `ingested_at_ms`, equal to the stored value.
- A repeated identical write and a recorded conflict leave `ingested_at_ms` of the stored row unchanged.
- A row written by the backfill carries the time of the backfill write, not a time derived from its open or period time.
- R2 holds under contention: with the write delayed (a held write lock, a slow executor), a reader that starts at or after the exposed instant sees the row. With the bound exceeded, the write is restamped or rolled back, never committed with the stale stamp.
- For open interest and funding batches split into several transactions, every transaction's rows satisfy R2 on their own.

## 4. Not requested now (possible later)

- **`first_seen_at_ms` of conflict records.** If the instant a conflict was first recorded is exposed per conflicted row, with the same R2 meaning, the engine could judge "conflicted as known at `as_of`". That would remove its one accepted difference between live and replay (engine domain section 6.10). It is a separate, later request.
- **The Collector's publication delay.** Since S20 the API and the Collector read the same configuration file. If the contract could state the publication delay that was in force when a row was written, the engine could check its own delay against it. This is not needed for R1 to R4.

## 5. Out of scope

- No new endpoint and no change to coverage, health, indicators, paging or errors.
- No change to what is stored, other than how the stamp is taken (R2).
- No point-in-time query parameter (`as_of`) on the API; the engine filters by availability itself.
