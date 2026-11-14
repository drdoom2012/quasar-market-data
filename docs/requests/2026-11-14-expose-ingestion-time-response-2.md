# Response 2: expose the ingestion time of every stored row

- **To:** Sentaris Engine
- **From:** Quasar Market Data
- **Date:** 2026-11-14
- **Re:** `docs/requests/2026-11-14-expose-ingestion-time-reply.md`
- **Status:** your answers to 6.1, 6.3 and 6.4 are accepted as written. For 6.2 we accept the goal and change the mechanism (section 1). The amended decisions are now going to the Quasar owner for approval, and the contract text follows (section 2).

## 1. 6.2: a per-row flag, set by the absence of a confirmation

We agree that a health counter is not enough, and that every affected row must be recognisable in the responses of all three endpoints.

The mechanism you suggested would record a violation after it is detected. It cannot be made reliable. Detection happens after `COMMIT`, in the Collector's memory. If the Collector stops between the commit and the record (a crash, a kill, a host failure), or if writing the record fails (for example on a locked database), the violation is lost. The row would then be served unflagged with a bound it did not meet. That breaks fail-closed, and we will not offer it.

We invert the record instead:

- After every commit, the Collector checks the stamp-to-commit-end time on the monotonic clock. If it is within `B`, the Collector writes a durable **confirmation** for that transaction.
- If the time exceeded `B`, the Collector writes no confirmation. The same holds when the Collector stops before the confirmation is written, or when writing it fails.
- A row is trusted only when its transaction is confirmed.

Response field, on all three endpoints, with the placement you proposed:

| Endpoint | Where | New field |
|---|---|---|
| `GET /v1/candles` | each element of `candles` | `readable_by_unconfirmed` |
| `GET /v1/open-interest` | each element of `rows` | `readable_by_unconfirmed` |
| `GET /v1/funding-rates` | each row | `readable_by_unconfirmed` |

The field is a boolean and always present.

- It is `true` exactly when no confirmation is stored for the transaction that stored the row. This covers a detected violation of `B`, an interrupted Collector and a failed confirmation write alike.
- It is `false` for rows stored before the cut-over. Their bound is the cut-over instant, which holds without a confirmation (response section 3.4).
- A confirmation, once stored, is never removed. So the field can change only from `true` to `false`, never back.

What this means for the engine:

- **No missed violations.** A row is either confirmed, or served as untrusted. Treat a `true` row exactly as you planned to treat a flagged one.
- **No difference between live and replay from this source.** The confirmation is written right after the commit, normally within milliseconds. That is well before `readable_by_ms`, which lies `B = 2 000 ms` after the stamp, and the engine does not use a row before its `readable_by_ms`. A row that is still unconfirmed at `readable_by_ms` has met no bound that anyone could check. Treating it as untrusted is then correct both live and in replay. The live/replay difference you planned to accept for 6.2 is therefore not needed. One case is left: a confirmation written later than `readable_by_ms`, for example after a long stall that still met `B`. Such a row turns from untrusted to trusted later. Live sees it as untrusted, while a replay at the same `as_of` sees it as trusted. The difference is in the safe direction, and it cannot occur for a row whose commit took longer than `B`.
- **Name.** We chose `readable_by_unconfirmed` over `readable_by_violated` because the field states what Quasar knows: whether the bound was confirmed, not whether it was proven broken. If you prefer another name, tell us. The meaning above is what the contract will state.

The confirmations are stored in the same schema migration as the cut-over, so only one migration is needed, as you asked. The health counter stays as an operational signal, as you suggested. It counts transactions that exceeded `B` or whose confirmation could not be written.

## 2. Next steps

1. The Quasar owner approves the amended decisions: Q5 replaced, ADR-0002 P-2, ADR-0003 and ADR-0004 amended, and the migration (cut-over marker and confirmations).
2. We publish the amended contract sections, covering `readable_by_ms` (3.1), the bound `B` and its exception (3.2, 3.3), the cut-over (3.4), the clock (3.5 item 4) and `readable_by_unconfirmed` (section 1 above). We will tell you when they are published.
3. We implement after that.

If the inverted flag does not work for you, tell us before step 2. Otherwise we proceed with it.
