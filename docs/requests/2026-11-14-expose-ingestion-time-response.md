# Response: expose the ingestion time of every stored row

- **To:** Sentaris Engine
- **From:** Quasar Market Data
- **Date:** 2026-11-14
- **Re:** `docs/requests/2026-11-14-expose-ingestion-time.md`
- **Status:** accepted in principle, with changes. This is a proposal. It becomes binding once you answer the questions in section 6 and the Quasar owner approves the amended decisions (Q5, ADR-0002 P-2, ADR-0003, ADR-0004).

## 1. Summary

We agree with the motivation. Your description of the current state is correct, and the funding case is worse than you described (section 2). We will expose a per-row upper bound on readability for candles, open interest and funding rates.

We cannot give R2 in the exact form you asked for, for two reasons:

1. **A row cannot carry the instant after its own `COMMIT`.** Any stamp written into the row is taken before the commit makes it visible. The commit itself runs with `synchronous=FULL` (an fsync), and its duration has no hard upper bound. We can enforce a bound up to the moment `COMMIT` is issued. Past that moment we can only detect a violation, not prevent it. Section 3.3 covers this.
2. **Rows already stored do not meet R2.** Their stamps are lower bounds with no limit on the gap to commit. The change cannot be additive for them. It needs a one-off cut-over, and that requires a schema migration (section 3.4). So section 5 of your request ("no change to what is stored") does not hold in full.

Everything else in R1, R3 and R4 we accept as written, with the clock caveats in section 3.5.

## 2. Current state: confirmed, plus one addition

- The Collector reads the wall clock before it hands the write to the writer. The writer only passes the value through. The stored value is a lower bound, as you say.
- **Funding, addition:** one stamp is shared by every transaction of a page. Between those transactions the funding writer deliberately yields to pending candle writes, which have priority. So the later transactions of a page can commit well after the stamp, by more than one lock wait.
- ADR-0002 P-2 says the column holds "the wall-clock time the Collector committed the row". The code does not do that, and we will correct the ADR either way.

## 3. What we propose

### 3.1 Field

| Endpoint | Where | New field |
|---|---|---|
| `GET /v1/candles` | each element of `candles` | `readable_by_ms` |
| `GET /v1/open-interest` | each element of `rows` | `readable_by_ms` |
| `GET /v1/funding-rates` | each row | `readable_by_ms` |

It is an integer of epoch milliseconds (UTC), always present and never `null`. We propose a name different from `ingested_at_ms` on purpose. The served value is not the stored column: it is the stored stamp plus a bound (3.2), or the cut-over instant (3.4). Reusing the column's name would make the two easy to confuse (question 6.1).

Contract meaning:

> A read transaction that starts at or after `readable_by_ms`, by the Collector host's wall clock, sees the row, except in the detected case of section 3.3.

### 3.2 How the stamp is taken (your option (a) combined with (b))

- The writer takes the stamp **inside** the write transaction, after `BEGIN IMMEDIATE` has acquired the write lock. There is one stamp per transaction, including each transaction of an open interest or funding batch. Lock waits, executor queueing and earlier transactions of the same batch are therefore outside the stamp-to-commit interval.
- The writer measures elapsed time from the stamp on the **monotonic** clock. If the time elapsed when `COMMIT` is about to be issued is more than `B` minus a fixed margin for the commit, the transaction is rolled back and retried with a fresh stamp. A row is never committed with a stale stamp.
- The served value is `readable_by_ms = ingested_at_ms + B`.
- `B` is a **contract constant**. We propose `B = 2 000 ms` (question 6.3). Only the in-transaction work and the commit fall inside `B`, because the lock is already held. On our hardware these take milliseconds, so `B` leaves a wide margin.
- `B` may only grow in a later version, never shrink. Growing it keeps every stored row correct, only more conservative. Shrinking it would need `/v2`.

### 3.3 The case we cannot prevent: a commit slower than its margin

Once `COMMIT` has been issued, the row may become visible at any moment, and stored rows are immutable (ADR-0002 P-4). If the fsync stalls past the stamp plus `B`, the row is already served with an upper bound that is too early. We cannot correct that row afterwards.

What we will do:

- After every commit, the writer measures the stamp-to-commit-end time on the monotonic clock. If it exceeds `B`, the writer logs an error naming the series, the time range of the transaction's rows and the observed duration.
- We will count these violations and expose the count in the health objects, as a cumulative counter plus the instant of the last violation. A consumer can then fail closed for a period with a violation instead of guessing (question 6.2).

The contract will state this exception explicitly. We will not describe the bound as unconditional.

### 3.4 Rows stored before the change (R3.3)

A schema migration runs once, at the start of the first Collector with the new semantics, before that Collector writes anything. It records the cut-over instant on the Collector host's wall clock, together with a marker that separates old rows from new ones.

The Collector is the only writer, and the previous Collector process has stopped by the time the migration runs. So every old row was already visible at the cut-over instant. Old rows are therefore served with `readable_by_ms` set to the **cut-over instant**. That value is a correct upper bound, never `null`, and keeps R1.

The consequence: a replay at an `as_of` before the cut-over sees no available rows, even where data is stored. History before the cut-over becomes usable only by re-collecting it under the new semantics (question 6.4).

### 3.5 Semantics for R3

1. **First stored value:** accepted. `readable_by_ms` belongs to the first stored value of the identity. An identical later observation (`ON CONFLICT DO NOTHING`) and a conflict record never change it. The immutability triggers already enforce this, and the contract will say so. Conflict records also store a stamp. It will be taken the same way, but it is not exposed (your section 4).
2. **Backfill and late collection:** accepted. A backfilled or late row carries the instant at which it was stored. One consequence: a replay before Quasar's own start (or before the cut-over, 3.4) sees nothing available, even though the backfill stored those rows.
3. **Every ingestion path:** accepted. `run` and its backfill, `fetch-once`, `fetch-open-interest-once` and `fetch-funding-rate-once` all write through the same writers and therefore get the same stamping. No import path exists today. If one is ever added, its rows will be distinguishable, or the path will not be added.
4. **Clock:** the field uses the Collector host's wall clock. We assume, but do not check, that the host is synchronised (NTP). Quasar records nothing about the host clock's offset to true time.
   - The offset to the exchange's server time (ADR-0001 D-D) is measured as a diagnostic. It appears only in the Collector log. It is not stored and not exposed.
   - The API and the Collector are deployed independently and may run on different hosts. The API's `as_of_ms` comes from the API host's clock, not the Collector's.
   - A backward step of the wall clock between stamp and commit is not detected by the monotonic check in 3.2. Its effect is limited to the size of the step.

   The OD-6 remainder stays your decision. Quasar provides no clock guarantee beyond this description.
5. **Not part of identity:** unchanged. The stamp stays out of the identical-duplicate comparison.

### 3.6 Decision Q5 (R4)

Q5 will be replaced by a new decision: expose `readable_by_ms` with the meaning of 3.1 for all three row types. The field stops being diagnostic. ADR-0002 P-2 and the matching text of ADR-0003 and ADR-0004 will be amended. This needs the Quasar owner's approval. That approval is the condition named in the header.

## 4. Tests

We accept your list in section 3 and will add:

- The bound check: a transaction whose in-transaction time is past `B` minus the margin is rolled back, restamped and committed with the fresh stamp.
- Post-commit detection: a commit that exceeds `B`, simulated with a fake clock, is logged and counted, and the count appears in health.
- Cut-over: old rows are served with the cut-over instant and new rows with their own `readable_by_ms`. The migration is idempotent and restart-safe.
- Funding: a page split into several transactions, with candle writes interleaved, gives each transaction its own stamp, and each one satisfies the bound.

## 5. Not part of this change

We agree with your sections 4 and 5. We would handle `first_seen_at_ms` of conflict records the same way later, if you request it.

## 6. Questions for you

1. **Name:** is `readable_by_ms` acceptable? We would rather not reuse `ingested_at_ms`, because the served value differs from the stored stamp.
2. **Residual exception (3.3):** is a detected but unpreventable violation acceptable, if it is exposed as a counter plus the last instant in health? If not, tell us what guarantee you need. The only stronger option we see is a two-phase publish, which would need a deeper schema change than this request.
3. **`B`:** does `2 000 ms` fit your availability lag? A smaller `B` causes more restamp retries under load. A larger one delays availability for you.
4. **Cut-over (3.4):** is it acceptable that rows stored before the cut-over become available only from the cut-over instant? The alternative is to delete them and re-collect them, which leaves the same gap in history.

Once we have your answers and the owner's approval, we will publish the amended contract sections and decisions before implementing.
