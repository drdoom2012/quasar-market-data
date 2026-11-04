# S20 — Versioned configuration file, `GET /v1/instruments`, universe in `/health/ingest` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Both processes start from one committed TOML file (`config/quasar.toml`) instead of switches; the API serves
`GET /v1/instruments` and adds a `universe` block (plus `configured` on `behind` entries) to `/health/ingest`.

**Architecture:** A new package `quasar_market_data.config` (stdlib + `domain` only) parses and validates the file into
frozen dataclasses; `collector/cli.py` (`run`) and `api/cli.py` (`serve`) load it at start and fail closed with exit `2`.
The API receives the configured universe in memory through `create_app`. `/health/ingest` computes the universe block
in memory from the stored series it already reads in one transaction, so its SQL is unchanged.

**Tech Stack:** Python 3.12, `tomllib`, `hashlib`, argparse, FastAPI/Starlette, SQLite, pytest, import-linter.

**Spec:** `docs/superpowers/specs/2026-10-28-s20-universe-config-design.md` (read it in full; section numbers below
refer to it).

## Global Constraints

- Every key in the file is required; an unknown key is an error; the code holds validation limits, never behaviour
  defaults (spec 5.1, 5.7). No environment variable is read anywhere in `src/`.
- Bounds (spec 5.2): `collector.limit` 1..1000; `collector.backfill.days` 1..365; `api.port` 1..65535;
  `rule.publication_delay_ms` 0..300000; `rule.lag_threshold_ms` 0..900000; `collector.max_concurrency` >= 1;
  `collector.min_request_interval_ms` >= 0; plus the existing cross-key rule
  `steady_state_limit_needed(publication_delay_ms, 300_000) <= limit` (S15 Decision 9).
- Version-1 limits: exactly one `[[venues]]`, `id == "binance-usdm"`, `timeframes == ["5m"]`; anything else is
  `not supported yet (S22)`.
- A relative `database.path` resolves against `CONFIG.resolve().parent` and is normalised; an absolute one is kept.
- An invalid file: exit `2`, first problem printed with its key path, nothing written, no database opened, no network
  call.
- `quasar-collector fetch-once` keeps all its switches and defaults unchanged.
- The S17 meaning of `scope`, `status`, `series_count`, `current`, `due`, `stale`, `behind`, `truncated` does not change
  (U6). New fields only.
- `docs/invariants.md` evidence must name existing tests at every commit (`tests/test_invariant_register.py` runs in
  `make check`): a task that renames or deletes a referenced test updates the register in the same commit.
- `make check` passes at the end of every task.
- **HARD STOP 1** after Task 1 (contract approval). **HARD STOP 2** in Task 7 if the benchmark gets worse.

## Review Focus

1. **Real symbols beyond plain ASCII letters** — `1000PEPE/USDT:USDT` and a non-ASCII base (ADR-0001 P-8) must load;
   a symbol with whitespace, a missing `:USDT`, a `USDC` quote or settle, or longer than 64 characters must not.
   Tests in Task 2.
2. **TOML type confusion** — `port = true`, `limit = 100.0`, `enabled = "true"`, `days = 30.5`, `symbols = "BTC/…"`
   (a string, not an array) must each fail with the key path, never pass through Python's `bool`-is-`int`. Task 2.
3. **Broken files** — an empty file, a non-UTF-8 file, a TOML syntax error, a duplicate key, and a `CONFIG` that is a
   directory: exit `2` with a message, never a traceback. Tasks 3 and 4.
4. **Odd `database.path` values** — `""` is an error; `"~/q.db"` is not expanded (it resolves to `<config dir>/~/q.db`
   and fails on the missing directory, showing that path). Task 3.
5. **A stored series of a configured symbol under another timeframe or venue** (for example a hand-written `1m` row)
   is `unconfigured_stored`, and the configured `5m` triple is `missing`; both equalities still hold. Task 7.

---

### Task 1: Contract amendment (HARD STOP 1)

**Files:**
- Modify: `docs/contracts/api-v1.md` (section 6 table row, 6.1.5, 6.2.3, new 6.4, section 8, section 9)

- [ ] **Step 1: Write the contract changes**

  - Section 6 table: the `GET /v1/instruments` row says "Specified in section 6.4 below."
  - New **6.4 `GET /v1/instruments`**: no query parameters (any parameter is `400 invalid_parameter`, as for the
    health paths; order of checks: query → database); body
    `{"instruments":[{"venue","symbol","timeframe","newest_open_time_ms"}]}` with one entry per configured triple,
    ordered by `(venue, symbol, timeframe)`, `newest_open_time_ms` `null` when nothing is stored; only configured
    instruments; unpaginated (bounded by the file); section 1 conventions; one read transaction; `503 not_ready` as in
    section 2; the universe is read from the file at start, a change needs a restart.
  - **6.2.3 additive amendment**: the example body gains `"configured": true` on the `behind` entry and the
    `universe` block of spec 5.5 after `truncated`; new bullets for `universe` (`configured_count`,
    `configured_stored`, `unconfigured_stored`, `missing_count`, `missing` ≤ 50 ordered by triple, `missing_truncated`),
    both equalities verbatim (`series_count == configured_stored + unconfigured_stored`,
    `configured_count == configured_stored + missing_count`), what `missing` does and does not mean (spec 5.5), and
    `configured` on `behind` entries. Append to the `scope` bullet: "Since S20 the separate `universe` block lists
    configured instruments that have no stored series; `scope` and the counts are unchanged." Leave every other
    sentence of the existing bullets as is, except that removed switch names become file keys
    (`--lag-threshold-ms` → `rule.lag_threshold_ms` in `config/quasar.toml`).
  - **6.1.5**: the rule comes from `[rule]` of the configuration file, read by both processes; same bounds; an
    out-of-bound value stops the process at start with exit `2`.
  - **Section 8**: rows for `/v1/instruments` and the `universe` block → S20 design, U4, U5, INV-066, INV-067.
  - **Section 9**: decisions U1–U7 exactly as listed in spec section 6 step 1, plus one line stating the file format,
    the 5.7 audit and the switch removal are part of the same approval.

- [ ] **Step 2: Commit**

```bash
git add docs/contracts/api-v1.md
git commit -m "docs: contract 6.4 /v1/instruments and the universe block of /health/ingest (S20, for approval)"
```

- [ ] **Step 3: STOP.** Show the user the diff and wait for explicit approval of U1–U7. Do not start Task 2 before it.

---

### Task 2: `config` package — pure parse and validation

**Files:**
- Create: `src/quasar_market_data/config/__init__.py` (empty), `src/quasar_market_data/config/settings.py`
- Create: `tests/config_files.py` (helper), `tests/config/__init__.py`, `tests/config/test_parse.py`

**Interfaces:**
- Produces (in `quasar_market_data.config.settings`):
  - `SUPPORTED_VENUE: Final = "binance-usdm"`, `SUPPORTED_TIMEFRAMES: Final = ("5m",)`, `MAX_LIMIT: Final = 1000`,
    `MAX_BACKFILL_DAYS: Final = 365`, `MAX_PUBLICATION_DELAY_MS: Final = 300_000`, `MAX_LAG_THRESHOLD_MS: Final = 900_000`,
    `MAX_SYMBOL_CHARS: Final = 64`
  - `class ConfigError(ValueError)`: `__init__(self, key: str, problem: str)`, attributes `key`, `problem`,
    `str(e) == f"{key}: {problem}"`
  - frozen dataclasses: `Instrument(venue: str, symbol: str, timeframe: str)` with `order=True`;
    `Rule(publication_delay_ms: int, lag_threshold_ms: int)`;
    `Venue(id: str, timeframes: tuple[str, ...], symbols: tuple[str, ...])`;
    `Backfill(enabled: bool, days: int)`;
    `CollectorSettings(limit: int, max_concurrency: int, min_request_interval_ms: int, backfill: Backfill)`;
    `ApiSettings(host: str, port: int)`;
    `Settings(database_path: Path, rule: Rule, venues: tuple[Venue, ...], collector: CollectorSettings, api: ApiSettings)`
    with method `instruments(self) -> tuple[Instrument, ...]` (every venue × timeframe × symbol, sorted)
  - `parse_settings(text: str, *, base_dir: Path) -> Settings` — no I/O; `base_dir` must be absolute
- Produces (in `tests/config_files.py`):
  - `TEN_SYMBOLS: tuple[str, ...]` — BTC, ETH, SOL, BNB, XRP, DOGE, ADA, AVAX, LINK, LTC, each `X/USDT:USDT`
  - `config_text(*, database_path: str = "market.db", symbols: Sequence[str] = ("BTC/USDT:USDT", "ETH/USDT:USDT"), publication_delay_ms: int = 15_000, lag_threshold_ms: int = 60_000, limit: int = 100, max_concurrency: int = 4, min_request_interval_ms: int = 100, backfill_enabled: bool = True, backfill_days: int = 30, host: str = "127.0.0.1", port: int = 8000) -> str`
    — the TOML of spec 5.1 with those values; strings and the symbol array written with `json.dumps` (valid TOML basic
    strings)
  - `write_config(directory: Path, name: str = "quasar.toml", **values: Any) -> Path`

- [ ] **Step 1: Write the failing tests** in `tests/config/test_parse.py` (`BASE = Path("/cfg")`, `VALID = config_text()`)

```python
def test_valid_text_parses_to_every_value() -> None:
    s = parse_settings(VALID, base_dir=BASE)
    assert s.database_path == Path("/cfg/market.db")
    assert s.rule == Rule(15_000, 60_000)
    assert s.venues == (Venue("binance-usdm", ("5m",), ("BTC/USDT:USDT", "ETH/USDT:USDT")),)
    assert s.collector == CollectorSettings(100, 4, 100, Backfill(True, 30))
    assert s.api == ApiSettings("127.0.0.1", 8000)


def test_instruments_are_sorted_by_triple() -> None:
    s = parse_settings(config_text(symbols=("ETH/USDT:USDT", "BTC/USDT:USDT")), base_dir=BASE)
    assert s.instruments() == (
        Instrument("binance-usdm", "BTC/USDT:USDT", "5m"),
        Instrument("binance-usdm", "ETH/USDT:USDT", "5m"),
    )


@pytest.mark.parametrize(
    "key, line", LEAF_LINES
)  # one case per leaf key: ("rule.lag_threshold_ms", "lag_threshold_ms = 60000\n"), …
def test_every_key_is_required(key: str, line: str) -> None:
    with pytest.raises(ConfigError) as e:
        parse_settings(VALID.replace(line, "", 1), base_dir=BASE)
    assert e.value.key == key and "missing" in e.value.problem


@pytest.mark.parametrize("table", ["database", "rule", "collector", "collector.backfill", "api"])
def test_a_missing_table_names_it(table: str) -> None: ...  # drop the whole table → e.value.key == table


@pytest.mark.parametrize("where, key", [("[rule]", "rule.extra"), ("[api]", "api.extra"), ("", "extra"), ...])
def test_an_unknown_key_is_an_error(where: str, key: str) -> None: ...  # insert "extra = 1" → e.value.key == key


@pytest.mark.parametrize(
    "values, key",
    [
        ({"limit": 0}, "collector.limit"),
        ({"limit": 1001}, "collector.limit"),
        ({"backfill_days": 0}, "collector.backfill.days"),
        ({"backfill_days": 366}, "collector.backfill.days"),
        ({"port": 0}, "api.port"),
        ({"port": 65536}, "api.port"),
        ({"publication_delay_ms": -1}, "rule.publication_delay_ms"),
        ({"publication_delay_ms": 300_001}, "rule.publication_delay_ms"),
        ({"lag_threshold_ms": -1}, "rule.lag_threshold_ms"),
        ({"lag_threshold_ms": 900_001}, "rule.lag_threshold_ms"),
        ({"max_concurrency": 0}, "collector.max_concurrency"),
        ({"min_request_interval_ms": -1}, "collector.min_request_interval_ms"),
        ({"host": ""}, "api.host"),
    ],
)
def test_out_of_bound_values(values: dict[str, Any], key: str) -> None: ...


def test_the_bounds_themselves_are_valid() -> None:
    ...  # limit 1000, days 1 and 365, port 1 and 65535, lag 0 and 900_000,
    # delay 0 and 300_000 each with limit = needed(delay) (below)


def needed(delay: int) -> int:
    return steady_state_limit_needed(delay, Timeframe("5m").milliseconds)  # never a hand-picked number


@pytest.mark.parametrize("delay", [0, 1, 15_000, 299_999, 300_000])  # 0 and 1 straddle a step of the formula
def test_the_smallest_limit_a_delay_needs_is_accepted_and_one_less_is_refused(delay: int) -> None:
    parse_settings(config_text(publication_delay_ms=delay, limit=needed(delay)), base_dir=BASE)  # accepted
    with pytest.raises(ConfigError) as e:
        parse_settings(config_text(publication_delay_ms=delay, limit=needed(delay) - 1), base_dir=BASE)
    assert e.value.key == "collector.limit" and f"needs limit >= {needed(delay)}" in e.value.problem


def test_the_largest_delay_with_the_largest_limit_is_accepted() -> None: ...  # delay 300_000, limit 1000
def test_needed_grows_with_the_delay() -> None:
    assert needed(0) < needed(1) and needed(1) <= needed(300_000) <= 1000  # pins the shape the cases above rely on


@pytest.mark.parametrize(
    "old, new, key",
    [  # Review Focus 2
        ("port = 8000", "port = true", "api.port"),
        ("limit = 100", "limit = 100.0", "collector.limit"),
        ("enabled = true", 'enabled = "true"', "collector.backfill.enabled"),
        ("days = 30", "days = 30.5", "collector.backfill.days"),
        ("enabled = true", "enabled = 1", "collector.backfill.enabled"),
        ('host = "127.0.0.1"', "host = 1", "api.host"),
    ],
)
def test_wrong_types(old: str, new: str, key: str) -> None: ...


def test_symbols_must_be_a_non_empty_array_without_duplicates() -> (
    None
): ...  # (), "BTC/USDT:USDT" as a string, a duplicate → key "venues[0].symbols"


@pytest.mark.parametrize("symbol", ["1000PEPE/USDT:USDT", "我踏马来了/USDT:USDT", "BTC/USDT:USDT"])
def test_canonical_usdt_symbols_load(symbol: str) -> None: ...  # Review Focus 1


@pytest.mark.parametrize(
    "symbol",
    [
        "BTC/USDT",
        "BTC/USDC:USDC",
        "BTC/USDT:USDC",
        "BTC USDT/USDT:USDT",
        "BTC/USDT:USDT ",
        "/USDT:USDT",
        "A" * 55 + "/USDT:USDT",
        "BTC/USDT:USDT\n",
    ],
)
def test_non_canonical_symbols_are_refused(symbol: str) -> None: ...  # key "venues[0].symbols[0]"


def test_a_second_venue_is_not_supported_yet() -> (
    None
): ...  # append a second [[venues]] → "not supported yet (S22)" in problem, key "venues"
def test_zero_venues_is_an_error() -> None: ...  # "venues = []" in place of the table → key "venues"
@pytest.mark.parametrize("venue", ["bybit", "Binance-USDM"])
def test_another_venue_id_is_not_supported_yet(venue: str) -> None: ...  # key "venues[0].id", "S22" in problem
@pytest.mark.parametrize("timeframes", ['["1m"]', '["5m", "1m"]', '["5m", "5m"]', "[]"])
def test_timeframes_other_than_5m_are_not_supported_yet(timeframes: str) -> None: ...  # key "venues[0].timeframes"


def test_a_duplicate_key_and_a_syntax_error_are_config_errors() -> (
    None
): ...  # tomllib.TOMLDecodeError → ConfigError, key "file"
def test_empty_text_reports_the_first_missing_table() -> None: ...  # "" → ConfigError, key "database"


@pytest.mark.parametrize(
    "path, expected",
    [
        ("../data/q.db", "/data/q.db"),
        ("a/./b/../q.db", "/cfg/a/q.db"),
        ("/abs/q.db", "/abs/q.db"),
        ("~/q.db", "/cfg/~/q.db"),
    ],
)
def test_database_path_is_resolved_against_base_dir(path: str, expected: str) -> None: ...  # Review Focus 4
def test_an_empty_database_path_is_an_error() -> None: ...  # key "database.path"


def test_bounds_equal_the_switch_bounds_they_replace() -> None:
    assert settings.MAX_LIMIT == fetch_once.MAX_LIMIT
    assert settings.MAX_PUBLICATION_DELAY_MS == rule.MAX_PUBLICATION_DELAY_MS
    assert settings.MAX_LAG_THRESHOLD_MS == rule.MAX_LAG_THRESHOLD_MS
    assert settings.SUPPORTED_VENUE == binance_klines.VENUE_BINANCE_USDM
```

- [ ] **Step 2: Run** `uv run pytest tests/config/test_parse.py -q` — Expected: FAIL (module not found).

- [ ] **Step 3: Implement `parse_settings` in `src/quasar_market_data/config/settings.py`**

  `tomllib.loads`, then walk the expected tree with one small helper per kind (`_table`, `_int`, `_bool`, `_str`,
  `_str_array`) that takes the key path and raises `ConfigError(path, problem)`; check unknown keys per table before
  reading values, missing keys in file order. `_int` rejects `bool`. Symbol rule:
  `re.fullmatch(r"[^/:\s\x00-\x1f\x7f]+/USDT:USDT", s)` and `len(s) <= MAX_SYMBOL_CHARS`. Database path:
  `Path(os.path.normpath(base_dir / raw))` (an absolute `raw` replaces `base_dir` by `/`'s semantics); no `~`
  expansion. The cross-key check uses `domain.reconcile.steady_state_limit_needed` and
  `domain.timeframe.Timeframe("5m").milliseconds`, error key `collector.limit`, problem
  `f"publication_delay_ms {d} needs limit >= {needed}"`.

- [ ] **Step 4: Run** `uv run pytest tests/config -q` — Expected: PASS.

- [ ] **Step 5: Commit** — `git add src/quasar_market_data/config tests/config tests/config_files.py` and
  `git commit -m "feat: config package: parse and validate the S20 settings file"`

---

### Task 3: Loading the file, paths, committed file, import contracts

**Files:**
- Modify: `src/quasar_market_data/config/settings.py`
- Create: `config/quasar.toml`, `data/.gitkeep`
- Modify: `.gitignore` (add `/data/*` and `!/data/.gitkeep`), `pyproject.toml` (import-linter), 
  `tests/test_import_contracts_negative.py`
- Create: `tests/config/test_load.py`

**Interfaces:**
- Consumes: `parse_settings`, `ConfigError`, `Settings` (Task 2)
- Produces:
  - `DEFAULT_CONFIG_PATH: Final = Path("config/quasar.toml")`
  - frozen `LoadedSettings(path: Path, sha256: str, settings: Settings)` with `start_line(self) -> str` returning
    `f"config path={path} sha256={sha256} database={settings.database_path}"`
  - `load_settings(path: Path) -> LoadedSettings` — `path.resolve()`, read bytes once, `sha256` of those bytes,
    decode UTF-8, `parse_settings(text, base_dir=resolved.parent)`, then require `database_path.parent.is_dir()`
    (`ConfigError("database.path", f"directory {parent} does not exist")`). `OSError` (missing, unreadable, a
    directory) and `UnicodeDecodeError` become `ConfigError("file", …)` with the resolved path in the problem.

- [ ] **Step 1: Write the failing tests** in `tests/config/test_load.py`

```python
def test_the_committed_file_loads() -> None:
    loaded = load_settings(REPO_ROOT / "config" / "quasar.toml")
    s = loaded.settings
    assert s.database_path == REPO_ROOT / "data" / "quasar.db"
    assert s.venues[0].symbols == TEN_SYMBOLS
    assert (s.rule, s.collector, s.api) == (
        Rule(15_000, 60_000),
        CollectorSettings(100, 4, 100, Backfill(True, 30)),
        ApiSettings("127.0.0.1", 8000),
    )  # today's switch values (spec 5.3)


def test_the_hash_is_of_the_bytes_read(tmp_path: Path) -> None: ...  # == hashlib.sha256(path.read_bytes()).hexdigest()


def test_the_resolved_database_path_does_not_depend_on_the_working_directory(tmp_path, monkeypatch) -> None:
    cfg = write_config(tmp_path / "conf", database_path="../data/q.db")
    (tmp_path / "data").mkdir()
    seen = set()
    for cwd in (tmp_path, tmp_path / "conf", tmp_path / "elsewhere"):  # mkdir elsewhere first
        monkeypatch.chdir(cwd)
        seen.add(load_settings(cfg).settings.database_path)
    assert seen == {tmp_path / "data" / "q.db"}


def test_a_relative_config_from_two_working_directories_names_the_same_database(tmp_path, monkeypatch) -> None:
    ...
    # chdir(tmp_path) + Path("conf/quasar.toml"), chdir(tmp_path/"conf") + Path("quasar.toml") → equal database_path


def test_a_symlinked_config_resolves_against_the_real_files_directory(tmp_path: Path) -> None:
    ...
    # real file in tmp_path/real with "q.db"; symlink tmp_path/link/quasar.toml → database_path == tmp_path/"real"/"q.db"


def test_an_absolute_database_path_stays_absolute(tmp_path: Path) -> None: ...


def test_a_missing_database_directory_shows_the_resolved_path(tmp_path: Path) -> None:
    cfg = write_config(tmp_path, database_path="nope/q.db")
    with pytest.raises(ConfigError) as e:
        load_settings(cfg)
    assert e.value.key == "database.path" and str(tmp_path / "nope") in e.value.problem


@pytest.mark.parametrize("make", [missing_file, a_directory, non_utf8_bytes])  # Review Focus 3
def test_unreadable_files_are_config_errors(tmp_path: Path, make: Callable[[Path], Path]) -> None: ...  # key "file"


def test_start_line_names_path_hash_and_database(tmp_path: Path) -> None: ...
```

  Import contracts, in `tests/test_import_contracts_negative.py`: add `CONFIG_CONTRACT = "config depends only on
  domain and the standard library"` to `CONTRACT_NAMES`, and cases `config_importing_collector…`, `…_api…`,
  `…_storage…`, `config_importing_sqlite3…`, `config_importing_fastapi…` (→ `CONFIG_CONTRACT`),
  `domain_importing_config_fails_domain_is_pure` (→ `DOMAIN_CONTRACT`), `config_importing_domain_is_allowed`,
  `api_importing_config_is_allowed`, `collector_importing_config_is_allowed` (→ `None`).

- [ ] **Step 2: Run** `uv run pytest tests/config tests/test_import_contracts_negative.py -q` — Expected: FAIL.

- [ ] **Step 3: Implement** `load_settings`, `LoadedSettings`, `DEFAULT_CONFIG_PATH`; write `config/quasar.toml`
  (spec 5.1 shape, `path = "../data/quasar.db"`, the ten symbols, today's values, a comment that relative paths
  resolve against this file's directory and that changes need a restart); `data/.gitkeep` and the `.gitignore` lines
  (so the committed file loads in a fresh clone — the spec's "add `data/` to `.gitignore`" with the directory kept);
  in `pyproject.toml` add `"quasar_market_data.config"` to the forbidden list of "domain is pure" and a new forbidden
  contract named exactly `CONFIG_CONTRACT` with source `quasar_market_data.config` and forbidden
  `quasar_market_data.collector`, `quasar_market_data.api`, `quasar_market_data.storage`, `ccxt`, `fastapi`, `talib`,
  `sqlite3`.

- [ ] **Step 4: Run** `make check` — Expected: PASS (all contracts kept, including "collector and api are independent").

- [ ] **Step 5: Commit** — `git commit -m "feat: load config/quasar.toml with paths relative to the file (S20)"`

---

### Task 4: Collector `run` reads the file

**Files:**
- Modify: `src/quasar_market_data/collector/cli.py`
- Modify: `tests/collector/test_cli_run.py`, `tests/collector/test_cli_backfill.py`,
  `tests/collector/test_reconcile_run.py`, `docs/invariants.md` (evidence names only, if any change)

**Interfaces:**
- Consumes: `load_settings`, `LoadedSettings`, `ConfigError`, `DEFAULT_CONFIG_PATH` (Task 3); `write_config` (Task 2)
- Produces: `run` subcommand = `run [CONFIG]` (one optional positional, default `DEFAULT_CONFIG_PATH`);
  `_run_command(settings: Settings, open_source: OpenSource, clock: Clock, sleep: Sleep) -> int`

- [ ] **Step 1: Convert the test helpers, then write the new failing tests**

  - `test_cli_run.argv(db, **values)` → `["run", str(write_config(db.parent, database_path=db.name,
    symbols=(SYMBOL,), max_concurrency=1, backfill_enabled=False, **values))]`; `run_cli(…, **values)`. Call sites
    move from switch strings to keywords (`"--min-request-interval-ms", "200"` → `min_request_interval_ms=200`).
    `round_lines` also drops lines starting with `"config "`; the "pre-start read comes first" assertion checks the
    line after the `config` line.
  - `test_reconcile_run.invoke(db, capsys, *, limit=10…, publication_delay_ms=…, …)` likewise; every symbol any test
    passes must be canonical (`X/USDT:USDT`). `test_a_delay_no_round_can_serve_is_a_usage_error` (INV-052 evidence,
    keep the name): `publication_delay_ms=300_000, limit=<needed - 1>` exits `2` with `"needs limit >= <needed>"`
    on stderr, nothing opened, no db; then `limit=<needed>` runs and `run.limits[0] == <needed>` (compute `needed`
    with `steady_state_limit_needed(300_000, TF)` in the test). The 700 000 ms test exceeds the new 300 000 bound:
    keep it at `publication_delay_ms=300_000` and recompute `target` from `T + tf + delay <= as_of` with the test's
    clock, keeping its assertions' meaning.
  - `test_cli_backfill.run_cli`: config from `env.db`, symbol `SYMBOL = "BTC/USDT:USDT"` (pass it to `Env(…,
    symbols=(SYMBOL,))`, `env.store(…, symbol=SYMBOL)`, `env.stored(SYMBOL)`), `backfill_days=1` /
    `backfill_enabled=False` instead of switches; delete `test_backfill_days_is_validated` (covered in Task 2).
  - Replace `test_usage_errors_exit_2_and_open_nothing` with config values (`symbols=(SYMBOL, SYMBOL)`,
    `max_concurrency=0`, `min_request_interval_ms=-1`, `limit=0`,
    `limit=steady_state_limit_needed(15_000, TF) - 1`, `publication_delay_ms=300_001`); keep
    `opened == [] and not db.exists()`.
  - `test_a_missing_database_directory_exits_1_and_opens_nothing` becomes `…_exits_2_…` (the loader refuses it).

  New tests in `test_cli_run.py`:

```python
@pytest.mark.parametrize("switch", [["--db", "x.db"], ["--symbol", SYMBOL], ["--limit", "100"],
    ["--publication-delay-ms", "15000"], ["--max-concurrency", "1"], ["--min-request-interval-ms", "1"],
    ["--backfill-days", "3"], ["--no-backfill"]])
def test_no_switch_is_accepted_any_more(db: Path, switch: list[str]) -> None:   # INV-063
    with pytest.raises(SystemExit) as raised: main(["run", str(write_config(db.parent)), *switch])
    assert raised.value.code == 2

@pytest.mark.parametrize("text", ["", "not toml [", "[database]\npath = 1\n"])
def test_an_invalid_file_exits_2_and_opens_nothing(tmp_path, text, capsys) -> None:   # INV-064
    # write text to tmp_path/"quasar.toml"; opened == [], no *.db in tmp_path, stderr names the key path, no "Traceback"

def test_a_config_that_is_a_directory_exits_2(tmp_path: Path) -> None: ...   # Review Focus 3

def test_run_prints_the_config_line_first(db, capsys) -> None:   # first stdout line == loaded.start_line()

def test_the_default_config_path_is_relative_to_the_working_directory(tmp_path, monkeypatch) -> None:
    # chdir(tmp_path); write tmp_path/"config"/"quasar.toml"; main(["run"], …) with a stop-on-sleep runs and uses it

def test_the_database_path_does_not_depend_on_the_working_directory(tmp_path, monkeypatch) -> None:   # INV-065
    # spy on cli._run_command; run main(["run", str(cfg)]) from two different cwds; both see the same database_path

def test_a_removed_symbol_keeps_its_rows(db, capsys) -> None:   # INV-067, collector side
    # seed ETH rows; run with symbols=(SYMBOL,) only; ETH row count and values unchanged, no ETH request made
```

- [ ] **Step 2: Run** `uv run pytest tests/collector/test_cli_run.py tests/collector/test_cli_backfill.py tests/collector/test_reconcile_run.py -q` — Expected: FAIL.

- [ ] **Step 3: Implement in `collector/cli.py`**

  `run` parser: only the positional `config` (`nargs="?"`, `type=Path`, `default=DEFAULT_CONFIG_PATH`). In `main`:
  `load_settings` → on `ConfigError` `parser.error(f"config {args.config}: {error}")`; print `loaded.start_line()`
  (stdout, `flush=True`); `_run_command(loaded.settings, …)` builds `LoopConfig` from `venues[0]` (symbols in file
  order, `Timeframe(venues[0].timeframes[0])`), `rule.publication_delay_ms`, `collector.*`; `BackfillConfig(days)`
  only when `enabled`. Remove the duplicate-symbol and steady-state checks from `main` (the loader owns them), `_positive`,
  `_backfill_days`, `DEFAULT_CONCURRENCY`, `DEFAULT_REQUEST_INTERVAL_MS`, `DEFAULT_BACKFILL_DAYS`. `fetch-once`,
  `_limit`, `_delay`, `DEFAULT_LIMIT` stay. Update the module docstring (`run [CONFIG]`, exit `2` covers an invalid
  file, no `--no-backfill`/`--backfill-days`).

- [ ] **Step 4: Run** `make check` — Expected: PASS (including the register test).

- [ ] **Step 5: Commit** — `git commit -m "feat: quasar-collector run reads config/quasar.toml instead of switches (S20)"`

---

### Task 5: API `serve` reads the file; `create_app` takes rule and universe

**Files:**
- Modify: `src/quasar_market_data/api/cli.py`, `src/quasar_market_data/api/app.py`, `src/quasar_market_data/api/rule.py`
- Modify: `tests/api/seed.py`, `tests/api/test_cli_serve.py`, `tests/api/test_rule.py`, every `create_app(` caller
  (`tests/api/test_app.py`, `test_health_endpoints.py`, `test_coverage_endpoint.py`, `test_indicators_endpoint.py`,
  `test_read_only.py`, `bench_health.py`, `bench_indicators.py`), `tests/test_api_process.py`, `docs/invariants.md`
  (evidence names, if any change)

**Interfaces:**
- Consumes: `load_settings`, `ConfigError`, `DEFAULT_CONFIG_PATH`, `Instrument` (Tasks 2–3); `write_config`
- Produces:
  - `create_app(db_path: Path, *, rule: ExpectationRule, universe: Sequence[Instrument], open_connection: ConnectionFactory = open_reader, now_ms: Callable[[], int] = system_now_ms) -> FastAPI`
    — `rule` and `universe` have no default; the app keeps `tuple(sorted(universe))`
  - `serve [CONFIG]`; `api/rule.py` loses `DEFAULT_RULE` (no runtime default)
  - `tests/api/seed.py`: `SAMPLE_RULE = ExpectationRule(15_000, 60_000)`,
    `SAMPLE_UNIVERSE: tuple[Instrument, ...] = (Instrument(VENUE, SYMBOL, TIMEFRAME),)`

- [ ] **Step 1: Convert callers, then write the failing tests**

  Every test `create_app(…)` call passes `rule=` (`SAMPLE_RULE` where it relied on the default) and
  `universe=SAMPLE_UNIVERSE` (or `()`); `DEFAULT_RULE` imports become `SAMPLE_RULE` from `tests.api.seed`.
  `test_rule.py` drops its `DEFAULT_RULE` assertions and keeps the bound checks. `test_api_process.launch` and
  `bench_indicators` start `quasar-api serve <cfg>` with `write_config(tmp_dir, database_path=db.name, host="127.0.0.1",
  port=port)`.

  Rewrite `tests/api/test_cli_serve.py` around `cfg = write_config(tmp_path, database_path="m.db", …)`:

```python
def test_serve_binds_host_and_port_from_the_file(tmp_path, run_calls) -> None: ...   # host "10.0.0.5", port 9001
def test_serve_passes_database_rule_and_universe_to_the_app(tmp_path, run_calls, monkeypatch) -> None:
    # recorder on cli.create_app; == (tmp_path/"m.db", ExpectationRule(1000, 2000), sorted Instruments of the file)
def test_serve_does_not_create_the_database(tmp_path, run_calls) -> None: ...   # m.db absent after start; tmp_path holds only the config
def test_serve_refuses_an_unsupported_sqlite(…) -> None: ...                       # unchanged meaning, exit 1
@pytest.mark.parametrize("key, bound", [("publication_delay_ms", MAX_PUBLICATION_DELAY_MS), ("lag_threshold_ms", MAX_LAG_THRESHOLD_MS)])
def test_an_out_of_bound_value_exits_2_and_never_binds(…) -> None: ...   # keep this name (INV-046): bound → 0, bound + 1 → SystemExit 2, run_calls == [], "rule.<key>" in stderr
@pytest.mark.parametrize("switch", [["--db", "m.db"], ["--host", "x"], ["--port", "1"],
                                    ["--publication-delay-ms", "1"], ["--lag-threshold-ms", "1"]])
def test_no_switch_is_accepted_any_more(tmp_path, switch) -> None: ...   # INV-063, exit 2
def test_an_invalid_file_exits_2_and_never_binds(tmp_path, run_calls, monkeypatch) -> None: ...   # INV-064: create_app never called
def test_serve_logs_the_config_line(tmp_path, run_calls, capsys) -> None: ...   # loaded.start_line() on stderr
def test_the_database_path_does_not_depend_on_the_working_directory(tmp_path, run_calls, monkeypatch) -> None: ...   # INV-065
def test_the_api_rule_equals_the_collector_delay_from_the_same_file(tmp_path, monkeypatch, run_calls) -> None:
    # one cfg; API: recorded rule.publication_delay_ms; Collector: spy on collector cli._run_command → settings.rule.publication_delay_ms; equal
```

  (The last test lives in `tests/test_entrypoints.py`, which already imports both CLIs; `tests/api` must not import the
  collector.)

- [ ] **Step 2: Run** `uv run pytest tests/api/test_cli_serve.py tests/test_entrypoints.py -q` — Expected: FAIL.

- [ ] **Step 3: Implement**

  `api/cli.py`: `serve` takes only the positional `config`; `load_settings` → `parser.error` on `ConfigError`; print
  `start_line()` to stderr; `ensure_sqlite_version()` as today; `uvicorn.run(create_app(s.database_path,
  rule=checked_rule(s.rule.publication_delay_ms, s.rule.lag_threshold_ms), universe=s.instruments()),
  host=s.api.host, port=s.api.port, log_level="info")`. Remove `_port`, `_bounded_ms`, `DEFAULT_HOST`,
  `DEFAULT_PORT`. `api/app.py`: new signature, drop the `DEFAULT_RULE` import. `api/rule.py`: remove `DEFAULT_RULE`
  and the imports only it used. Update the module docstrings.

- [ ] **Step 4: Run** `make check` — Expected: PASS (`tests/test_api_process.py` included).

- [ ] **Step 5: Commit** — `git commit -m "feat: quasar-api serve reads config/quasar.toml; the app holds the universe (S20)"`

---

### Task 6: `GET /v1/instruments`

**Files:**
- Create: `src/quasar_market_data/api/instrument_reader.py`, `tests/api/test_instruments_endpoint.py`
- Modify: `src/quasar_market_data/api/database.py`, `src/quasar_market_data/api/app.py`

**Interfaces:**
- Consumes: `Instrument`; `_with_reader`, `require_no_query`, `verify_schema`
- Produces:
  - `instrument_reader.InstrumentNewest(instrument: Instrument, newest_open_time_ms: int | None)` (frozen)
  - `instrument_reader.read_instrument_newest(conn: sqlite3.Connection, universe: Sequence[Instrument]) -> tuple[InstrumentNewest, ...]`
    — `BEGIN`, `verify_schema`, one `SELECT MAX(open_time_ms) … WHERE venue = ? AND symbol = ? AND timeframe = ?` per
    instrument (primary-key seek), `ROLLBACK` in `finally` (same `_rollback` discipline as `health_reader`)
  - `database.read_instruments(db_path: Path, universe: Sequence[Instrument], *, open_connection: ConnectionFactory = open_reader) -> tuple[InstrumentNewest, ...]`

- [ ] **Step 1: Write the failing tests** (`client_for(path, universe)` builds the app with `SAMPLE_RULE`)

```python
def test_lists_configured_instruments_with_newest_or_null(tmp_path) -> None:
    # stored BTC slot(0..2); universe BTC, ETH → [{"venue":VENUE,"symbol":"BTC/USDT:USDT","timeframe":"5m","newest_open_time_ms":slot(2)},
    #                                            {… "ETH/USDT:USDT" …, "newest_open_time_ms": None}]
def test_a_stored_series_that_is_not_configured_is_absent(tmp_path) -> None: ...   # INV-067: stored LTC, universe BTC → only BTC
def test_order_is_by_triple_whatever_the_universe_order(tmp_path) -> None: ...      # universe passed reversed
def test_body_is_exactly_the_instruments_key(tmp_path) -> None: ...                 # set(body) == {"instruments"}
@pytest.mark.parametrize("reason, make", BROKEN)   # reuse the BROKEN table of test_health_endpoints
def test_503_when_the_database_is_not_ready(tmp_path, reason, make) -> None: ...
def test_a_query_parameter_is_400(tmp_path) -> None: ...                            # "?x=1" → 400 invalid_parameter
def test_no_store_utf8_json_and_405_for_post(tmp_path) -> None: ...
def test_reads_one_seek_per_configured_instrument(tmp_path) -> None: ...            # trace statements: exactly len(universe) MAX statements, no full scan (pattern of test_health_reader.test_work_depends_on_series_not_on_rows)
def test_the_answer_is_one_snapshot(tmp_path) -> None: ...                          # pattern of test_health_reader.test_snapshot_is_consistent_while_a_writer_commits
```

- [ ] **Step 2: Run** `uv run pytest tests/api/test_instruments_endpoint.py -q` — Expected: FAIL (404 on the path).

- [ ] **Step 3: Implement** the reader, `database.read_instruments`, and the route in `create_app`:
  `require_no_query` → `run_in_threadpool(read_instruments, db_path, universe, open_connection=…)` →
  `_json(200, {"instruments": [...]})`, entries in the key order of contract 6.4. Update the `app.py` docstring.

- [ ] **Step 4: Run** `make check` — Expected: PASS.

- [ ] **Step 5: Commit** — `git commit -m "feat: GET /v1/instruments lists the configured universe (S20)"`

---

### Task 7: `/health/ingest` universe block and `configured`; benchmark (HARD STOP 2)

**Files:**
- Modify: `src/quasar_market_data/api/app.py`, `tests/api/test_health_endpoints.py`, `tests/api/bench_health.py`
- Create: `tests/api/test_health_universe.py`, `tests/api/golden/health_ingest_s17.json`

**Interfaces:**
- Consumes: `StoredSeries`, `Instrument`, the app's sorted universe
- Produces: `HEALTH_MISSING_MAX: Final = 50` in `api/app.py`;
  `_ingest_body(stored: Sequence[StoredSeries], as_of_ms: int, rule: ExpectationRule, universe: Sequence[Instrument]) -> dict[str, object]`

- [ ] **Step 1: Capture the golden answer and the baseline benchmark before changing `_ingest_body`**

  In `test_health_universe.py` write `golden_database(path)`: 55 stale series, one due, one current (symbols
  `G000/USDT:USDT`…), plus one `1m` row of a configured symbol, at a fixed `AS_OF`. With the code as it is after Task 5,
  write the `/health/ingest` body for it to `tests/api/golden/health_ingest_s17.json` (a throwaway script in the
  scratchpad, not committed). Commit nothing yet, then freeze the baseline for Step 6:
  `git worktree add <scratchpad>/s20-baseline HEAD` (this commit already has the Task 5 `create_app` signature, so
  its `bench_health.py` runs unchanged with `universe=()`). Benchmarks are run in Step 6 only, alternating with the
  changed tree.

- [ ] **Step 2: Write the failing tests** in `tests/api/test_health_universe.py`

```python
S17_KEYS = ("as_of_ms", "rule", "scope", "status", "series_count", "current", "due", "stale", "behind", "truncated")

def s17_fields(body: dict[str, Any]) -> dict[str, Any]:
    out = {k: body[k] for k in S17_KEYS}
    out["behind"] = [{k: v for k, v in e.items() if k != "configured"} for e in body["behind"]]
    return out

@pytest.mark.parametrize("universe", [(), GOLDEN_SUBSET, GOLDEN_SUBSET + MISSING_60])
def test_s17_fields_equal_the_golden_answer_whatever_the_universe(tmp_path, universe) -> None:   # INV-066
    assert s17_fields(ingest(golden_database(tmp_path / "m.db"), AS_OF, universe)) == GOLDEN

def test_both_equalities_hold_on_every_fixture(tmp_path) -> None: ...   # every fixture of this file, via one helper assert_equalities(body)
def test_a_configured_instrument_with_no_row_is_missing_and_status_is_unchanged(tmp_path) -> None: ...
def test_a_configured_series_with_an_old_row_is_not_missing_and_stays_stale(tmp_path) -> None: ...
def test_a_retired_stale_series_stays_in_behind_marked_not_configured(tmp_path) -> None:   # INV-067
    # RETIRED stale + LIVE current, universe LIVE → status "behind", behind[0] == {…, "symbol": "RETIRED…", "configured": False},
    # universe.unconfigured_stored == 1, series_count == 2
def test_configured_flag_on_behind_entries(tmp_path) -> None: ...
@pytest.mark.parametrize("count, truncated", [(50, False), (51, True), (60, True)])
def test_missing_is_capped_with_a_complete_count(tmp_path, count, truncated) -> None: ...   # len(missing) == min(count, 50), missing_count == count
def test_all_configured_missing_is_no_series(tmp_path) -> None: ...   # migrated empty db: status no_series, series_count 0, missing_count == configured_count
def test_missing_is_ordered_by_triple(tmp_path) -> None: ...
def test_another_timeframe_of_a_configured_symbol_is_unconfigured(tmp_path) -> None: ...   # Review Focus 5: stored BTC 1m only, universe BTC 5m → missing BTC 5m, unconfigured_stored 1
def test_503_when_the_database_is_not_ready_with_a_universe(tmp_path) -> None: ...
class CommitDuringRead:
    """Connection proxy (pattern of ``test_health_reader.WriterAfterNthSeries``, plus ``close``): after the
    ``after``-th next-series statement, a separate writer connection commits while the read transaction is open."""

def test_a_commit_inside_the_open_read_transaction_changes_neither_s17_fields_nor_universe(tmp_path) -> None:
    # Fixture: configured A (stale), M (missing, sorts in the middle), Z (stale, sorts last); stored X (unconfigured).
    # reference = full body of one plain request (same as_of). Then a request through
    # create_app(open_connection=lambda p: CommitDuringRead(open_reader(p), p, after=2)); the commit adds
    #   a first row for M (would move it from missing to configured_stored),
    #   a current row for A (already read) and for Z (not yet read) (would change stale/current and status).
    # Assert: proxy committed exactly once while BEGIN was open; s17_fields(body) == s17_fields(reference);
    #         body["universe"] == reference["universe"]; assert_equalities(body).
    # Then a fresh request sees the commit: M not in missing, configured_stored + 1, A and Z current.

def test_a_commit_after_the_read_finished_is_not_in_the_answer(tmp_path, monkeypatch) -> None:
    # wrap health_reader.read_stored_series so that after it returns a writer commits a configured series' first row;
    # the body still lists it in missing and does not count it (the universe block is derived from the same tuple)
```

  In `tests/api/test_health_endpoints.py` change only the `ingest` helper: it returns `s17_fields(body)` (import from
  `test_health_universe`) so every existing S17 test body stays unchanged and keeps passing.

- [ ] **Step 3: Run** `uv run pytest tests/api/test_health_universe.py -q` — Expected: FAIL (no `universe` key).

- [ ] **Step 4: Implement** in `_ingest_body`: `configured = {(i.venue, i.symbol, i.timeframe) for i in universe}`;
  `configured` flag on each `behind` entry (last key); after `truncated` the `universe` block in the key order of
  spec 5.5, with `missing` from the sorted universe minus the stored triples, sliced to `HEALTH_MISSING_MAX`. No new
  SQL: the stored series tuple is the one snapshot. Update `bench_health.py` to pass the generated stored triples as
  the universe (and the S17 docstring of `health_reader` if it mentions the universe).

- [ ] **Step 5: Run** `make check` — Expected: PASS.

- [ ] **Step 6: Benchmark (HARD STOP 2 decision).** Same infrastructure as S17: the `target` profile of
  `tests/api/bench_health.py` (600 series x 100 000 rows, fresh temp database per run), unchanged except the
  `universe` argument of Step 4. Protocol:

  1. **Runs.** On one idle machine on mains power, alternate **B** (baseline worktree of Step 1) and **A** (this
     tree): `B A B A B A`, each `uv run python -m tests.api.bench_health --profile target > <scratchpad>/bench-{B|A}-{n}.json`
     run from its own tree. Record both commit SHAs, Python, SQLite, OS and machine.
  2. **Validity.** Every run must report `H1_steps_within_2x`, `H2_within_250ms` and
     `H3_writer_does_not_break_or_change_reads` as `true`. An `A` run that fails one where `B` passes is a regression:
     HARD STOP 2.
  3. **Figures.** Per run take the median of `handler_ms_quiet` (the endpoint this task changes) and of
     `snapshot_ms_quiet` (the read, expected unchanged since no SQL changes). For each figure, `B` = median of the B
     run medians, `A` = median of the A run medians, noise = (max − min) of the B run medians.
  4. **Decision per figure.**
     - `A <= 1.10 x B`: no regression.
     - `A > 1.10 x B` but `A <= B + noise` (the difference is inside the baseline's own spread): inconclusive; run two
       more pairs `B A B A` and recompute over all runs. Still inconclusive, or noise > 10 % of `B`: report it to the
       user as inconclusive (HARD STOP 2), never as a pass.
     - `A > 1.10 x B` and `A > B + noise`: regression, **STOP (HARD STOP 2)** and show the user every JSON file.
  5. Keep the JSON files and a table (per figure: B, A, noise, ratio, verdict) for the README in Task 8; remove the
     worktree (`git worktree remove <scratchpad>/s20-baseline`).

- [ ] **Step 7: Commit** — `git commit -m "feat: /health/ingest reports the configured universe apart from the S17 fields (S20)"`

---

### Task 8: Documentation, invariants, follow-up, roadmap

**Files:**
- Modify: `README.md`, `docs/invariants.md`, `docs/follow-ups.md`, `docs/superpowers/specs/2026-10-20-market-data-v1-design.md`

- [ ] **Step 1: README** — `quasar-collector run [CONFIG]` and `quasar-api serve [CONFIG]` sections describe the file
  (default `config/quasar.toml` relative to the working directory, relative `database.path` relative to the file,
  every key required, restart to apply, exit `2` on an invalid file, the start line with the SHA-256); remove every
  removed switch; `fetch-once` unchanged; add `/v1/instruments` and the `universe` block; the Task 7 Step 6 table
  (B, A, noise, ratio, verdict per figure; number of runs; SHAs, Python, SQLite, machine) next to the S17 numbers.

- [ ] **Step 2: Invariants** — add INV-063 to INV-067 (spec 8 wording), `ENFORCED`, with evidence:
  INV-063 `tests/config/test_parse.py::test_every_key_is_required`, `::test_an_unknown_key_is_an_error`,
  `tests/collector/test_cli_run.py::test_no_switch_is_accepted_any_more`,
  `tests/api/test_cli_serve.py::test_no_switch_is_accepted_any_more`; INV-064 the two `invalid_file` tests of
  Tasks 4–5; INV-065 the loader path tests and the two CLI working-directory tests; INV-066
  `tests/api/test_health_universe.py::test_s17_fields_equal_the_golden_answer_whatever_the_universe`,
  `::test_both_equalities_hold_on_every_fixture`,
  `::test_a_commit_inside_the_open_read_transaction_changes_neither_s17_fields_nor_universe`; INV-067 the retired-series, the `/v1/instruments` absence and the
  Collector removed-symbol tests. In INV-057 replace the "Not covered: an instrument whose ingestion never started is
  invisible (… S20/F-18)" clause with a pointer to the `universe` block. Also add a register line for the new
  `config` import contract next to the existing import-contract invariant, citing its negative cases. Note in the
  "Not covered" cells: different files for the two processes are only visible by the logged hash (spec 10).

- [ ] **Step 3: Follow-up and roadmap** — mark F-18 closed by S20 (date 2026-10-29, the `universe` block and
  `/v1/instruments`); append "— implemented (plan `2026-10-29-s20-universe-config.md`)" to the S20 roadmap bullet.

- [ ] **Step 4: Run** `make check` — Expected: PASS (the register test validates every evidence name).

- [ ] **Step 5: Commit** — `git commit -m "docs: S20 README, invariants INV-063 to INV-067, close F-18"`
