# Notice: the row readability contract is published

- **To:** Sentaris Engine
- **From:** Quasar Market Data
- **Date:** 2026-11-14
- **Re:** `docs/requests/2026-11-14-expose-ingestion-time-reply-2.md`
- **Status:** the contract text is published and approved by the Quasar owner. The implementation is complete and
  reviewed but **not yet deployed**. We will send a separate notice when it is.

## 1. Where the contract is

`docs/contracts/api-v1.md`:

| Section | Content |
|---|---|
| §0 item 7 | the summary: every row carries the two fields; treat `readable_by_unconfirmed: true` as untrusted |
| §4.1 | the normative definition of `readable_by_ms` and `readable_by_unconfirmed` |
| §5.2, §6.5.4, §6.7.4 | the row fields on `/v1/candles`, `/v1/open-interest` and `/v1/funding-rates`, with examples |
| §6.2.4 | the `readability` object of `GET /health/ingest` |
| §7 | what is out of scope |
| §9, decisions RB1 to RB6 | the decisions; Q5 is marked superseded |

The design behind it is ADR-0005 (`docs/adr/0005-row-readability.md`).

## 2. What it says, in short

Everything below is as agreed in our exchange. Section 4.1 is the binding text.

- **The two fields.** `readable_by_ms` (integer epoch ms, never `null`) and `readable_by_unconfirmed` (boolean,
  always present) are on every row of the three endpoints. They follow `conflicted`, in this order.
- **`readable_by_unconfirmed: false`.** A read transaction that starts at or after `readable_by_ms`, by the Collector
  host's wall clock, sees the row.
- **`readable_by_unconfirmed: true`.** The bound could not be confirmed, so `readable_by_ms` is not a bound. This
  covers a commit slower than the bound, a Collector stop between commit and confirmation, and a failed
  confirmation write.
- **`B = 2 000 ms`** is a contract constant. Within `/v1` it may only grow.
- **One direction.** The flag can only change from `true` to `false`, within one database (see §3).
- **Rows stored before the cut-over** are served with `readable_by_ms` equal to the cut-over instant and
  `readable_by_unconfirmed: false`.
- **First stored value, every ingestion path.** The fields belong to the first stored value of a row. Backfilled
  and late rows carry the instant they were stored. Every ingestion path stamps rows the same way, and the database
  refuses a row that has no registered write transaction.
- **The clock** is the Collector host's wall clock. NTP is assumed but not checked. The API's `as_of_ms` comes from
  the API host. A backward step of the clock is not detected.
- **What the fields do not cover:** conflict records, unavailable candles, the funding verified span, coverage
  blocks and indicators. There is no `as_of` query parameter.

## 3. Points to note

1. **The health counter covers a 24-hour window and is not cumulative.** Our first response (section 3.3) described
   a cumulative counter plus the instant of the last violation. A cumulative count would read every transaction
   ever written on every health request, so section 6.2.4 defines a count per table over
   `(as_of_ms - window_ms, as_of_ms - bound_ms]`, with `window_ms` = 24 h, echoed in the body. The window may change
   without a contract break. As agreed, the counter is information only; the per-row flag is the guarantee.
2. **The confirmation rule is slightly stricter than first described.** The stamp is the wall clock rounded down to
   the millisecond. A confirmation is therefore written only when the commit ended at least 1 ms before `stamp + B`.
   For you this changes nothing: a row with `readable_by_unconfirmed: false` keeps exactly the meaning above. The
   sentence of section 4.1 that states the stricter rule comes with the implementation, so it reaches the published
   text when the implementation is merged.
3. **Restores from backup.** A restore can drop confirmations written after the backup, so a row can turn from
   `false` back to `true`. Migrating a restored older database also gives a new cut-over instant. We will announce
   every restore in advance.
4. **Replay before the cut-over** has no available data, as you accepted. The cut-over instant is `cutover_ms` in
   the `readability` object of `GET /health/ingest`.

## 4. Until deployment

The deployed API does not send the new fields yet. As agreed, your client fails closed until they appear.

The deployment will announce that `GET /health/ready` answers `"schema_version": 4`. From then on every row carries
the fields and `/health/ingest` ends with `readability`. It needs a short API outage (`503 not_ready`).
