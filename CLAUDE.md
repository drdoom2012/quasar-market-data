# [CLAUDE.md](http://CLAUDE.md) — Quasar Market Data

## Project

Independent Python service for cryptocurrency market data collection, persistence, and technical indicators.

Stack: Python, CCXT, CCXT Pro, TA-Lib, FastAPI, SQLite WAL, asyncio, uv, pytest.

The service provides reliable market data to external consumers. It does not implement trading, signals, MOVR, or opportunity evaluation.

## Architecture

Two independently deployable processes:

- **Collector:** exchange integration, ingestion, normalization, validation, gap recovery, persistence.
- **API:** read-only market data access, technical indicators, REST endpoints.

SQLite WAL is the persistence boundary. The Collector is the only writer.

Read `docs/[architecture.md](http://architecture.md)` before making architectural changes.

## Non-Negotiable Rules

- Keep Collector and API operationally independent.
- Preserve strict module boundaries and dependency direction.
- Keep domain contracts independent of CCXT, FastAPI, TA-Lib, and SQLite.
- Use exact decimal representations for canonical market data. Never assume float-to-Decimal conversion restores precision.
- Persist only confirmed closed candles.
- Never silently overwrite conflicting market data.
- Never fabricate missing candles, prices, volumes, or indicators.
- Fail closed when required data is missing, stale, or inconsistent.
- All ingestion operations must be restart-safe and idempotent.
- Never fetch exchange data directly from API request handlers.
- Never introduce trading functionality or private trading credentials.
- Do not change accepted architectural decisions without explicit approval.

## Engineering Guidelines

- Prefer simple solutions over speculative abstractions.
- Follow existing project patterns before introducing new ones.
- Keep changes small, focused, and testable.
- Do not introduce unnecessary frameworks or infrastructure.
- Do not perform unrelated refactoring.
- Do not modify public contracts or database schemas without explicit approval.
- Handle errors explicitly; never hide data-quality failures.
- Keep external integrations behind adapters.
- Separate pure domain logic from I/O.
- Avoid global mutable state.

## Development Workflow

Before coding:

1. Inspect relevant code, tests, and documentation.
2. Identify the affected architectural layer.
3. For non-trivial changes, propose a concise plan.
4. Identify contract, data-integrity, and security risks.

During coding:

- Implement only the approved scope.
- Add tests for new behavior and failure cases.
- Preserve backward compatibility unless explicitly approved otherwise.
- Do not weaken validation to make tests pass.

After coding:

- Run relevant tests, linting, formatting, and type checks.
- Review architectural boundaries and error handling.
- Report what changed, what was verified, and remaining risks.
- Never claim a check passed unless it actually ran.

## Testing

- Use pytest.
- Mock exchange responses in unit tests.
- Keep live exchange tests separate from the default test suite.
- Test duplicate data, conflicts, missing intervals, retries, and recovery.
- Verify deterministic indicator calculations.
- Never use production market data storage for tests.

Use the project commands documented in [`README.md`](http://README.md) or `Makefile`. Do not invent commands that have not been configured.

## Architectural Decisions

Accepted architectural decisions are documented in `docs/adr/`.

Before proposing an architectural change:

1. Read the relevant ADR.
2. Identify new evidence or a concrete limitation.
3. Explain the trade-offs and migration impact.
4. Request approval before implementation.

Do not silently reverse accepted decisions.

## References

- `docs/[architecture.md](http://architecture.md)` — architecture and component responsibilities
- `docs/adr/` — architectural decisions
- `docs/contracts/` — data and API contracts

If a referenced document does not exist, report i
