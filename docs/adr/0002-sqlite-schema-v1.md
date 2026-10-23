# ADR-0002: SQLite schema v1

- **Status:** Accepted (approved by the user on 2026-10-22: Proposals P-1 to P-9 as written, and Q1 to Q5 as recommended)
- **Date:** 2026-10-22
- **Scope:** the persistent schema of the Collector/API boundary for Binance USDT-M 5m OHLCV (`turnover` included), conflict records, permanently unavailable candles, and the migration mechanism.
- **Spec:** `docs/superpowers/specs/2026-10-20-market-data-v1-design.md` (D1, D4, §6.3, §6.4, §11 items 1, 3, 4, 7)
- **Related:** ADR-0001 (P-1, P-6, P-7, P-8), `docs/architecture.md` §5

This ADR is split like ADR-0001. **Part 1** lists constraints that are already decided and are not re-opened. **Part 2** holds proposals that need approval; the DDL in P-2, P-5 and P-6 is exactly what S6 would implement. **Part 3** is the evidence behind the SQLite-specific claims, from a throwaway probe. **Resolved questions** at the end record the user's answers.

## Context

SQLite WAL is the persistence boundary. The Collector is the only writer; the API reads. The schema must make the project's rules hard to break by accident: exact decimal text, no silent overwrite, no fabricated rows, fail closed on anything unexpected. SQLite has two properties that matter here. It cannot compare decimal text numerically (so exact OHLC relations cannot be a database constraint), and it is permissive by default (type coercion, `INSERT OR REPLACE`). The proposals below say what the database enforces and, separately, what it cannot.

## Part 1 — Already decided

| ID | Decision | Source |
|---|---|---|
| C-1 | Independent schema; no compatibility target with any prior contract. | spec D1 |
| C-2 | Candle identity is `(venue, symbol, timeframe, open_time_ms)`; values are canonical decimal text; `turnover` is stored only when the venue provides it, otherwise `NULL`, never computed. | architecture §5.1, spec D4, ADR-0001 |
| C-3 | A differing duplicate is rejected and recorded in a database table, never overwritten; a conflicted interval is reported as conflicted and indicators refuse to compute across it. | spec §6.3, §11.3 |
| C-4 | Coverage is computed on read from candles plus recorded unavailable intervals; it is not a stored derived table. | spec §11.4 |
| C-5 | Stored `symbol` is the CCXT unified symbol (1–64 characters, Unicode allowed); `venue` is `<exchange>-<segment>`. | ADR-0001 P-8 |
| C-6 | The API opens the database read-only at the connection level (`mode=ro`, `query_only`); the Collector is the only writer. | architecture §5.2, spec §6.4 |

## Part 2 — Proposals

### P-1 Database file, engine requirements and connection settings

One SQLite file in WAL mode on a local volume. Every table is `STRICT`. The minimum SQLite version is **3.37** (`STRICT`); a lower version is a startup error for both processes, never a silent fallback.

Writer (Collector) connection, set on every connection it opens:

| Setting | Value | Why |
|---|---|---|
| `journal_mode` | `WAL` | architecture §5.2 |
| `synchronous` | `FULL` | a committed candle survives power loss; with `NORMAL` the last commits can be lost (no corruption). Write volume is tiny, so this costs nothing that matters. |
| `recursive_triggers` | `ON` | **required** for P-4: without it `INSERT OR REPLACE` silently overwrites a row (Part 3, E-2). It is per connection and is not persisted. |
| `busy_timeout` | bounded, configurable | the single writer should rarely wait |

Reader (API) connection: `mode=ro` URI, `PRAGMA query_only=ON`, short read transactions (so WAL checkpoints are not blocked). The reader never migrates and never creates the database.

`PRAGMA application_id` is set to one constant for this project, so a Collector or API pointed at some other SQLite file refuses to start.

### P-2 `candles`

```sql
CREATE TABLE candles (
  venue          TEXT    NOT NULL CHECK (length(venue) BETWEEN 1 AND 32),
  symbol         TEXT    NOT NULL CHECK (length(symbol) BETWEEN 1 AND 64),
  timeframe      TEXT    NOT NULL CHECK (length(timeframe) BETWEEN 2 AND 3),
  open_time_ms   INTEGER NOT NULL CHECK (open_time_ms BETWEEN 946684800000 AND 4102444800000),
  open           TEXT    NOT NULL CHECK (<decimal-shape>),
  high           TEXT    NOT NULL CHECK (<decimal-shape>),
  low            TEXT    NOT NULL CHECK (<decimal-shape>),
  close          TEXT    NOT NULL CHECK (<decimal-shape>),
  volume         TEXT    NOT NULL CHECK (<decimal-shape>),
  turnover       TEXT             CHECK (turnover IS NULL OR <decimal-shape>),
  ingested_at_ms INTEGER NOT NULL,
  PRIMARY KEY (venue, symbol, timeframe, open_time_ms)
) STRICT, WITHOUT ROWID;
```

- The natural key is the table key (`WITHOUT ROWID`), so the identity is exactly the domain identity and range queries by `open_time_ms` for one series use the primary key. **No secondary index is needed in v1.**
- `ingested_at_ms` is the wall-clock time the Collector committed the row. It is diagnostic (settling analysis per ADR-0001 P-6, ingestion lag); no logic depends on it and it is excluded from the "identical duplicate" comparison. The caller supplies it; the domain still reads no clock.
- There is no `is_closed` column: only eligible, validated candles are ever inserted (INV-007, INV-008), so presence in the table is the claim.
- Only the five value columns and `turnover` of ADR-0001 P-2 are stored. Trades count and taker volumes are not (not in v1 scope).

**`<decimal-shape>`** (repeated per column) is `typeof(c) = 'text' AND length(c) BETWEEN 1 AND 97 AND c NOT GLOB '*[^0-9.]*' AND c NOT GLOB '.*' AND c NOT GLOB '*.' AND c NOT GLOB '*.*.*' AND c NOT GLOB '*.*0' AND c NOT GLOB '0[0-9]*'`. This accepts exactly the non-negative canonical form produced by `canonical_decimal` (no sign, no exponent, no leading zero, no trailing fractional zero, at most one point) and rejects everything else. Non-negative is correct for every column: prices are `> 0` and volume and turnover are `>= 0` under candle validation.

*Rejected:* a numeric column type. SQLite would store `REAL`, which is a binary float (violates C-2); `NUMERIC` affinity converts text to float.

**What the database cannot enforce, and who does:**

| Not enforced by the schema | Enforced by |
|---|---|
| OHLC relations (`high >= low`, …): text cannot be compared exactly in SQL | `require_valid` / `ValidatedCandle` in the writer (S7, INV-008) |
| Timeframe alignment of `open_time_ms`, closed-candle eligibility | domain (`Candle`, `select_closed_candles`) |
| That a float was never bound to a text column | the writer's types. SQLite converts a bound float to its text form **before** the CHECK runs (E-3), so the database cannot detect it |

A consequence: price and volume cannot be filtered or aggregated numerically in SQL. Consumers read the text and use `Decimal`; the API converts at the read boundary.

### P-3 Estimated size (for the key decision in Q1; not measured)

About 100–120 bytes per row. At the 600-instrument, 5m, unlimited-retention target that is about 63 million rows and roughly **6–7 GB per year**. The first live runs (about 10 instruments, 30 days) are about 86 000 rows.

### P-4 Candles are immutable at the database level

```sql
CREATE TRIGGER candles_no_update BEFORE UPDATE ON candles
  BEGIN SELECT RAISE(ABORT, 'candles are immutable'); END;
CREATE TRIGGER candles_no_delete BEFORE DELETE ON candles
  BEGIN SELECT RAISE(ABORT, 'candles are immutable'); END;
```

With P-1's `recursive_triggers=ON`, this makes `UPDATE`, `DELETE`, `INSERT OR REPLACE` and `ON CONFLICT DO UPDATE` abort (E-2). The writer's only legal operation on an existing identity is a read followed by an `INSERT … ON CONFLICT DO NOTHING`: identical values are a no-op, differing values are recorded by P-5. The same triggers are created for `candle_conflicts` and `unavailable_candles`. There is no retention or deletion in v1; removing rows would be a new migration with its own approval.

This is a guard against bugs, not against a person with write access to the file.

### P-5 `candle_conflicts` (record of rejected differing duplicates)

```sql
CREATE TABLE candle_conflicts (
  conflict_id      INTEGER PRIMARY KEY,
  venue            TEXT    NOT NULL,
  symbol           TEXT    NOT NULL,
  timeframe        TEXT    NOT NULL,
  open_time_ms     INTEGER NOT NULL,
  open             TEXT    NOT NULL CHECK (<decimal-shape>),
  high             TEXT    NOT NULL CHECK (<decimal-shape>),
  low              TEXT    NOT NULL CHECK (<decimal-shape>),
  close            TEXT    NOT NULL CHECK (<decimal-shape>),
  volume           TEXT    NOT NULL CHECK (<decimal-shape>),
  turnover         TEXT             CHECK (turnover IS NULL OR <decimal-shape>),
  first_seen_at_ms INTEGER NOT NULL
) STRICT;
CREATE UNIQUE INDEX candle_conflicts_observation ON candle_conflicts
  (venue, symbol, timeframe, open_time_ms, open, high, low, close, volume, coalesce(turnover, ''));
CREATE INDEX candle_conflicts_slot ON candle_conflicts (venue, symbol, timeframe, open_time_ms);
```

- A row is the **rejected** observation. The accepted one stays untouched in `candles` (immutable), so a conflict is always "stored candle vs this row", found through the identity columns. No foreign key: the identity is the link, and the trigger design never deletes either side.
- Recording is idempotent and restart-safe: `INSERT … ON CONFLICT DO NOTHING` against the unique index (wording amended 2026-10-23 with the user's approval: this ADR first said `INSERT OR IGNORE`, which also swallows CHECK violations, see `test_insert_or_ignore_would_swallow_an_invalid_conflict_record`; the idempotence behavior is unchanged). The same wrong value seen again (every recheck, every restart) adds nothing. Index uses `coalesce(turnover, '')` because SQLite treats `NULL`s as distinct in unique indexes; `''` cannot be a valid turnover (E-4).
- The row holds no reason code: the reason is always "values differ from the stored candle". Which fields differ is derivable by comparing the two rows.
- Same identity with a different value appearing a second time gives a second row; the table is the full history of distinct disagreeing observations.

### P-6 `unavailable_candles` (exchange confirmed it has no data)

```sql
CREATE TABLE unavailable_candles (
  venue          TEXT    NOT NULL,
  symbol         TEXT    NOT NULL,
  timeframe      TEXT    NOT NULL,
  open_time_ms   INTEGER NOT NULL CHECK (open_time_ms BETWEEN 946684800000 AND 4102444800000),
  reason         TEXT    NOT NULL CHECK (reason IN ('EXCHANGE_NO_DATA')),
  recorded_at_ms INTEGER NOT NULL,
  PRIMARY KEY (venue, symbol, timeframe, open_time_ms)
) STRICT, WITHOUT ROWID;
```

- **One row per missing candle slot**, not per range. This needs no overlap rules (SQLite cannot declare non-overlapping ranges), makes recording idempotent through the primary key, and makes the coverage join a plain key lookup. The cost is one row per slot, bounded by the backfill depth (30 days is 8 640 rows per instrument at most).
- `reason` has one value today. It is a closed enum so that a new kind of unavailability must be added deliberately (migration plus approval) rather than appearing as free text. What counts as "confirmed" is decided by S16 (gap detection), not here.
- A slot is never both stored and unavailable by design, but the database does not prevent it (no cross-table trigger). If it happens, P-7 reports the slot as conflicted instead of picking a winner.

### P-7 Coverage is derived on read; the precedence for the states this schema creates

No coverage table. For one slot, the states that depend on this schema are decided in this order (first match wins):

1. one or more `candle_conflicts` rows for the slot → **conflicted**
2. both a `candles` row and an `unavailable_candles` row → **conflicted** (the two records disagree)
3. a `candles` row → **present**
4. an `unavailable_candles` row → **unavailable**
5. otherwise **missing**, **stale** or **not-yet-expected**, decided by S11 from `as_of`, the publication delay and the lag threshold.

Indicators (S18) refuse to compute across any slot in state 1 or 2, and, per the spec, across missing ones. The stored candle in a conflicted slot is still returned by the candle endpoint; whether it is flagged in the response is a decision for the API contract (S10).

### P-8 Migration mechanism

```sql
CREATE TABLE schema_migrations (
  version         INTEGER PRIMARY KEY,
  name            TEXT    NOT NULL,
  checksum_sha256 TEXT    NOT NULL,
  applied_at_ms   INTEGER NOT NULL
) STRICT;
```

- Migrations are forward-only SQL, numbered from 1; version 1 creates everything in this ADR. There are no down-migrations.
- Each migration runs in one transaction together with its `schema_migrations` row, so it applies completely or not at all.
- Before doing anything the runner verifies the checksum of every already-applied migration against the shipped file. A mismatch, a gap in versions, or a database version **newer** than the code knows is a startup error (fail closed).
- **Only the Collector migrates.** The API never alters the database: at startup and in readiness it verifies `application_id` and that the applied version equals the version it was built for, and otherwise reports not ready.
- File layout, the runner's Python API and how the migrations are packaged are left to the S6 plan. Because both processes need SQLite access, S6 will probably need a shared storage package outside `domain`, `collector` and `api`; that adds a layer to the import-linter contracts and is a plan-level decision for the S6 plan, not decided here.

### P-9 Governance of later schema changes

This ADR approves schema version 1 only. Collector checkpoints (S15), health data and any later table or column are added by new migrations, each preceded by approval of an amendment or a new ADR (CLAUDE.md: no schema changes without explicit approval).

## Consequences

- S6 can be planned: it creates version 1 exactly as written, the runner, and negative tests for P-1 to P-8. The tests that matter most are the ones that must fail when a guard is removed: `INSERT OR REPLACE` with `recursive_triggers` off, a float or exponent form in a value column, a second row for the same identity, a conflict recorded twice.
- S7 and S8 use only `INSERT … ON CONFLICT DO NOTHING`, a read for comparison, and the same `ON CONFLICT DO NOTHING` form for conflicts (see the P-5 amendment note). A writer connection that forgets `recursive_triggers=ON` silently loses the P-4 guarantee, so the connection factory must set it and a test must prove it.
- An immutable `candle_conflicts` and `candles` means a conflict has **no resolution path in v1**: the slot stays conflicted and indicators keep refusing it until a later ADR defines an audited operator action (Q3).
- The natural key trades disk size for simplicity (Q1). The size is an estimate, not a measurement; the S23 soak test should measure it.
- SQLite 3.53.1 (this machine) was used for the probe; Docker images must provide at least 3.37, and the startup check makes a lower version a hard error.

## Part 3 — Evidence

Throwaway probe, run 2026-10-22 on SQLite 3.53.1 via Python 3.12 (`sqlite3` stdlib); the scripts are not in the repository. The DDL in P-2, P-4 and P-5 was executed as written.

- **E-1 DDL validity.** `STRICT, WITHOUT ROWID`, the `GLOB` shape check, both triggers and the expression-based unique index are accepted. A duplicate primary key raises `UNIQUE constraint failed`; `ON CONFLICT DO NOTHING` leaves the row untouched and reports no change.
- **E-2 Overwrite paths.** With triggers installed: `UPDATE` and `DELETE` abort; `ON CONFLICT DO UPDATE` aborts. `INSERT OR REPLACE`: aborts **with `recursive_triggers=ON`**, but **succeeds and replaces the stored value with `recursive_triggers=OFF`** (the default). This is why P-1 makes the pragma mandatory.
- **E-3 Value shape.** Rejected by the CHECK: `'1e5'`, `'-1'`, `'1.50'`, `'01'`, `'1.'`, `'.5'`, `'1.2.3'`, `'1 '`, `''`. Accepted: `'0'`, `'100'`, `'0.5'`, `NULL` turnover. **A bound Python float `0.5` was also accepted**: STRICT converts it to the text `'0.5'` before the CHECK. The schema therefore cannot detect a float that reached the writer; that guarantee lives in the writer's types (INV-006).
- **E-4 Idempotent conflict recording.** Inserting the same rejected observation twice with `NULL` turnover, and twice with `'5'`, via `INSERT OR IGNORE` leaves one row per distinct observation (`[(None, first_seen=1), ('5', first_seen=3)]`).

## Resolved questions (answered by the user on 2026-10-22: all as recommended)

1. **Q1 — Natural key.** Every table is keyed by `(venue, symbol, timeframe, open_time_ms)`; no `series` table. A later migration to one stays possible, with its own approval.
2. **Q2 — Unavailable candles per slot** (P-6), not per range.
3. **Q3 — No conflict resolution in v1.** A conflicted slot stays conflicted, and indicators keep refusing it, until a later ADR defines an audited operator action.
4. **Q4 — `synchronous=FULL`** on the writer connection (P-1).
5. **Q5 — Database-level immutability** (P-4) with the mandatory `recursive_triggers=ON` pragma on every writer connection.
