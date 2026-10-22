# Quasar Market Data — Architecture

**Repository:** `quasar-market-data`  
**Status:** Proposed architecture — agreed foundations, with explicit open decisions  
**Scope:** Market data collection, quality assurance, storage, technical indicators and read API. Quasar Engine and SENTARIS are outside this system.

## 1. Purpose and principles

Quasar Market Data is an **independently deployable Python market-data service**. It collects exchange data, validates and persists it, repairs recoverable gaps, and exposes historical data and technical indicators to downstream consumers.

Priorities: **correctness → data integrity → reliability → simplicity → performance**.

Key invariants:

- Data collection does not depend on Quasar Engine, SENTARIS, or API availability.
- Ingestion, normalization, quality evaluation and persistence remain distinct responsibilities.
- The service never fabricates unavailable market data or silently treats incomplete coverage as complete.
- The service does not generate trading signals, MOVR states, opportunities, or orders.
- Stored data and read responses are governed by explicit, versioned contracts.

## 2. System boundary

```text
  Binance / Bybit / Bitget
         REST / WebSocket
                 |
                 v
     +------------------------+
     | Collector process      |
     |                        |
     | Exchange adapters      |
     | Universe + Scheduler   |
     | Ingestion + Backfill   |
     | Normalization          |
     | Data Quality           |
     | Storage writer         |
     +-----------+------------+
                 |
                 v
      +-----------------------+
      | SQLite database       |
      | WAL, persistent volume|
      | candles, metadata,    |
      | coverage, progress    |
      +-----------+-----------+
                  |
             read-only
                  v
     +------------------------+
     | API process            |
     | FastAPI                |
     | Read/query services    |
     | TA-Lib indicators      |
     +-----------+------------+
                 |
                 v
        External consumers
   (Quasar Engine, SENTARIS, ...)
```

Collector and API are **separate processes**, built from one repository and deployed independently. Their shared boundary is persisted data and documented contracts, not mutable in-process state.

## 3. Technology baseline

| Concern | Default |
|---|---|
| Implementation | Python |
| Exchange REST | CCXT |
| Exchange streaming | CCXT Pro, where supported |
| Technical indicators | TA-Lib |
| HTTP interface | FastAPI |
| Persistence | SQLite in WAL mode |
| Async orchestration | `asyncio` |
| Deployment | Docker Compose; separate Collector and API services |

These are implementation choices, not guarantees of exchange capabilities or data precision. Exchange support and data semantics must be verified per venue and data type.

## 4. Component responsibilities

### 4.1 Collector process

- **Exchange adapters:** isolate CCXT/CCXT Pro and exchange-specific capabilities, symbol mapping, market types, pagination and rate limits.
- **Universe manager:** hold configured venues, instruments and timeframes; distinguish venue-specific instrument identifiers from canonical identity.
- **Scheduler:** plan regular collection based on UTC candle boundaries with configurable publication delay, bounded concurrency and exchange quotas.
- **Ingestion:** request market data, process responses and coordinate persistence.
- **Normalization:** convert timestamps, identifiers and values into canonical domain models without storage or HTTP dependencies.
- **Data quality:** validate candles, detect duplicates/conflicts, gaps, staleness and incomplete coverage.
- **Backfill/recovery:** fill available historical gaps without starving current collection; persist restart-safe progress.
- **Storage writer:** perform transactional and idempotent writes and maintain coverage/ingestion metadata.

The Collector is the **single logical writer** for the database. If multiple ingestion tasks run concurrently, write operations remain coordinated through that ownership boundary.

### 4.2 API process

- **Query services:** read stored data and report completeness and freshness.
- **Indicator engine:** calculate indicators from persisted, confirmed closed candles with explicit parameters and sufficient lookback.
- **REST interface:** expose versioned, bounded queries and consistent error responses.

API handlers do not contact exchanges and do not mutate persistent collection state. Indicator caching is optional and must account for inputs, parameters and implementation version.

## 5. Data contracts and storage

### 5.1 Candle contract

The candle contract is independent and defined by this project:

- Identity: `(venue, symbol, timeframe, open_time_ms)`.
- Values: `open`, `high`, `low`, `close`, `volume`, `turnover`.
- Time: UTC milliseconds; candles aligned to their timeframe.
- Numbers: canonical decimal text rather than binary float as an authoritative persisted representation.

**Critical integration risk:** CCXT's unified OHLCV representation normally contains floating-point numbers. Converting an already rounded float to `Decimal` does **not** recover source precision. Before promising lossless decimal storage, benchmark raw exchange responses and choose a verified extraction strategy per venue. Unavailable `turnover` must not be invented.

The precise database schema is **not yet approved**; document it in a contract/ADR before migrations are implemented.

### 5.2 SQLite

- SQLite WAL on a persistent **local** volume; avoid network-shared filesystems for WAL.
- Collector owns schema/collection-state writes; API uses read-only connections.
- Explicit transactions, unique candle identity and deterministic conflict handling.
- Versioned migrations; no silent destructive schema changes.
- Short-lived read transactions so API traffic does not cause unchecked WAL growth.
- Backup/recovery procedures must account for WAL consistency.

### 5.3 Coverage semantics

Coverage is evaluated **per venue + instrument + timeframe + requested interval** and at a declared assessment time (`as_of`). It distinguishes present, missing, stale and not-yet-expected periods. Missing or unverified data must be exposed as such; the service must not imply completeness from a successful HTTP response alone.

## 6. Collection and recovery lifecycle

The initial primary OHLCV timeframe is **5m**. The target scale is roughly **300–600 instruments**, with workload varying by venue count.

1. At startup, load configuration and persisted collection checkpoints.
2. Reconcile expected closed periods with persisted periods.
3. Prioritize recent closed candles; queue recoverable historical gaps separately.
4. Apply bounded per-exchange concurrency and rate limiting.
5. Normalize and validate responses before writes.
6. Commit data and progress atomically where applicable.
7. Expose health, freshness, missing coverage and recovery status.
8. Repeat on UTC-aligned 5m boundaries after configured publication delay.

A returned candle is not automatically considered final. Closed-candle eligibility and exchange publication behavior require adapter-specific verification.

Failures must be recoverable: process restarts, timeouts, rate limiting, duplicate responses, gaps, conflicting historical candles and database contention. Historical periods unavailable from the exchange remain visible as gaps.

## 7. Streaming and additional datasets

CCXT Pro may be used for WebSocket trades, order-book updates and other supported streams. **Public liquidation coverage varies materially by exchange**; no claim of a complete liquidation tape should be made without venue-specific verification.

Streaming workers must have independent connection supervision, stale-stream detection, bounded reconnect attempts and coverage metadata. Missed WebSocket events cannot be assumed historically recoverable.

Funding rate, open interest, liquidations and order books are **extension datasets**, not part of the initial candle contract. Each needs its own identity, timestamp, units, provenance and quality semantics. A streaming failure must not compromise basic OHLCV collection.

## 8. Indicators and public API

Indicators are calculated **on demand from persisted data**, not collected from exchanges. TA-Lib provides standard functions (e.g. RSI, EMA, MACD, ATR, ADX, Bollinger Bands).

Requirements:

- Use confirmed closed candles, chronological order and sufficient warm-up history.
- Define indicator parameters and output timestamps explicitly.
- Report insufficient history or incomplete input coverage; do not silently calculate across gaps.
- Make results reproducible for the same data, parameters and library version.
- Do not persist indicator history by default; consider caching only when needed.

Proposed initial HTTP surface:

```text
GET /v1/instruments
GET /v1/candles
GET /v1/indicators
GET /v1/coverage
GET /health
```

Query limits, pagination, errors, response schemas and freshness semantics belong in a separate versioned API contract. Readiness should be distinguished from process liveness.

## 9. Operational model and security

- One repository; two independent Docker services/images or entrypoints: `collector` and `api`.
- Collector can run without API; restarting/deploying API must not restart Collector.
- Both access one local persistent SQLite volume, with API read-only at the SQLite connection level.
- Structured logs, counters and health checks for ingest lag, coverage, adapter failures, retries and backlog.
- No private trading credentials or trading functionality; public data endpoints require no trade keys.
- Bind API to localhost or a private network by default; secure any external exposure.
- Safe shutdown, checkpointing and restart recovery are required.

## 10. Verification strategy

Test separately:

- Pure normalization/decimal/alignment/coverage logic.
- Exchange parsing from captured fixture responses.
- SQLite conformance, transactions, idempotency and conflicts.
- Backfill across outages and restart boundaries.
- API read-only behavior and indicators on incomplete datasets.
- Bounded concurrency, rate limiting and prolonged collection under load.

Standard tests must not depend on live exchanges. Run separately marked exchange integration tests, followed by a multi-day soak test and inspection of coverage and WAL behavior before calling the service stable.

## 11. Decisions and open questions

### Established direction

- Python + CCXT / CCXT Pro + TA-Lib + FastAPI.
- Collector and API are separately running services.
- SQLite WAL is the persistence boundary with a single logical writer.
- Collection is independent from Quasar Engine and SENTARIS.
- Initial OHLCV cadence: 5m; indicators are computed from stored candles.
- Data quality is fail-closed, and no signals or execution belong here.

### Resolve before full implementation

1. **Decimal source fidelity:** unified CCXT data vs raw exchange payloads, including turnover semantics.
2. **Database/consumer contract:** exact schema and candle/API contract (independent schema; to be recorded in an ADR).
3. **Initial universe:** which exact venues, perpetual market types, symbol selection and scale per venue.
4. **V1 dataset scope:** OHLCV-only vs funding/OI and liquidation streams.
5. **Operational limits:** initial backfill depth, API query limits, data retention and acceptable ingestion lag.
6. **Streaming persistence:** raw event retention versus aggregated events and their coverage semantics.

Record approved choices as small ADRs. This document describes the system architecture; project-specific coding instructions belong in `CLAUDE.md`.
