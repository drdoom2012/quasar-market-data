# Invariant register

This register lists the rules from `CLAUDE.md` ("Non-Negotiable Rules") and says, for each one, whether anything
enforces it today. It exists so that no rule is described as enforced when it is only written down.

## Statuses

- `ENFORCED` — a check runs in CI (`make check`) **and** a named negative test exists that fails when the invariant is
  violated. The evidence cell is `path/to/test.py::test_name`, optionally followed by `[case_id]` to name the specific
  parametrized negative case. Several references are separated by commas. A passing positive test alone does not
  qualify; the unmodified-copy positive control in `tests/test_import_contracts_negative.py` is not evidence.
- `PLANNED` — not enforced yet. The cell names the slice or task that will enforce it, taken from
  `docs/superpowers/specs/2026-10-20-market-data-v1-design.md` (section 8) or
  `docs/superpowers/plans/2026-10-20-slices-s1-s4.md`. `unscheduled` means no slice names it.
- `NOT_APPLICABLE` — decided to be out of scope as an automated mechanism. The cell gives the reason.

`tests/test_invariant_register.py` checks this file's integrity only (unique IDs, valid statuses, ENFORCED evidence
points at an existing test function, PLANNED and NOT_APPLICABLE rows have a target or reason). It does not prove that
any invariant holds. Table cells must not contain a literal pipe character.

## Register

| ID | Invariant | Status | Mechanism | Evidence / Target / Reason |
|---|---|---|---|---|
| INV-001 | Collector and API do not import each other | ENFORCED | import-linter contract `collector and api are independent` (`make imports`). Only the collector-to-api direction has a negative case. | `tests/test_import_contracts_negative.py::test_import_contracts_on_copied_package[collector_importing_api_fails_independence]` |
| INV-002 | Domain does not import collector or api | ENFORCED | import-linter contract `domain is pure` (`make imports`); AST test on the domain sources (`make test`) | `tests/test_import_contracts_negative.py::test_import_contracts_on_copied_package[domain_importing_collector_fails_domain_is_pure]`, `tests/test_domain_purity.py::test_find_violations_counts` |
| INV-003 | Domain does not import sqlite3 | ENFORCED | import-linter contract `domain is pure`; AST test on the domain sources | `tests/test_import_contracts_negative.py::test_import_contracts_on_copied_package[domain_importing_sqlite3_fails_domain_is_pure]`, `tests/test_import_contracts_negative.py::test_removing_forbidden_module_from_copied_config_makes_domain_probe_pass`, `tests/test_domain_purity.py::test_find_violations_counts` |
| INV-004 | Domain imports only the stdlib allowlist (`__future__`, `collections`, `dataclasses`, `decimal`, `enum`, `re`, `typing`) and itself, which also excludes ccxt, fastapi and talib | ENFORCED | AST test on the domain sources (`make test`); import-linter also lists ccxt, fastapi and talib as forbidden. The AST negative cases use `sqlite3`, `datetime`, `os`, collector and api imports; no case imports ccxt, fastapi or talib specifically, so those three rest on the allowlist rule. The check is direct-import only and does not follow transitive imports or computed names. | `tests/test_domain_purity.py::test_find_violations_counts` |
| INV-005 | Domain makes no I/O or dynamic-code calls (`open`, `__import__`, `eval`, `exec`, `compile`) | ENFORCED | AST test on the domain sources (`make test`). Negative cases cover `open` and `__import__` only; `eval`, `exec` and `compile` are in the forbidden set but have no case of their own. Direct calls by name only. | `tests/test_domain_purity.py::test_find_violations_counts` |
| INV-006 | Canonical market data uses exact decimals; no float for canonical values | PLANNED | none yet | S3 Task 6 (canonical decimal text, `docs/superpowers/plans/2026-10-20-slices-s1-s4.md`) |
| INV-007 | Only confirmed closed candles are persisted | PLANNED | none yet | S5 (Binance parser applies the closed-candle rule from ADR-0001), then S7 (writer) |
| INV-008 | The writer accepts only `ValidatedCandle`, and an invalid candle is not persisted | PLANNED | none yet | S4 Task 8 defines `ValidatedCandle` and `require_valid`; S7 (writer) enforces it with a test that an invalid candle is not persisted |
| INV-009 | Conflicting market data is never silently overwritten | PLANNED | none yet | S8 (conflict handling: differing duplicate rejected and recorded, never overwritten) |
| INV-010 | Missing candles, prices, volumes and indicators are never fabricated | PLANNED | none yet | S5 (parser) for candles and prices; S18 (indicators refuse to compute across gaps or with insufficient warm-up) |
| INV-011 | Fail closed when required data is missing, stale or inconsistent | PLANNED | none yet | S4 (fail-closed candle validation), S11 (coverage: present, missing, stale), S17 (readiness) |
| INV-012 | Ingestion operations are restart-safe and idempotent | PLANNED | none yet | S7 (identical duplicate is a no-op), S15 (checkpoints and restart reconcile) |
| INV-013 | API request handlers never fetch exchange data | PLANNED | none yet | S10 (`GET /v1/candles`, read-only, reads only from SQLite); no earlier check exists |
| INV-014 | API is read-only on SQLite and the Collector is the only writer | PLANNED | none yet | S10 (read-only connection), S21 (connection-level read-only for the API in Docker Compose) |
| INV-015 | Standard tests never use live exchanges or production storage | PLANNED | the `live` pytest marker is excluded by default (`-m 'not live'`), but that only excludes tests that are marked; an unmarked network test or a test touching production storage is not caught | unscheduled |
| INV-016 | No trading functionality or private trading credentials | NOT_APPLICABLE | none | Scope prohibition kept by code review and the project's non-goals, not by an automated mechanism |
| INV-017 | Accepted ADRs are not changed without explicit approval | NOT_APPLICABLE | none | Process rule that needs a human approval decision; no automated mechanism applies |
