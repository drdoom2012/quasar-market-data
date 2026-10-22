# Quasar Market Data v1 — Implementation Design

**Status:** Draft for approval
**Inputs:** `CLAUDE.md`, `docs/architecture.md` (accepted constraints; not redesigned here)
**Priority order:** Correctness → Data Integrity → Reliability → Simplicity → Performance

## 1. Intent

Build the independently deployable market-data service described in `docs/architecture.md`: a Collector process that ingests, validates and persists confirmed closed candles into SQLite WAL, and a read-only API process that serves candles, coverage and technical indicators. This document fixes the v1 scope, the decisions made during brainstorming, and the incremental slice roadmap. It does not restate the architecture.

The point of the service is trustworthy data: downstream consumers must be able to rely on what is stored and on explicit statements of what is missing. Speed of delivery and breadth of coverage are secondary to that.

## 2. Goals

- A working end-to-end pipeline for **one venue** (Binance USDT-M perpetuals), timeframe **5m**, before any generalization.
- Collector and API are independently executable from the first slice that has both.
- Every persisted candle is exact (decimal text), confirmed closed, and idempotently written; conflicts are never silently overwritten.
- Coverage is explicit (present / missing / stale / not-yet-expected) and indicators refuse to compute across gaps.
- Each slice is small, test-first, independently demonstrable, and reviewable in one short session.

## 3. Non-goals (v1)

- Funding rate, open interest, liquidations, order books, trades and any WebSocket/CCXT Pro streaming (extension datasets, each needs its own ADR later).
- Venues other than Binance USDT-M perpetuals until the single-venue pipeline works end to end. A second venue (Bybit) is added only then, and the adapter interface is generalized from two real examples.
- Compatibility with any external or prior candle/storage contract. The schema is independent; any adapter is a future, separate request.
- Trading, signals, MOVR, opportunity evaluation, private credentials.
- Indicator persistence; caching only if measured to be needed.

## 4. Anti-goals (failure even if it "works")

- A fabricated, interpolated or float-derived value stored as canonical data.
- A conflicting candle silently replacing an existing one.
- A response that implies completeness when coverage is partial.
- API handlers contacting an exchange or writing to the database.
- Domain code importing CCXT, FastAPI, TA-Lib or sqlite3.

## 5. Decisions made

| # | Decision |
|---|---|
| D1 | Independent database/candle schema. No reuse of, or compatibility target with, any prior contract. Recorded in ADR-0002. |
| D2 | First venue and market: Binance USDT-M perpetuals, 5m. |
| D3 | v1 dataset scope: OHLCV only. |
| D4 | `turnover` is stored only when the venue provides it; otherwise `NULL`. Never computed or estimated (e.g. not `volume × price`). The column is nullable. Whether Binance quote asset volume has the intended meaning is verified in the S2 spike and recorded in ADR-0001. |
| D5 | Starting operational values, all configuration (not code): backfill depth 30 days of 5m candles; publication delay 5 s after the UTC candle boundary; API limit 1000 candles per request with pagination; unlimited retention; ~10 instruments for first live runs, scaling toward 300–600 only after verification. |
| D6 | Tooling: Python 3.12, uv, pytest, ruff, mypy, `import-linter` (the only additional dev dependency; enforces module boundaries). Database access via stdlib `sqlite3` with a single writer, called from asyncio through a thread; no `aiosqlite`. TA-Lib, CCXT and FastAPI are added only in the slice that first needs them. TA-Lib requires the native C library (Homebrew locally, build step in Docker). |
| D7 | Detailed execution plans (`writing-plans`) are written only for S1–S4. Later slices are re-planned after the S2 spike results are known. |

## 6. Gaps from the architecture review and how they are handled

1. **Decimal fidelity** gates the schema. CCXT unified OHLCV returns floats; float → `Decimal` does not restore precision. S2 captures raw Binance kline payloads (strings) and compares them with CCXT output; the verified extraction strategy goes into ADR-0001 before the schema (S6).
2. **Closed-candle rule** is undefined in the architecture. Candidate rule to verify in S2: a candle is eligible when `open_time + timeframe <= now - publication_delay` and the exchange's in-progress last kline is excluded. The verified rule goes into ADR-0001. Time comes from an injected `Clock`; domain code never reads the wall clock.
3. **Conflict semantics.** Identical duplicate → no-op. Differing duplicate → rejected and recorded, never overwritten. Where the record lives (table vs log) is part of ADR-0002 and requires approval before S6.
4. **Read-only WAL readers.** A read-only volume mount breaks WAL readers (shared-memory file needs write access). The API enforces read-only at the SQLite connection level (`mode=ro` plus `query_only`), not by mounting the volume read-only. Addressed in S21 (Docker Compose).
5. **Smaller decisions left to the builder** (decide in the owning slice, record if architectural): canonical symbol format (suggested `BTC/USDT:USDT`), configuration file format, `Clock` abstraction shape, migration runner design.

## 7. Constraints

Everything in `CLAUDE.md` applies. In particular: schema and public-contract changes need explicit approval (hence ADR-0001 and ADR-0002 are approval gates), tests use mocked exchange responses and captured fixtures, live exchange tests are separate and excluded from the default suite, tests never touch production storage, and no check is claimed to have passed unless it actually ran. Commands are limited to those configured in the Makefile/README (S1 defines them).

## 8. Roadmap

Each slice ends green and demonstrable, is written test-first, and adds only what that slice needs.

**Foundations**
- **S1 Tooling skeleton** — uv project, `collector` and `api` entrypoints that only start and exit, Makefile (`test`, `lint`, `typecheck`, `check`), module-boundary contracts via `import-linter`.
- **S2 Fidelity spike** — throwaway script captures raw Binance klines and CCXT output; commits fixtures; produces **ADR-0001** (decimal source, closed-candle rule, turnover semantics). The script is labeled throwaway; keeping it requires a new request.

**Domain core (pure, no I/O)**
- **S3 Candle value object** — identity, canonical decimal text, UTC alignment per timeframe.
- **S4 Candle validation** — OHLC invariants; fail-closed rejection reasons.
- **S5 Binance parser** — fixtures → `Candle`, applying the closed-candle rule.

**Persistence**
- **S6 SQLite schema v1 and migration runner** — gated by approved **ADR-0002**.
- **S7 Idempotent writer** — identical duplicate is a no-op.
- **S8 Conflict handling** — differing duplicate rejected and recorded, never overwritten.

**Walking skeleton**
- **S9 Collector `fetch-once` CLI** — fetch recent closed candles for one symbol live, store them (live test marked separately).
- **S10 API `GET /v1/candles`** — read-only connection, bounded queries, consistent errors. After S10, Collector writes and API reads as separate processes.

**Correctness machinery**
- **S11 Coverage calculation** — pure; present / missing / stale / not-yet-expected for a given `as_of`.
- **S12 `GET /v1/coverage`.**
- **S13 Scheduler planner** — pure UTC boundary + publication-delay planner using the injected `Clock`.
- **S14 Collection loop** — bounded concurrency, rate limiting, safe shutdown.
- **S15 Checkpoints and restart reconcile.**
- **S16 Gap detection and backfill** — must not starve current collection.
- **S17 Health and readiness** — liveness separate from readiness.

**Indicators and operations**
- **S18 One indicator (RSI) end to end** — refuses to compute across gaps or with insufficient warm-up.
- **S19 EMA, MACD, ATR, ADX, Bollinger Bands.**
- **S20 `/v1/instruments` and universe config.**
- **S21 Docker Compose** — two services, shared local volume, connection-level read-only for the API.
- **S22 Second venue (Bybit)** — generalize the adapter interface from two real examples.
- **S23 Soak test and WAL inspection.**

Later: extension datasets, each with its own ADR.

Slices that combine independent responsibilities are split during planning.

## 9. First slice — S1 Tooling skeleton

**Goal:** a runnable, testable, boundary-enforced project containing only entrypoints.

**Scope:**
- `pyproject.toml` (uv) with dev dependencies pytest, ruff, mypy, import-linter.
- `src/quasar_market_data/{domain,collector,api}/` and `tests/`.
- `collector` and `api` entrypoints, each supporting only `--help`/`--version`.
- Makefile targets `test`, `lint`, `typecheck`, `check`; README documents them.
- Import contracts: `domain` imports nothing from `collector`, `api`, ccxt, fastapi, talib or sqlite3; `api` and `collector` never import each other.
- No TA-Lib, CCXT or FastAPI installed yet.

**Acceptance criteria:**
- `make check` is green (tests, ruff, mypy, import contracts).
- A deliberately violating import makes the boundary check fail (shown during TDD, then removed).
- Both entrypoints run independently.

**Definition of Done:** acceptance criteria verified by actually running the commands; README and Makefile document the commands; nothing outside the scope was added.

## 10. Verification expectations (all slices)

Test-first (failing test, minimal code, refactor). Each slice includes happy-path, failure-path and, where relevant, data-integrity tests (duplicates, conflicts, missing intervals, retries, restart recovery); data-integrity tests are never deferred. Each slice finishes with passing tests and a demonstrable result, reported with the commands that were run.

## 11. Builder-check resolutions (builder's calls, open to review)

These were settled without a user decision; each can be overridden.

1. **ADR flow.** ADR-0001 (S2) and ADR-0002 (S6) are drafted by the builder and approved by the user. ADR-0002 must fully specify the schema (columns, constraints, indexes, conflict-record location, how permanently unavailable intervals are represented) before S6 starts. S3–S5 do not depend on ADR-0002 and may proceed while it awaits approval; S6 is a hard stop until approval. `docs/adr/` and `docs/contracts/` are created by the first slice that needs them.
2. **If the S2 spike contradicts the design.** The first fallback stays inside the accepted CCXT baseline: CCXT's implicit raw-endpoint methods return the exchange's untouched payload. Calling Binance HTTP directly (bypassing CCXT) is not pre-approved; it would need an ADR and user approval. A failed closed-candle rule or a wrong `turnover` meaning is likewise reported to the user before S3–S5 proceed.
3. **Conflicts.** The Collector (sole writer) records a rejected conflict in a database table (shape in ADR-0002). A conflicted interval is reported by coverage as conflicted/unverified, and indicators refuse to compute across it (fail closed).
4. **Coverage and staleness.** Coverage is computed on read from candles plus recorded known-unavailable intervals, not stored as a derived table. States: present, missing, stale, not-yet-expected, conflicted, unavailable (exchange confirmed it has no data). An expected closed candle is "not yet expected" until `boundary + publication delay`, and "stale" if still absent after a configurable ingestion-lag threshold (suggested default 60 s; exact semantics fixed in the S11 plan).
5. **API contract.** A draft `docs/contracts/api-v1.md` (pagination style, error format, response shape including coverage/incompleteness) is written and approved by the user before S10. Suggested pagination: `start`/`end` open_time bounds plus a `next` cursor equal to the next open_time.
6. **Live access.** Whether Binance public endpoints are reachable from the build environment is checked at the start of S2. If not, the user is told before S9 is planned.
7. **Symbols.** Canonical symbol format is `BTC/USDT:USDT`, venue-independent, so a second venue does not change the schema. Initial ~10 instruments (builder's choice, configurable): BTC, ETH, SOL, BNB, XRP, DOGE, ADA, AVAX, LINK, LTC against USDT.
8. **Timeframes.** The domain alignment code is generic over standard timeframes, but v1 collects and tests 5m only. The `--version` source of truth is `pyproject.toml`. No separate early spike for the migration runner or the thread-based writer; both are validated in their own slices (S6, S7).

## 12. Left to the builder

Internal module layout beyond the three top-level packages, naming, configuration format, logging format, the `Clock` abstraction shape, migration runner internals, and the test fixture layout, provided the constraints above and in `CLAUDE.md` hold.
