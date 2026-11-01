# Symbol overview page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local, read-only tool that serves one page showing, for every configured symbol, whether it is fine, where its history starts and how complete the last 30 days are, using only the existing API.

**Architecture:** A top-level package `tools/symbol_overview/` (stdlib only, not part of `quasar_market_data`, never imports it). A pure core (window, validation, judgement, strip) is separated from the I/O (HTTP fetch, single-flight refresh, HTTP server). The page only draws what `/overview.json` returns. The pure core is tested with hand-built API answers; one integration test runs the real API app on a seeded temporary database so the fakes cannot drift from the contract.

**Tech Stack:** Python 3.12 standard library (`http.server`, `http.client`/`urllib`, `concurrent.futures`, `threading`, `argparse`, `json`), plain HTML/JavaScript, pytest, ruff, mypy strict.

**Spec:** `docs/superpowers/specs/2026-11-01-symbol-overview-design.md` (APPROVED 2026-11-01). Section numbers below (5.3, V7 ...) point into it. Executors read both documents.

## Global Constraints

- The tool imports **nothing** from `quasar_market_data`, `ccxt`, `fastapi`, `httpx`, `requests`, `numpy`, `talib`, `sqlite3`, `uvicorn`; no new dependency; no framework or CDN in the page (spec 2, 5.1).
- No change to the Collector, the API, the database, or `docs/contracts/` (spec 2). Do not edit the contract.
- The tool calls only `GET /health/ingest`, `GET /v1/instruments`, `GET /v1/coverage`, and the coverage call carries exactly `venue, symbol, timeframe, start, end` (V11). It binds `127.0.0.1`, accepts only `GET`.
- `tf = 300 000` ms; `WINDOW_SLOTS = 8640`; `E = (T0 // tf + 1) × tf`; `S = E − 8640 × tf` (5.3). Valid timestamps `946684800000 ≤ t ≤ 4102444800000` (V10).
- Strip: 720 characters, bucket = 12 slots (5.7). Response body cap 2 MB (5.6).
- Concurrency 4; per-call timeout 10 s; refresh deadline 60 s; snapshot cache 15 s; page poll 60 s (5.10). Constants, overridable in tests.
- States and order: `no_data, unknown, stale, conflict, holes, behind, ok` (5.4). `unknown` and `no_data` are never `ok`. The tool compares no millisecond value against a time threshold (5.3).
- Page text is Czech; all text lives in one table in the page script; Python emits codes and numbers only (5.4, section 2).
- CLAUDE.md: tests mock the API (a local fake server), never production data or a live exchange; never claim a check passed unless it ran; use `make check` (lint, typecheck, imports, test).
- **Commit steps are performed only if the user has asked for commits** (the repository's local hook runs `make check` before any `git commit`). Otherwise stop after the verification step and report.

## Review Focus

Failure modes the spec implies that a user would hit; each has a test in the named task.

1. The newest candle is older than the window (a dead symbol) must read `stale`, not `ok` or `no_data` — Task 4 (`test_series_older_than_window_is_stale`), Task 6 (real API, symbol `ADA`).
2. A coverage answer whose counts and runs contradict each other must give `unknown`, never `ok` — Task 3 (mutation table).
3. A symbol that needs percent-encoding in the query (`&`, `+`, a space, non-ASCII; ADR-0001 P-8) must reach the API intact — Task 5 (`test_coverage_path_percent_encodes`).
4. The API failing for some calls mid-refresh (health fine, one coverage `503`) must mark only that symbol `unknown`, not the page — Task 5.
5. Two browser tabs, or the refresh button pressed repeatedly, must cost one refresh — Task 5 (`test_simultaneous_gets_share_one_refresh`, `test_get_within_min_interval_makes_no_api_call`).

---

## File structure

| File | Responsibility |
|---|---|
| `tools/symbol_overview/model.py` | constants and frozen dataclasses (no logic) |
| `tools/symbol_overview/window.py` | `window_for`, `build_strip` (pure) |
| `tools/symbol_overview/validate.py` | decoding and validation of the three API answers (pure) |
| `tools/symbol_overview/judge.py` | per-symbol judgement, summary, overview object (pure) |
| `tools/symbol_overview/fetch.py` | `http_fetch`, `TransportError`, `coverage_path` (I/O) |
| `tools/symbol_overview/refresh.py` | `Overviewer`: one refresh, single flight, cache, concurrency, deadline (I/O orchestration) |
| `tools/symbol_overview/server.py` | `make_server`: `/`, `/overview.json`, method and path refusal |
| `tools/symbol_overview/cli.py`, `__main__.py` | argument parsing, start |
| `tools/symbol_overview/page.html` | the page (rendering only) |
| `tests/tools/symbol_overview/` | tests, `helpers.py`, `fake_api.py` |

---

### Task 1: Package skeleton, tool coverage, boundary test

**Files:**
- Create: `tools/__init__.py`, `tools/symbol_overview/__init__.py`, `tests/tools/__init__.py`, `tests/tools/symbol_overview/__init__.py`, `tests/tools/symbol_overview/test_boundary.py`
- Modify: `Makefile` (the `lint` target), `pyproject.toml` (`[tool.mypy] files`, `[tool.ruff] src`), `README.md` ("Tool scope" table)

**Interfaces:**
- Produces: the importable package `tools.symbol_overview`; `make lint`/`typecheck` cover `tools/`; the boundary test.

- [ ] **Step 1: Write the failing test** in `test_boundary.py`. A function `forbidden_imports(source: str, allowed: frozenset[str]) -> set[str]` (AST: every `import` and `from ... import`, also inside functions; relative imports and `tools.symbol_overview*` are allowed; returns the top-level names not in `allowed`). `ALLOWED = frozenset({"argparse", "collections", "concurrent", "dataclasses", "enum", "http", "json", "pathlib", "sys", "threading", "time", "typing", "urllib", "__future__"})`. Tests:
  - `test_detects_forbidden_imports`: `forbidden_imports("import sqlite3\nfrom quasar_market_data.api import app\n", ALLOWED) == {"sqlite3", "quasar_market_data"}`; an import inside a function is found; `from . import model` and `import tools.symbol_overview.model` are allowed.
  - `test_tool_sources_import_only_allowed_modules`: for every `*.py` under `tools/symbol_overview/`, `forbidden_imports(...) == set()` (parametrize by file path; with no module yet use a module-level assertion that the directory exists).
  - `test_tool_sources_do_not_call_dynamic_imports`: no call to `__import__`, `eval`, `exec`, `compile` (same AST scan as `tests/test_domain_purity.py`).
- [ ] **Step 2: Run** `uv run pytest tests/tools -v`. Expected: FAIL (`tools` not importable / directory missing).
- [ ] **Step 3: Create the empty packages**, add `"tools"` to `files` in `[tool.mypy]` and to `src` in `[tool.ruff]`, change `Makefile` `lint` to `ruff check src tests spikes tools && ruff format --check src tests spikes tools`. Add two rows to the README "Tool scope" table: ruff and mypy cover `tools/`; `lint-imports` does not (the AST boundary test does).
- [ ] **Step 4: Run** `make check`. Expected: all four parts pass (`lint`, `typecheck`, `imports`, `test`).
- [ ] **Step 5: Commit** (only if asked): `git add tools tests/tools Makefile pyproject.toml README.md docs/superpowers && git commit -m "chore: symbol overview tool skeleton, tool coverage and import boundary test"`. This commit also carries the spec and this plan.

---

### Task 2: Model, window, strip (pure)

**Files:**
- Create: `tools/symbol_overview/model.py`, `tools/symbol_overview/window.py`, `tests/tools/symbol_overview/helpers.py`, `tests/tools/symbol_overview/test_window.py`

**Interfaces:**
- Produces in `model.py`: constants `TF_MS, WINDOW_SLOTS, BUCKET_SLOTS, BUCKETS, MIN_TS_MS, MAX_TS_MS, VENUE, TIMEFRAME, REFRESH_DEADLINE_MS, CALL_TIMEOUT_MS, MAX_BODY_BYTES` (values from Global Constraints; `BUCKETS = 720`, `REFRESH_DEADLINE_MS = 60_000`, `CALL_TIMEOUT_MS = 10_000`, `MAX_BODY_BYTES = 2_000_000`) and frozen dataclasses:
  - `Rule(publication_delay_ms: int, lag_threshold_ms: int)`, `Window(start: int, end: int)`, `Health(as_of_ms: int, rule: Rule)`, `InstrumentRow(venue: str, symbol: str, timeframe: str, newest_open_time_ms: int | None)`, `Run(state: str, start: int, end: int)`
  - `Coverage(as_of_ms: int, rule: Rule, oldest_open_time_ms: int, newest_open_time_ms: int, stored_ahead_of_as_of: bool, present_slots: int, conflicted_slots: int, unavailable_slots: int, missing_slots: int, not_yet_expected_slots: int, due_slots: int, stale_slots: int, runs: tuple[Run, ...])`
  - `Invalid(code: str, detail: str = "")`, `Reason(code: str, slots: int, runs: int, detail: str = "")`
  - `SymbolReport(venue: str, symbol: str, timeframe: str, state: str, reasons: tuple[Reason, ...], oldest_open_time_ms: int | None, newest_open_time_ms: int | None, as_of_ms: int | None, strip: str | None)`
- Produces in `window.py`: `window_for(as_of_ms: int) -> Window`; `build_strip(runs: Sequence[Run], oldest_open_time_ms: int, window: Window) -> str`.
- Produces in `helpers.py`: `T0 = 1_792_677_000_000` (slot-aligned), `RULE = Rule(15_000, 60_000)`, `instrument(symbol="BTC/USDT:USDT", newest=...) -> InstrumentRow`.

- [ ] **Step 1: Write the failing tests** in `test_window.py`:
  - `test_window_for_slot_boundary`: for `T0` → `Window(E − 8640*TF_MS, E)` with `E == T0 + TF_MS`; for `T0 − 1` → `E == T0`; for `T0 + 1` → `E == T0 + TF_MS`; always `end − start == 8640 * TF_MS`, both aligned.
  - `test_window_for_rejects_out_of_range_time`: `ValueError` below `MIN_TS_MS` and above `MAX_TS_MS`.
  - Strip, with `W = window_for(T0)` and `S = W.start`: `test_strip_is_720_chars`; `test_all_present_is_dots` (no runs, `oldest == S` → `"." * 720`); `test_one_slot_hole_is_visible` (a `missing` run of one slot at `S + 5*12*TF + 3*TF` → character `m` at index 5, rest `.`); `test_pre_history_buckets` (`oldest = S + 100*TF`, run `missing [S, oldest)` → indices 0–7 are `_`, index 8 is `.`); `test_conflicted_slot_before_oldest_shows_in_pre_history_zone` (`oldest = S + 100*TF`, runs `missing [S, S+10*TF)`, `conflicted [S+10*TF, S+11*TF)`, `missing [S+11*TF, oldest)` → index 0 is `c`, index 1–7 `_`); `test_worst_state_wins_within_a_bucket` (one bucket with `due` then `stale` runs → `s`; ranks `_ < . < n < d < u < m < c < s`, verify every adjacent pair with a parametrized test); `test_trailing_runs` (`not_yet_expected` for the last slot → last char `n`; a `stale` run covering the last 40 slots → last 4 chars `s`, and the 4th from the end `s`/`.` per bucket boundaries); `test_strip_ignores_nothing_outside_window` (a run with `start < S` or `end > E` raises `ValueError`).
- [ ] **Step 2: Run** `uv run pytest tests/tools/symbol_overview/test_window.py -v`. Expected: FAIL (modules missing).
- [ ] **Step 3: Implement `model.py`** (constants, dataclasses only) **and `window_for`** (integer arithmetic, `ValueError` outside the V10 range).
- [ ] **Step 4: Implement `build_strip`** by this algorithm: alphabet `.` present, `_` history not there yet, `n` not_yet_expected, `d` due, `u` unavailable, `m` missing, `c` conflicted, `s` stale (state → char map in `model.py`), rank order `_ < . < n < d < u < m < c < s`. Start each bucket `k` (slots `[S+12k·tf, S+12(k+1)·tf)`) as `_` if its end ≤ `oldest_open_time_ms`, else `.`. For each run (validate `S ≤ start < end ≤ E` and alignment, else `ValueError`), clip a `missing` run to `start ≥ oldest` (a `missing` part before `oldest` is pre-history and contributes nothing), then raise every bucket the clipped run overlaps to the run's char when it ranks higher. Cost is proportional to runs × buckets touched, never to slots.
- [ ] **Step 5: Run** the same command. Expected: PASS.
- [ ] **Step 6: Commit** (only if asked): `feat: symbol overview window and strip`.

---

### Task 3: Validation of the API answers (pure)

**Files:**
- Create: `tools/symbol_overview/validate.py`, `tests/tools/symbol_overview/test_validate.py`
- Modify: `tests/tools/symbol_overview/helpers.py`

**Interfaces:**
- Consumes: Task 2 (`model.py`, `window_for`).
- Produces in `validate.py`:
  - `health_result(status: int, content_type: str | None, body: bytes) -> Health | Invalid`
  - `instruments_result(status: int, content_type: str | None, body: bytes) -> tuple[InstrumentRow, ...] | Invalid`
  - `coverage_result(status: int, content_type: str | None, body: bytes, *, instrument: InstrumentRow, window: Window, health: Health) -> Coverage | Invalid`
  - Invalid codes (closed set, constants in `model.py`): `bad_status, api_error, bad_content_type, too_large, bad_json, schema, counts, runs, inconsistent_runs, echo, rule_mismatch, clock_inconsistent, regressed, inconsistent_with_instruments, empty_universe, transport, deadline`. For `api_error`, `detail` is the API's `error.code`.
- Produces in `helpers.py`: `health_doc(as_of_ms=T0, rule=RULE) -> dict[str, object]`; `coverage_doc(window, *, symbol, oldest, newest, slot_states: Mapping[int, str] = {}, as_of_ms=T0 + 1000, rule=RULE, stored_ahead=False) -> dict[str, object]` — builds a **self-consistent** `/v1/coverage` body: slots in `[oldest, newest]` not listed are `present`; slots before `oldest` are `missing`; slots after `newest` are `not_yet_expected`; listed slots take the given state; counts and maximal runs are derived from that.

- [ ] **Step 1: Write the failing tests** in `test_validate.py` (call the `*_result` functions with `json.dumps(doc).encode()`, status 200, content type `application/json; charset=utf-8`):
  - Transport level: `test_non_200_with_api_error_body` (`503`, body `{"error":{"code":"not_ready","message":"x","details":{"reason":"database_missing"}}}` → `Invalid("api_error", "not_ready")`); `test_non_200_without_error_body_is_bad_status`; `test_wrong_content_type`; `test_body_over_cap` (`MAX_BODY_BYTES + 1`); `test_not_utf8_json_and_not_an_object` (invalid bytes, `[]`, `"x"` → `bad_json`).
  - Health: valid → `Health(T0, RULE)`; `as_of_ms` missing, a string, a bool, below the V10 range; rule values negative or non-int → `Invalid("schema")`.
  - Instruments: valid list; `newest_open_time_ms` `null` accepted; a row with another venue or timeframe than `binance-usdm`/`5m` is **accepted** (it is judged per symbol in Task 5, spec 5.6); unaligned or out-of-range newest, duplicate triple, missing key, non-string field → `schema`; `[]` → `Invalid("empty_universe")`.
  - Coverage, a parametrized table `(mutation of a valid doc → expected code)`: wrong echo of `symbol`/`start`/`end`/`venue`/`timeframe` → `echo`; a count as a string or negative, `fully_present` not bool, missing `series` → `schema`; `slots != 8640`, seven counts not summing to `slots`, `fully_present` contradicting `present_slots` → `counts`; a run with state `present`, misaligned start, `start < window.start`, `end > window.end`, `start >= end`, `slots` not equal to length, overlapping or unordered runs, per-state run sum different from the state's count → `runs`; a `due`/`stale`/`not_yet_expected` run starting at or before `newest`, a `missing` run ending after `newest + TF_MS`, a `present` slot before `oldest` (runs not covering `[S, min(oldest, E))`) → `runs`; a `missing` run crossing `oldest` → `inconsistent_runs`; `rule` different from `health.rule` → `rule_mismatch`; `as_of_ms < health.as_of_ms` or `> health.as_of_ms + REFRESH_DEADLINE_MS + CALL_TIMEOUT_MS` → `clock_inconsistent`; `series.newest_open_time_ms <` the instrument's newest → `regressed`; `oldest > newest` → `schema`.
  - `test_valid_partial_history_is_accepted`, `test_valid_with_hole_conflict_and_stale_is_accepted` (returned `Coverage` fields equal the doc).
  - `test_404_unknown_series_for_a_stored_symbol` (`404` with the API error body for an instrument whose `newest_open_time_ms` is not null → `Invalid("inconsistent_with_instruments")`).
- [ ] **Step 2: Run** `uv run pytest tests/tools/symbol_overview/test_validate.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement `decode`** (private; order: size → status → content type → UTF-8 JSON object; a non-200 body of shape `{"error":{"code": str}}` gives `api_error`, any other non-200 `bad_status`) **and `health_result`, `instruments_result`.** Reject `bool` where `int` is required (`type(x) is int`).
- [ ] **Step 4: Implement `coverage_result`** following spec 5.6 exactly: echo, types, counts, runs (per-state run sums equal the counts), cross-checks with the series block (use `[S, min(O, E))` coverage by runs for "no `present` before `O`"), consistency with the other answers. The upper bound for `as_of_ms` is `health.as_of_ms + REFRESH_DEADLINE_MS + CALL_TIMEOUT_MS`: **this refines the spec's `T0 + refresh deadline`, because a call started just before the deadline is still handled by the API up to one call timeout later; mention it in the commit message.** `stored_ahead_of_as_of` is parsed, not judged here.
- [ ] **Step 5: Run** the same command. Expected: PASS.
- [ ] **Step 6: Commit** (only if asked): `feat: symbol overview answer validation`.

---

### Task 4: Judgement and the overview object (pure)

**Files:**
- Create: `tools/symbol_overview/judge.py`, `tests/tools/symbol_overview/test_judge.py`

**Interfaces:**
- Consumes: Tasks 2–3 (`Coverage`, `Invalid`, `InstrumentRow`, `Window`, `build_strip`; `coverage_doc` helper, parsed through `coverage_result` to get `Coverage` objects in tests).
- Produces in `judge.py`:
  - `judge_symbol(instrument: InstrumentRow, outcome: Coverage | Invalid | None, window: Window) -> SymbolReport` — `outcome is None` is allowed only when `instrument.newest_open_time_ms is None` (`no_data`); any other combination of `None` and a non-null newest raises `ValueError`.
  - `build_overview(health: Health, window: Window, reports: Sequence[SymbolReport], *, duration_ms: int, deadline_hit: bool) -> dict[str, object]` — shape of spec 5.8 (`generated_at_ms`, `rule`, `window`, `refresh`, `summary`, `symbols`), plus `detail` in each reason.
  - `page_error(invalid: Invalid) -> dict[str, object]` → `{"api_error": {"code": ..., "detail": ...}}`.
  - `STATE_ORDER = ("no_data", "unknown", "stale", "conflict", "holes", "behind", "ok")`.

- [ ] **Step 1: Write the failing tests** in `test_judge.py` (build coverage via `coverage_doc` → `coverage_result` → `judge_symbol`; `W = window_for(T0)`):
  - Reasons (spec 5.4 table): `test_no_data` (`outcome=None`, newest `None` → state `no_data`, reason `no_data`, `strip is None`); `test_invalid_outcome_is_unknown` (`Invalid("counts")` → `unknown`, reason `invalid`, `detail == "counts"`, never `ok`); `test_stored_ahead_is_unknown`; `test_stale_slots_make_stale`; `test_conflicted_slots_make_conflict`; `test_real_hole_makes_holes` (3 missing slots, one run → `Reason("missing", 3, 1)`); `test_unavailable_alone_is_holes`; `test_due_alone_is_behind`; `test_not_yet_expected_alone_is_ok`; `test_clean_is_ok`.
  - History: `test_leading_missing_run_is_not_a_hole` (`oldest = S + 100*TF`, nothing else → `ok`, no `missing` reason, `oldest_open_time_ms` reported); `test_hole_right_after_oldest_is_a_hole`; `test_pre_history_conflict_is_reported`; `test_oldest_equals_window_start`; `test_oldest_one_slot_after_start` (one pre-history slot, `ok`).
  - Clock: `test_series_older_than_window_is_stale` (`oldest == newest` before `S`, all window slots `stale` → `stale`, not `ok`); `test_tool_uses_counts_not_its_own_thresholds` (an answer with `due` slots whose `as_of_ms` and `rule` would classify them otherwise still gives `behind`).
  - Combination: `test_several_reasons_keep_all_and_headline_is_most_severe` (stale + conflict + missing → state `stale`, three reasons); `test_due_and_unavailable_headline_is_holes` (reasons `due` and `unavailable` both kept, state `holes`).
  - Overview: `test_summary_counts_each_symbol_once_by_headline`; `test_summary_has_every_state_key_with_zero`; `test_overview_carries_api_clock_and_rule`; `test_overview_symbols_keep_input_order`; `test_page_error_shape`; `test_overview_size_for_600_symbols` (600 reports with 720-character strips: `len(json.dumps(overview)) < 1_000_000`).
- [ ] **Step 2: Run** `uv run pytest tests/tools/symbol_overview/test_judge.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement `judge_symbol`** per spec 5.4: Step A with `real_missing = missing_slots − slots of missing runs with end ≤ oldest`, `hole_runs = missing runs with end > oldest`; reason codes `no_data, invalid, stored_ahead, stale, conflict, missing, unavailable, due`; the `detail` of an `invalid` reason is `invalid.code` when `invalid.detail` is empty, else `f"{invalid.code}:{invalid.detail}"` (so `"counts"`, `"api_error:not_ready"`, `"schema:unsupported_series"`); reason → state map (`invalid`, `stored_ahead` → `unknown`; `missing`, `unavailable` → `holes`; `due` → `behind`); headline = first of `STATE_ORDER` among the reasons' states, `ok` if none; `strip = build_strip(...)` for a `Coverage`, `None` otherwise; `as_of_ms` taken from the coverage.
- [ ] **Step 4: Implement `build_overview` and `page_error`.**
- [ ] **Step 5: Run** the same command. Expected: PASS.
- [ ] **Step 6: Commit** (only if asked): `feat: symbol overview judgement`.

---

### Task 5: Fetching and the refresh (I/O)

**Files:**
- Create: `tools/symbol_overview/fetch.py`, `tools/symbol_overview/refresh.py`, `tests/tools/symbol_overview/fake_api.py`, `tests/tools/symbol_overview/test_fetch.py`, `tests/tools/symbol_overview/test_refresh.py`

**Interfaces:**
- Consumes: Tasks 2–4.
- Produces in `fetch.py`: `RawResponse(status: int, content_type: str | None, body: bytes)` (frozen dataclass); `TransportError(Exception)` with attribute `code: str` (`"timeout"` or `"connection"`); `Fetch = Callable[[str], RawResponse]` (argument = path plus query); `http_fetch(base_url: str, timeout_s: float = 10.0) -> Fetch` (refuses any path other than `/health/ingest`, `/v1/instruments`, `/v1/coverage` with `ValueError`; reads at most `MAX_BODY_BYTES + 1` bytes; maps timeouts and connection errors to `TransportError`; never follows redirects); `coverage_path(instrument: InstrumentRow, window: Window) -> str` (query order `venue, symbol, timeframe, start, end`; the symbol is `urllib.parse.quote(symbol, safe="/:")`).
- Produces in `refresh.py`: `Snapshot(status: int, body: dict[str, object])`; `class Overviewer` with `__init__(self, fetch: Fetch, *, concurrency: int = 4, deadline_s: float = 60.0, min_interval_s: float = 15.0, monotonic: Callable[[], float] = time.monotonic)` and `get(self) -> Snapshot` (thread-safe).
- Produces in `fake_api.py`: `class FakeApi` — a context manager running a `ThreadingHTTPServer` on `127.0.0.1:0`; attributes `url`, `requests: list[str]` (path plus query, in arrival order), `max_in_flight: int`; constructed with `routes: Mapping[str, Callable[[dict[str, str]], FakeReply]]` (keyed by path; the callable gets the decoded query) and `delay_s: float = 0.0`; `FakeReply(status: int, body: bytes, content_type: str = "application/json")`.

- [ ] **Step 1: Write the failing tests.**
  - `test_fetch.py`: `test_coverage_path_percent_encodes` (symbols `A&B/USDT:USDT`, `1000+X/USDT:USDT`, `BTC USDT`, `币安/USDT:USDT` → the fake API decodes the query back to the original symbol; `/` and `:` stay unencoded); `test_http_fetch_refuses_other_paths` (`/v1/candles`, `/health`, `//x` → `ValueError`, no request reaches the fake); `test_http_fetch_returns_status_and_body_for_non_200`; `test_timeout_is_transport_error` (a fake delaying longer than `timeout_s` → `TransportError` with `code == "timeout"`); `test_connection_refused_is_transport_error` (`code == "connection"`); `test_body_is_capped` (a body over the cap is cut at `MAX_BODY_BYTES + 1` bytes); `test_redirect_is_not_followed` (a `302` is returned as a `RawResponse` with status 302).
  - `test_refresh.py` (fake API answering `health_doc`, an instruments list and `coverage_doc` per symbol): `test_full_refresh_judges_every_symbol` (four symbols: ok, hole, `null` newest → `no_data` with **no coverage request recorded for it**, stale); `test_page_error_when_health_fails` (`503` `not_ready` → `Snapshot(502, {"api_error": ...})`, no instruments or coverage request made); `test_page_error_when_instruments_fail` and `test_page_error_when_universe_is_empty`; `test_all_instruments_no_data` (every newest `null` → every symbol `no_data`, no coverage request, status 200, summary `ok == 0`); `test_unsupported_timeframe_symbol_is_unknown_without_request` (a stored row with timeframe `1h` or another venue → `unknown`, reason `invalid` with detail `schema:unsupported_series`, no coverage request for it, other symbols unaffected); `test_one_coverage_503_marks_only_that_symbol_unknown`; `test_connection_refused_is_page_error`; `test_concurrency_is_bounded` (20 symbols, `delay_s=0.05`, `max_in_flight <= 4`); `test_deadline_marks_remaining_symbols_unknown` (`concurrency=1`, `deadline_s=0.2`, 0.1 s per call, 10 symbols → later symbols reason `invalid`/`deadline`, `refresh.deadline_hit is True`, fewer than 10 coverage requests recorded, none after the deadline); `test_simultaneous_gets_share_one_refresh` (8 threads call `get()` at once; the fake saw exactly one `/health/ingest`; all 8 results are equal); `test_get_within_min_interval_makes_no_api_call` (injected `monotonic`; second `get()` at +14.9 s returns the same snapshot; at +15.1 s it refreshes); `test_errors_are_cached_for_min_interval` (a page error is also served from the cache); `test_a_bug_in_fetch_is_not_cached` (a `fetch` raising `RuntimeError` makes `get()` raise, the in-flight marker is cleared, the next `get()` tries again); `test_coverage_call_has_exactly_the_documented_parameters` (the recorded query keys are `venue, symbol, timeframe, start, end`).
- [ ] **Step 2: Run** `uv run pytest tests/tools/symbol_overview/test_fetch.py tests/tools/symbol_overview/test_refresh.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement `fake_api.py`** (stdlib `ThreadingHTTPServer`, a lock-protected in-flight counter updated around the sleep and the reply).
- [ ] **Step 4: Implement `fetch.py`** (`http.client` is enough; set the socket timeout; do not use `urllib.request` redirect handling).
- [ ] **Step 5: Implement `refresh.py`.** One refresh: `/health/ingest` → `health_result`; `/v1/instruments` → `instruments_result`; either `Invalid` → `Snapshot(502, page_error(...))`. Then `window_for(health.as_of_ms)`; an instrument with a non-null newest but a venue or timeframe other than `VENUE`/`TIMEFRAME` gets `Invalid("schema", "unsupported_series")` without any request; a `ThreadPoolExecutor(max_workers=concurrency)` created per refresh runs one task per other instrument with a non-null newest, results kept in input order; a task first checks `monotonic() > start + deadline_s` (then `Invalid("deadline")`, no request), else `fetch(coverage_path(...))` and `coverage_result(...)`; `TransportError` → `Invalid("transport", code)`; other exceptions propagate. Then `judge_symbol` for every instrument and `build_overview` → `Snapshot(200, ...)`. Single flight: under a lock, return the cached snapshot if younger than `min_interval_s`; else if a refresh is in flight, wait on its `concurrent.futures.Future`; else become the leader, run the refresh, store the snapshot (also a page error) with its `monotonic()` time, set the future, and always clear the in-flight marker in a `finally`; an exception from the leader is set on the future (followers re-raise it) and is not cached.
- [ ] **Step 6: Run** the same command. Expected: PASS. Then `make typecheck`. Expected: no errors.
- [ ] **Step 7: Commit** (only if asked): `feat: symbol overview fetch and single-flight refresh`.

---

### Task 6: Server, CLI, page, README, real-API test

**Files:**
- Create: `tools/symbol_overview/server.py`, `tools/symbol_overview/cli.py`, `tools/symbol_overview/__main__.py`, `tools/symbol_overview/page.html`, `tests/tools/symbol_overview/test_server.py`, `tests/tools/symbol_overview/test_cli.py`, `tests/tools/symbol_overview/test_against_real_api.py`
- Modify: `README.md` (a "Symbol overview (tool)" section)

**Interfaces:**
- Consumes: `Overviewer`, `Snapshot` (Task 5), `http_fetch`.
- Produces: `make_server(source: SnapshotSource, host: str, port: int) -> ThreadingHTTPServer` where `SnapshotSource` is a `Protocol` with `get() -> Snapshot`; `main(argv: Sequence[str] | None = None) -> int` with `--api-url` (default `http://localhost:8000`, scheme must be `http` or `https`) and `--port` (default `8765`), host fixed to `127.0.0.1`.

- [ ] **Step 1: Write the failing tests.**
  - `test_server.py` (a stub `SnapshotSource`): `GET /` → 200 `text/html` containing the element ids `summary`, `problems`, `ok-group`, `heatmap`, `refresh`, `api-error`; `GET /overview.json` → the snapshot's status and JSON body, `Cache-Control: no-store`; `GET /overview.json?x=1` → 400; `GET /other` → 404; `POST`, `PUT`, `DELETE`, `PATCH`, `HEAD`, `OPTIONS` → 405 with `Allow: GET`; a source that raises → 500 with a generic JSON body and no stack trace; the server's bound address is `127.0.0.1`.
  - `test_cli.py`: `main(["--api-url", "ftp://x"])` → usage error (exit code 2, nothing started); defaults parse to `http://localhost:8000` and `8765` (test the argument parser function, do not start a server).
  - `test_against_real_api.py` (this directory is outside `tests/api/`, so the socket guard does not apply): seed a temporary database with `tests.api.seed` (`make_database` for the first symbol, `open_writer` plus `insert_candle` for the rest), `AT = tests.api.seed.AT`, rule `SAMPLE_RULE`; serve `quasar_market_data.api.app.create_app(db, rule=SAMPLE_RULE, universe=[Instrument(...) ...], now_ms=lambda: AT)` with `uvicorn.Server` in a thread on port 0 (read the port from `server.servers[0].sockets[0].getsockname()[1]` after `server.started`; shut down with `should_exit = True`). With `W = window_for(AT)`: `BTC` candles `W.start … AT − 2*TF_MS` → `ok`; `ETH` the same minus 3 consecutive slots → `holes`, `Reason("missing", 3, 1)`; `SOL` from `W.start + 100*TF_MS` → `ok`, strip starts with 8 × `_` then `.`, `oldest_open_time_ms == W.start + 100*TF_MS`; `ADA` newest `AT − 20*TF_MS` → `stale`; `XRP` configured, nothing stored → `no_data`. Assert through `Overviewer(http_fetch(url)).get()`: status 200, **no symbol is `unknown`** (the real answers pass validation), `summary == {total: 5, ok: 2, holes: 1, stale: 1, no_data: 1, unknown: 0, conflict: 0, behind: 0}`. A second test: `create_app` on a path of a database that does not exist → `Snapshot(502, {"api_error": {"code": "api_error", "detail": "not_ready"}})`.
- [ ] **Step 2: Run** `uv run pytest tests/tools/symbol_overview/test_server.py tests/tools/symbol_overview/test_cli.py tests/tools/symbol_overview/test_against_real_api.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement `server.py`, `cli.py`, `__main__.py`** (`raise SystemExit(main())`; the CLI prints one line with the URL of the page; Ctrl-C exits 0).
- [ ] **Step 4: Write `page.html`** (plain HTML, CSS, JavaScript; no external resource). Behavior (spec 5.8, 5.11):
  - One `TEXT` table in the script holds every Czech string and the reason templates (`no_data`, `invalid`, `stored_ahead`, `stale`, `conflict`, `missing`, `unavailable`, `due`), keyed by the reason code.
  - Fetch `/overview.json`; schedule the next poll 60 s after the previous request **finished or failed**; the `#refresh` button triggers a fetch only when none is in flight.
  - `#api-error`: shown for any response with `api_error` (and for a network failure to the tool itself); when shown, no states are drawn.
  - `#summary`: a tile per state with its count; `#problems`: every symbol whose state is not `ok`, ordered by `STATE_ORDER` then symbol, each with all reasons in words; `#ok-group`: a `<details>` "N symbolů v pořádku" listing the `ok` symbols.
  - `#heatmap`: one row per symbol with "Historie od" (UTC date of `oldest_open_time_ms`) and one `<canvas width=720 height=14>` drawn from the strip string (one fill per run of equal characters; a hatched fill for `strip === null`); a tooltip from `mousemove` giving the bucket's UTC range and state in words. Colors must also differ in lightness, and the legend names every state.
  - Header line: `generated_at_ms` (API clock) and the snapshot's age from the **browser** clock; older than 3 × 60 s is shown in the warning color. This age is display only.
  - The page contains no rule that decides a state.
- [ ] **Step 5: Run** the Step 2 command. Expected: PASS.
- [ ] **Step 6: Write the README section** "Symbol overview (tool)": what it shows; the command `uv run python -m tools.symbol_overview --api-url http://localhost:8000 --port 8765`; how to reach the VPS API (the SSH tunnel from the `compose.yaml` header, then the same command); what each state means; the limits L1–L3 of the spec in three lines; that it only reads, binds `127.0.0.1`, and refreshes only while the page is open. Do not claim any measurement of coverage load (spec Q3).
- [ ] **Step 7: Run** `make check`. Expected: all four parts pass.
- [ ] **Step 8: Manual check** (not a unit test; the page cannot be unit-tested). In a scratch directory outside the repo, build a database like the integration test (one ok symbol, one with a hole, one with a partial history, one never stored), start the real API on it (`quasar-api serve` with a temporary configuration; command and file shape are in README "Configuration file (S20)"), start the tool against it, open `http://127.0.0.1:8765/` and check by eye: summary tiles, the problem list with all reasons, the collapsed `ok` group, the strips (grey start for partial history, a visible one-slot hole), the tooltip, the refresh button, and the page error after stopping the API. Report what was seen and what was not checked; do not claim the page works if the browser step was not done.
- [ ] **Step 9: Commit** (only if asked): `feat: symbol overview server, page and README`.

---

## Notes for the executor

- **Decisions this plan adds to the spec** (flag them when reporting): the coverage `as_of_ms` upper bound is `T0 + 60 s + 10 s` (Task 3); each reason carries a `detail` string (Task 4); the Czech text lives only in the page script, Python emits codes and numbers (spec section 2 asked for one place); the page-level error is HTTP `502` (spec left the status to the builder); a page error is cached for the same 15 s as a snapshot (Task 5).
- **Not measured** (spec Q3): the load of `/v1/coverage` for hundreds of symbols. The constants are placeholders until someone measures them; say so in the final report.
- Do not edit `docs/contracts/api-v1.md` or record the approval of its section 6.4 (spec section 9).
