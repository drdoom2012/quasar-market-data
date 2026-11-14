# Reply 2: expose the ingestion time of every stored row

- **To:** Quasar Market Data
- **From:** Sentaris Engine
- **Date:** 2026-11-14
- **Re:** `docs/requests/2026-11-14-expose-ingestion-time-response-2.md`
- **Status:** accepted as written. Approved by the engine's owner and recorded in the engine's domain document, section 6.10.

## 1. 6.2: the inverted flag is accepted

- **Mechanism.** We accept the durable confirmation written after a commit that met `B`. A row is trusted only when its transaction is confirmed. This is stronger than the recorded violation we asked for, because no failure mode can leave a row unflagged.
- **Name.** We accept `readable_by_unconfirmed`, with the meaning stated in section 1 of your response. That includes `false` for rows stored before the cut-over, and that the field changes only from `true` to `false`.
- **Engine behaviour.** The engine treats a row with `readable_by_unconfirmed: true` as untrusted, exactly like a conflicted row. An evaluation that needs it fails.

## 2. The remaining difference between live and replay

We accept the one remaining case: a confirmation written after a live evaluation read the row. Live then sees the row as unconfirmed and fails, while a later replay at the same `as_of` sees it as confirmed. We record it as an accepted difference between live and replay.

It can be removed later if Quasar exposes when a confirmation became readable, with the meaning of `readable_by_ms`. The engine would then treat a row as confirmed at `as_of` only when its confirmation was readable by then. This is not requested now. It is a possible later request, together with `first_seen_at_ms` of conflict records.

## 3. Next steps

Please proceed as in section 2 of your response:

1. owner approval;
2. publication of the amended contract sections, including `readable_by_unconfirmed`;
3. implementation.

Please tell us when the contract text is published.
