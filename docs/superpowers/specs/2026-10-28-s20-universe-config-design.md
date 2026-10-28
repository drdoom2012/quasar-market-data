# S20 — Versioned configuration file, `GET /v1/instruments` and configured instruments in `/health/ingest` — Design

- **Status:** Final draft of revision 2 for approval at HARD STOP 1 (2026-10-28). Written after a short question round
  with the user. Revision 2 (the same day, the user's minimal review, finalised with the decision of section 11): relative paths are defined and tested (5.1, 7); the S17 fields of `/health/ingest`
  keep their meaning and the universe information is added apart from them (5.5); the runtime settings are audited
  one by one (5.7). No change to the architecture, the single TOML file, independence, restart-only or
  `GET /v1/instruments`.
- **Inputs:** spec `2026-10-20-market-data-v1-design.md` (S20, D5, D6, point 5 and 7 of section 11), architecture §8 and
  §11, contract `docs/contracts/api-v1.md` sections 6 and 6.2, follow-up F-18, `docs/invariants.md`, the S14/S17 plans,
  and the code of `collector/cli.py`, `api/cli.py`, `api/rule.py`, `api/health_reader.py`.
- **Marking:** "the user" marks what the user decided in the question round; "designer's call" marks a choice made
  without the user, which can be overturned.

## 1. Intent

Today a run is defined by command-line switches typed at start: `--symbol` (repeated), `--limit`,
`--publication-delay-ms`, `--max-concurrency`, `--min-request-interval-ms`, `--backfill-days`, `--no-backfill` for the
Collector, and `--publication-delay-ms`, `--lag-threshold-ms`, `--host`, `--port` for the API. Nothing says which universe
or which timings ran last week, and the API does not know which instruments should exist, so `/health/ingest` cannot see
an instrument whose ingestion never started (F-18).

S20 moves all of that into **one versioned file that both processes read** (the user: "I do not want to enter
parameters at start, I need it versioned"; "all operational values too, everything in one file"; "the API reads the same
file"). With it the API can answer `GET /v1/instruments` and tell `/health/ingest` which instruments are missing.

## 2. Goals

- One file in the repository defines the universe and every operational value of `quasar-collector run` and
  `quasar-api serve`; both start without switches.
- `GET /v1/instruments`: the configured universe and, per instrument, its newest stored candle (contract section 6.4,
  new).
- `GET /health/ingest` reports configured instruments that have no stored series in a separate `universe` block; every
  existing S17 field keeps its meaning (the user, and the review of revision 2).
- The Collector and the API stay independent processes: no channel between them, only the shared file (the user, and S17
  decision D6).

## 3. Non-goals

- Reloading the file while a process runs: a change takes effect after a restart (the user).
- Command-line overrides: the switches above are removed, not kept as overrides (the user).
- `fetch-once` (a one-shot diagnostic, not a service start): keeps its switches (designer's call).
- Environment-variable overrides, secrets, credentials (the project has none and must not get any).
- More than one venue or any timeframe except `5m`: the file shape allows them, the loader rejects them until S22
  (section 5.2).
- Discovering instruments from the exchange, or checking at start-up that a symbol exists there (no network call at
  start; an unserved symbol keeps today's `gave_up` handling).
- A schema change, a new table, a new ADR, any change to `/v1/candles`, `/v1/coverage` or `/v1/indicators`.

## 4. Anti-goals (failure even if it works)

- A value that is not in the file but still changes behaviour (a default hidden in code, an environment variable, a
  leftover switch). The file is the whole setting.
- The Collector and the API running with different values of the same rule without anyone being able to tell (today the
  two `--publication-delay-ms` can differ). One value, one place.
- A partly valid file that starts anyway. Fail closed: an invalid file stops the process and says why.
- Any existing S17 field of `/health/ingest` (`scope`, `status`, `series_count`, `current`, `due`, `stale`, `behind`,
  `truncated`) changing meaning, or a missing instrument turning an HTTP status into an error.
- An instrument the user removed from the file being silently deleted or still collected, or a stored series being
  dropped from the S17 counts without a word.
- A relative path that means a different file depending on where the process was started.
- A new Collector-to-API channel, or the API reaching the exchange.

## 5. Decided behaviour

### 5.1 The file

- **Format and place (designer's call).** TOML, parsed with the standard library (`tomllib`). Committed at
  `config/quasar.toml`; the database file it points to is not committed.
- **Selecting the file (designer's call).** `quasar-collector run` and `quasar-api serve` take one optional positional
  `CONFIG` path, default `config/quasar.toml` relative to the working directory. This is a location, not a parameter.
  `--version` and `--help` stay.
- **Relative paths (revision 2).** A relative `database.path` is resolved against the directory that contains the
  configuration file, never against the working directory: `base = CONFIG.resolve().parent`, then
  `base / path`, normalised. An absolute path is used as it is. The file at `config/quasar.toml` with
  `../data/quasar.db` therefore means `<repo>/data/quasar.db` wherever the process starts. The only cwd-relative
  things are the `CONFIG` argument itself and the default `config/quasar.toml` (a location, section above); once a
  file is chosen, no value depends on the working directory. The builder adds `data/` to `.gitignore` if it is not
  there.
- **Every key is required (designer's call).** The file lists every value; a missing key is an error. The code holds
  limits for validation, not defaults for behaviour, so the file always shows what ran.
- **Unknown keys are an error.** A misspelt key must not silently fall back.

```toml
[database]
path = "../data/quasar.db"         # relative to the directory of THIS file: <repo>/data/quasar.db

[rule]                              # read by both processes: one value, one place
publication_delay_ms = 15000
lag_threshold_ms = 60000

[[venues]]
id = "binance-usdm"
timeframes = ["5m"]
symbols = ["BTC/USDT:USDT", "ETH/USDT:USDT"]   # the ten instruments of spec point 7, in the committed file

[collector]
limit = 100
max_concurrency = 4
min_request_interval_ms = 100

[collector.backfill]
enabled = true
days = 30

[api]
host = "127.0.0.1"
port = 8000
```

### 5.2 Validation (fail closed)

Checked once at start by a shared loader; every bound is the one the switch had (`limit` 1..`MAX_LIMIT`, `days` 1..365,
`port` 1..65535, the API's existing bounds for `publication_delay_ms` and `lag_threshold_ms`, `max_concurrency` >= 1,
the delays >= 0).

- Version-1 limits (designer's call): exactly one venue, `id == "binance-usdm"`, `timeframes == ["5m"]`. Anything else
  is `not supported yet (S22)`, so the file shape does not have to change when Bybit arrives.
- `symbols`: non-empty, unique, each in the canonical form `BASE/QUOTE:SETTLE` with `QUOTE == SETTLE == USDT` (the
  survey filter of the S1 plan, spec point 7).
- Duplicate keys, wrong types, a missing or unreadable file, a `database.path` whose resolved directory does not
  exist: error (the message shows the resolved path).
- An invalid file ends the process with exit `2` (usage), prints the first problem with its key path, and writes
  nothing, opens no database and makes no network call (designer's call: the same code as a bad switch today).

### 5.3 Collector and API

- **Collector.** `run` takes symbols, timings, backfill and the database path from the file; behaviour is otherwise
  unchanged (S14 to S16). The committed file uses today's values (`publication_delay_ms = 15000`, the ADR-0001 P-3
  initial value, not the 5 s of spec D5), so behaviour does not change by moving to the file.
- **API.** `serve` takes the database path, the rule (`publication_delay_ms`, `lag_threshold_ms`), host and port from
  the file, and holds the configured universe in memory for the life of the process.
- **Shared code (designer's call).** A new package `quasar_market_data.config` holds the typed file contents and the
  loader. It imports only the standard library and `domain`; the Collector and the API import it. An import contract
  states this (section 8), and the existing contract "collector and api are independent" must still hold.
- **Same file, evidence.** At start each process logs the SHA-256 of the file it read and the path (designer's call), so
  a log line shows which version ran and whether both read the same one.
- **Restart only.** Neither process re-reads the file (the user).

### 5.4 `GET /v1/instruments` (new contract section 6.4, needs approval)

- The configured universe, one entry per `(venue, symbol, timeframe)`, ordered by that triple (designer's call, so the
  answer is deterministic whatever the order in the file), each with `newest_open_time_ms`: the newest stored candle's
  open time, or `null` when nothing is stored (designer's call: the time only; more fields can be added later).
- An unpaginated list: its size is bounded by the file (target 300 to 600), not by data (designer's call).
- It lists only configured instruments (the user). A stored series that is not configured is not listed.
- Same conventions as section 1 of the contract (`GET` only, `no-store`, one read transaction, `503 not_ready` when the
  database is unreadable). It reads the newest candle per configured series by primary key, never a scan.

### 5.5 `GET /health/ingest` (additive amendment of section 6.2.3, needs approval)

**Nothing existing changes.** `scope` stays `"stored_series"`; `series_count`, `current`, `due`, `stale`, `status`,
`behind` and `truncated` are computed exactly as in S17 over **all stored series**, configured or not, with the same
rules, order and limits. A pre-change request and a post-change request on the same database give the same values for
these fields. The only addition inside `behind` is a boolean `configured` on each entry (new fields may be added,
section 6.2).

**New block `universe`** (the information about the configured instruments, apart from the above):

```json
"universe": {
  "configured_count": 10, "configured_stored": 9, "unconfigured_stored": 1,
  "missing_count": 1,
  "missing": [ { "venue": "binance-usdm", "symbol": "LTC/USDT:USDT", "timeframe": "5m" } ],
  "missing_truncated": false
}
```

- `configured_count`: configured `(venue, symbol, timeframe)` triples in the file.
- `configured_stored`: configured triples with at least one row in `candles` in the read transaction.
- `unconfigured_stored`: stored series that are not in the file (retired or foreign). `series_count ==
  configured_stored + unconfigured_stored` always, and `configured_count == configured_stored + missing_count`.
- `missing`: configured triples with no row in `candles`, ordered by `(venue, symbol, timeframe)`, at most 50 (its own
  constant, equal to `HEALTH_BEHIND_MAX` today); `missing_count` is complete; `missing_truncated` is `true` when more
  exist.
- `configured` on a `behind` entry says whether that stored series is in the file, so a consumer can ignore a retired
  series without the endpoint hiding it. A retired series keeps `status` at `behind` while it is stale, exactly as
  section 6.2.3 already says; S20 does not change that.
- **What `missing` means and does not.** Zero rows for a configured series at the snapshot. It does not say why (never
  started, first write failed, a symbol the exchange does not serve, a Collector running on another file or database)
  and it does not judge time: a configured series with any row is never missing, however stale. It never changes
  `status`, any HTTP status or the order of checks of section 6.2.
- One read transaction for both the S17 fields and `universe`. Cost: one indexed existence lookup per configured series
  on top of the S17 read; `make bench-health` is rerun (section 6).

### 5.6 A removed instrument (the user)

Removed from the file, an instrument is no longer collected after the next restart. Its stored candles stay;
`/v1/candles`, `/v1/coverage` and `/v1/indicators` keep serving them exactly as today; it is absent from
`/v1/instruments`. `/health/ingest` keeps counting and judging it as in S17, marked `configured: false` and counted
in `unconfigured_stored` (decided by the user in revision 2: the earlier answer "not judged by `/health/ingest`"
is replaced by "visible and marked", because excluding it would rewrite what `series_count`, `status` and `behind`
mean; section 11). Nothing is deleted.

### 5.7 Audit of the runtime settings (revision 2)

Rule: a value goes into the file when it is an operational choice that differs between deployments or was a switch
until now. A value stays in code when it is a protocol or storage fact, a published contract number, or a tuned
constant without a measured need to vary; making it configurable would add combinations that no test covers. No new
switch and no new infrastructure is added.

| Setting (where it lives today) | In TOML? | Why |
|---|---|---|
| Database path (`--db`, both processes) | yes, `[database] path` | deployment-specific |
| Symbols (`--symbol`) | yes, `[[venues]] symbols` | the universe |
| Venue id (`VENUE_BINANCE_USDM`, `SERVED_VENUE`) | yes, `venues.id`, only `binance-usdm` accepted | identity of the data; S22 adds more |
| Timeframe (`Timeframe("5m")`, `SUPPORTED_TIMEFRAMES`, `SERVED_TIMEFRAME`) | yes, `timeframes`, only `["5m"]` accepted | ADR-0001 measured only 5m |
| `limit` (`--limit`) | yes, `[collector] limit` | switch today |
| `publication_delay_ms` (Collector and API switches) | yes, one key in `[rule]` | removes the two-values mismatch |
| `lag_threshold_ms` (`--lag-threshold-ms`) | yes, `[rule]` | switch today |
| `max_concurrency`, `min_request_interval_ms` | yes, `[collector]` | switches today |
| Backfill on/off and days (`--no-backfill`, `--backfill-days`) | yes, `[collector.backfill]` | switches today |
| API host and port (`--host`, `--port`) | yes, `[api]` | deployment-specific |
| `fetch-once` switches (`--db`, `--symbol`, `--limit`, `--publication-delay-ms`) | no, stay switches | one-shot diagnostic; its defaults (`DEFAULT_LIMIT`, `DEFAULT_PUBLICATION_DELAY_MS`) stay in code for it |
| Retry policy (`RetryPolicy`: 6 attempts, 1 s to 8 s), `shutdown_grace_ms` 10 s, `POST_CHECK_GRACE_MS` | no, code | never a switch; behaviour fixed by S14/S15 tests; promote on evidence of need |
| Time-trust limits (`DEFAULT_TIME_TRUST`) | no, code | safety limits derived from ADR-0001; a looser value weakens fail-closed |
| Backfill internals (`IDLE_RESERVE_MS`, `REQUEST_TIMEOUT_MS`, `WRITE_RESERVE_MS`, `RESCAN_CYCLES`, `MAX_BACKOFF_CYCLES`, `UNPROVEN_RETRY_CYCLES`, `OVERRUN_LIMIT`, `MAX_RUNS`), `REACH_MARGIN_SLOTS` | no, code | tuned constants of S15/S16 with their tests |
| `MAX_LIMIT` 1000 (Collector, API), API `DEFAULT_LIMIT`, `MAX_WINDOW_SLOTS`, `MAX_INDICATOR_WINDOW_SLOTS`, `HEALTH_BEHIND_MAX` and the new `missing` cap, `RECEIVED_MAX_CHARS` | no, code | exchange limit or published contract number; they are also the validation bounds of the file |
| SQLite busy timeout 5 s, `MIN_SQLITE_VERSION`, `APPLICATION_ID`, `SCHEMA_VERSION`, WAL and `query_only` settings | no, code | storage invariants (ADR-0002); the busy timeout is also stated in the contract |
| Exit codes, `PROG`, uvicorn `log_level="info"` | no, code | not operational choices today; a log level can be added when someone needs it |
| `DEFAULT_RULE`, `DEFAULT_LAG_THRESHOLD_MS` (API) | removed as runtime defaults | the loader requires the keys; a constant may stay only where a test needs a sample value |

Nothing that carries credentials exists, so there is nothing secret to keep out of the file.

## 6. Order of work and gates

1. **HARD STOP 1: contract.** Written and approved by the user before any code (CLAUDE.md: public contracts): the new
   section 6.4 (`/v1/instruments`), the additive amendment of 6.2.3 (`universe`, `configured` on `behind` entries; the
   S17 text of every existing field is left as it is), the table row in section 6, the traceability table (section 8)
   and decisions U1 to U7 in section 9: U1 one TOML file read by both processes; U2 restart-only; U3 paths resolve
   against the file's directory; U4 `/v1/instruments` shape and order; U5 the `universe` block and its arithmetic;
   U6 the S17 statistics of `/health/ingest` keep their meaning and retired series stay in them, marked
   `configured: false` (section 11); U7 version-1 limits (one venue, `5m`). The file format, the runtime
   settings of 5.7 and the switch removal are operational, but are listed in the same approval so nothing is decided
   twice. The invariants of section 8 are added to `docs/invariants.md` with the implementation.
2. The loader and its validation tests (pure parse from text; the file read separately).
3. Collector and API switch to the file; the old switches and their tests are removed; `README.md` examples updated.
4. `/v1/instruments`, then the `/health/ingest` change, with the read benchmark `make bench-health` rerun: the S17 read
   cost was accepted at 600 stored series, and the change must not make it worse (HARD STOP 2 if it does).
5. Close F-18 in `docs/follow-ups.md`, add the invariants, mark S20 in the roadmap.

## 7. Tests (what must be shown)

- The loader: the committed `config/quasar.toml` loads; each rule of 5.2 has a failing-file test with the key path in
  the message; a missing key, an unknown key, a duplicate symbol, a non-USDT symbol, a second venue, a timeframe other
  than `5m`.
- Start-up: an invalid file means exit `2`, no database opened, no network call; no switch is accepted any more.
- `/v1/instruments`: a configured instrument with candles, one without (`null`), a stored series that is not configured
  (absent), the order, an unreadable database (`503`).
- `/health/ingest`, existing fields: on one database, the S17 fields (`scope`, `status`, `series_count`, `current`,
  `due`, `stale`, `behind` apart from the new `configured`, `truncated`) equal a golden answer taken before the change,
  with and without a configured universe; the whole S17 test file passes unchanged.
- `/health/ingest`, `universe`: a configured instrument with no row is in `missing` and `status` is unchanged; a
  configured series with an old row is not missing and stays `stale`; `series_count == configured_stored +
  unconfigured_stored` and `configured_count == configured_stored + missing_count` on every fixture; a retired stale
  series stays in `behind` with `configured: false` and keeps `status` at `behind`; more than 50 missing gives 50
  entries, the complete `missing_count` and `missing_truncated: true`; all configured instruments missing gives
  `status: no_series`, `series_count: 0` and `missing_count == configured_count`; the order is by triple; an unreadable
  database is still `503`; the answer comes from one transaction (a write between the two reads is not visible).
- Paths: with an explicit `CONFIG`, the resolved database path is the same for the Collector's and the API's loader
  after the working directory is changed to an unrelated directory (and for a relative `CONFIG` given from two
  different working directories that name the same file); `../data/quasar.db` resolves against the file's directory; an
  absolute path stays absolute; a symlinked `CONFIG` resolves against the real file's directory; a missing directory is
  an error that shows the resolved path.
- The same file read by both: the API's rule equals the Collector's delay by construction (one key).
- Import contracts still pass (section 8).

## 8. Boundaries and invariants

- `quasar_market_data.config` imports no `collector`, `api`, `storage`, `ccxt`, `fastapi`, `talib` or `sqlite3`; `domain`
  does not import `config` (new import-linter contracts; the negative test file lists them).
- New invariants for the register (numbers and status follow the register's convention; each gets its enforcing test):
  INV-063 the file is the only source of operational values (no switch, hidden default or environment variable);
  INV-064 an invalid file starts nothing (no database opened, no network call); INV-065 once a `CONFIG` is given, no
  value depends on the working directory; INV-066 the S17 fields of `/health/ingest` are independent of the universe,
  and `series_count == configured_stored + unconfigured_stored`; INV-067 a stored series that is not configured is
  never deleted, never dropped from the S17 counts and never listed by `/v1/instruments`.

## 9. Left to the builder

The module and class names inside `config`; the exact wording of error messages; whether `venues` is modelled as a tuple
of frozen dataclasses; how the in-memory universe is handed to the API's readers; the shape of the start-up log line.

## 10. Risks

- **Different files, same database.** Nothing forces both processes to read the same file; the logged hash shows it but
  does not enforce it. Accepted: a channel would break the independence rule.
- **A larger universe makes a round longer than `lag_threshold_ms`.** Already noted in contract 6.2.3; putting the
  threshold in the versioned file makes the pairing visible, it does not solve it.
- **A retired series keeps `status` at `behind`.** This is S17's documented behaviour and S20 leaves it (section 11);
  the `configured: false` mark lets a consumer ignore it. Excluding retired series would be a separate contract
  change with its own approval.
- **Two sources for the same count.** `series_count` is all stored series, `configured_stored` only the configured ones;
  the contract text must give the equality of 5.5 so a reader does not mix them up.

## 11. Decision U6 (closed)

The question round had settled "a removed instrument is not judged by `/health/ingest`". That could not hold together
with keeping every S17 field's meaning, and the user decided (revision 2 review): **the original S17 statistics of
`/health/ingest` stay unchanged, and retired instruments are marked `configured: false`.**

- `scope`, `status`, `series_count`, `current`, `due`, `stale`, `behind` and `truncated` keep the S17 meaning and are
  computed over all stored series, configured or not (5.5).
- A retired stored series stays in them, appears in `behind` (while stale) with `configured: false`, and is counted in
  `universe.unconfigured_stored`. It keeps `status` at `behind` while stale, as section 6.2.3 already says.
- Nothing is excluded, nothing is deleted. Excluding retired series from the statistics would rewrite the S17 fields
  and is not part of S20; it would need its own contract change and approval.
- Carried into HARD STOP 1 as decision U6 (section 6) and into the tests of section 7 (the golden comparison of the
  S17 fields, the retired-series test, and the two equalities of 5.5).
