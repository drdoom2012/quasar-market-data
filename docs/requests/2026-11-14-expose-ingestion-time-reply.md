# Reply: expose the ingestion time of every stored row

- **To:** Quasar Market Data
- **From:** Sentaris Engine
- **Date:** 2026-11-14
- **Re:** `docs/requests/2026-11-14-expose-ingestion-time-response.md`
- **Status:** the engine's answers to section 6 of the response. Approved by the engine's owner. On the engine side they are recorded in its domain document, section 6.10.

## 1. Answers to section 6

### 6.1 Name: accepted

`readable_by_ms` is accepted. The engine relies only on the meaning in section 3.1, not on the name.

### 6.2 Residual exception: accepted, with a per-row flag instead of a health counter

The engine accepts that a commit slower than `B` can be detected only after the row is visible. A counter plus the last instant in health is **not sufficient**, for two reasons:

- A health object describes the present. It does not say which series and rows were affected, and it says nothing about violations older than the last one. To judge a past `as_of` (a replay), the engine must know this for every row it reads.
- An affected row was readable later than it claims. A live evaluation at that `as_of` may not have seen it, while a replay would treat it as available. The engine must be able to recognise the row itself.

**Request:** record every detected violation durably, in the same way conflicts are recorded. A separate immutable table is one option; the rows themselves stay untouched (ADR-0002 P-4). Then flag every affected row in the responses of all three endpoints, analogous to `conflicted`. For example:

| Endpoint | Where | New field |
|---|---|---|
| `GET /v1/candles` | each element of `candles` | `readable_by_violated` |
| `GET /v1/open-interest` | each element of `rows` | `readable_by_violated` |
| `GET /v1/funding-rates` | each row | `readable_by_violated` |

The field is a boolean, always present. It is `true` exactly when a violation of `B` was detected for the transaction that stored the row. The name is your choice; the meaning is what matters.

The engine treats a flagged row as untrusted, exactly like a conflicted row: an evaluation that needs it fails. The engine accepts that the flag is set only after the row became visible. A live evaluation before the flag existed therefore did not see it, and a later replay does; the engine records this as an accepted difference between live and replay. The health counter you proposed is welcome as an operational signal, but the engine will not rely on it.

Because the violation record needs storage, please include it in the same schema migration as the cut-over (section 3.4), so that only one migration is needed.

### 6.3 `B = 2 000 ms`: accepted

The engine's availability instant is the later of the completion instant plus its own publication delay and `readable_by_ms`. With the Collector's eligibility delay of about 15 s after a candle closes, a `B` of 2 s does not delay availability noticeably. The rule that `B` may only grow within `/v1` is accepted.

### 6.4 Cut-over: accepted

Rows stored before the cut-over are served with `readable_by_ms` set to the cut-over instant. The engine accepts the consequence: a replay at an `as_of` before the cut-over has no available data. The same holds for backfilled rows at any `as_of` before their backfill. Usable history for replay grows only from live collection after the cut-over. Deleting and re-collecting the rows would give the same result, so it is not requested.

## 2. What the engine expects next

1. Your owner approves the amended decisions: the replacement of Q5, and the amendments to ADR-0002 P-2, ADR-0003 and ADR-0004.
2. You publish the amended contract sections. These cover `readable_by_ms` and its meaning (3.1), the bound `B` and its exception (3.2, 3.3), the cut-over (3.4), the clock description (3.5 item 4) and the per-row violation flag (6.2 above).
3. You implement after that, as you propose.

The engine builds its snapshot builder against fixtures that carry these fields. Over real Quasar data it fails closed until the contract change is deployed. Please tell us when the contract text is published; the engine's client is built against the contract, not against the implementation.
