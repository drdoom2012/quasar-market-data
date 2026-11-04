"""Benchmark of one refresh of the symbol overview (OI-6a plan, task 1; spec 7.2 and 7.3). Not part of ``make check``.

It reproduces the call order of ``tools/symbol_overview/refresh.py`` against a real ``quasar-api`` process on a
generated database (never production data): ``/health/ingest``, ``/v1/instruments``, then a **candle phase** (one
``/v1/coverage`` call per stored symbol) and an **open interest phase** (one call per symbol), 4 workers, a new
connection per call, the refresh deadline checked before every call. ``--oi-mode proxy`` asks ``/v1/coverage`` for the
open interest slot (the G0 stand-in, before the endpoint exists); ``real`` asks ``/v1/open-interest/coverage``.
Run by hand: ``make bench-overview``.

FR-6: ``--funding-mode real`` adds a third phase (one ``/v1/funding-rates`` call per symbol, the tool's own window), a
generated funding history, and reports the total time of a refresh **and what each phase of it took**, with and
without the funding phase from the same run (refreshes alternate, so drift hits both), plus the difference.
"""

import argparse
import http.client
import json
import math
import platform
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal
from urllib.parse import quote, urlsplit

from quasar_market_data.domain.open_interest_cadence import open_interest_cadence
from quasar_market_data.storage.connection import open_writer
from quasar_market_data.storage.migrate import migrate
from tests.api.bench_server import BenchError, peak_rss_bytes, reap, start_api
from tools.bench_open_interest_write import INSERT_OPEN_INTEREST, nearest_rank
from tools.symbol_overview.fetch import Fetch, RawResponse, TransportError, coverage_path, funding_path, http_fetch
from tools.symbol_overview.model import MAX_BODY_BYTES, Health, InstrumentRow, Invalid, Window
from tools.symbol_overview.validate import funding_health, health_result, instruments_result
from tools.symbol_overview.window import funding_window, window_for

TF_MS: Final = 300_000
VENUE: Final = "binance-usdm"
OI_PATH: Final = "/v1/open-interest/coverage"
AC1_LIMIT_S: Final = 45.0
AC4_P99_LIMIT_MS: Final = 1000.0
AC5_LIMIT_BYTES: Final = 432 * 1024 * 1024
AC6_LIMIT_BYTES: Final = 1_000_000
WRITER_MAX_ROWS: Final = 100
COVERAGE_KINDS: Final = ("candle", "oi")

type OiMode = Literal["real", "proxy"]
type FundingMode = Literal["off", "real"]
PHASES: Final = ("head", "candle", "oi", "funding")

_INSERT_CANDLE: Final = (
    "INSERT OR IGNORE INTO candles (venue, symbol, timeframe, open_time_ms, open, high, low, close, volume,"
    " turnover, ingested_at_ms) VALUES (?, ?, '5m', ?, '100', '101', '99', '100', '1', NULL, ?)"
)


@dataclass(frozen=True)
class CallRecord:
    kind: str  # health | instruments | candle | oi | funding
    path: str
    started_s: float
    duration_s: float
    status: int  # 0 for a transport error or a 200 answer that is not JSON
    body_bytes: int
    decode_s: float


@dataclass(frozen=True)
class RefreshResult:
    duration_s: float
    calls: tuple[CallRecord, ...]
    candle_not_asked: int
    oi_not_asked: int
    failed: int  # calls whose status is not 200
    funding_not_asked: int = 0
    phases: Mapping[str, float] = field(
        default_factory=dict
    )  # wall time of each phase in seconds (head, candle, oi, funding)


def _call(fetch: Fetch, kind: str, path: str, monotonic: Callable[[], float]) -> tuple[CallRecord, RawResponse | None]:
    started = monotonic()
    try:
        raw = fetch(path)
    except TransportError:
        return CallRecord(kind, path, started, monotonic() - started, 0, 0, 0.0), None
    duration = monotonic() - started
    status = raw.status
    decode = 0.0
    if status == 200:
        t0 = time.perf_counter()
        try:
            json.loads(raw.body)
        except ValueError:
            status = 0  # an answer that is not JSON is a failed call
        decode = time.perf_counter() - t0
    return CallRecord(kind, path, started, duration, status, len(raw.body), decode), raw


def _oi_path(instrument: InstrumentRow, window: Window) -> str:
    """The open interest slots of the periods the candle window covers: slot ``T`` is a period end, so shift by ``tf``."""
    return (
        f"{OI_PATH}?venue={quote(instrument.venue, safe='')}&symbol={quote(instrument.symbol, safe='/:')}"
        f"&period={quote(instrument.timeframe, safe='')}&start={window.start + TF_MS}&end={window.end + TF_MS}"
    )


def _head(name: str, raw: RawResponse | None, parse: Callable[[int, str | None, bytes], Any]) -> Any:
    if raw is None:
        raise BenchError(f"{name} could not be reached")
    parsed = parse(raw.status, raw.content_type, raw.body)
    if isinstance(parsed, Invalid):
        raise BenchError(f"{name}: {parsed.code} {parsed.detail}".strip())
    return parsed


def run_refresh(
    fetch: Fetch,
    *,
    oi_mode: OiMode,
    funding_mode: FundingMode = "off",
    workers: int = 4,
    deadline_s: float = 60.0,
    monotonic: Callable[[], float] = time.monotonic,
) -> RefreshResult:
    """One refresh in the order of the overview tool (spec 8.1): head calls, candle phase, open interest phase and,
    with ``funding_mode="real"``, the funding phase (FR-6 design 5). The wall time of each phase is recorded."""
    started = monotonic()
    health_record, raw_health = _call(fetch, "health", "/health/ingest", monotonic)
    health: Health = _head("/health/ingest", raw_health, health_result)
    instruments_record, raw_instruments = _call(fetch, "instruments", "/v1/instruments", monotonic)
    instruments: tuple[InstrumentRow, ...] = _head("/v1/instruments", raw_instruments, instruments_result)
    window = window_for(health.as_of_ms)
    funding_window_ = None
    if funding_mode == "real":
        assert raw_health is not None
        funding_state = funding_health(raw_health.status, raw_health.content_type, raw_health.body, health=health)
        if funding_state is None or isinstance(funding_state, Invalid):
            raise BenchError("funding mode needs an API with an enabled [funding_rate] table")
        funding_window_ = funding_window(window, funding_state.rule, health.as_of_ms)
    phases: dict[str, float] = {"head": monotonic() - started}

    def candle_path(instrument: InstrumentRow) -> str:
        return coverage_path(instrument, window)

    def oi_path(instrument: InstrumentRow) -> str:
        return coverage_path(instrument, window) if oi_mode == "proxy" else _oi_path(instrument, window)

    with ThreadPoolExecutor(max_workers=workers) as pool:

        def phase(
            items: Sequence[InstrumentRow], kind: str, path_for: Callable[[InstrumentRow], str]
        ) -> tuple[list[CallRecord], int]:
            def one(instrument: InstrumentRow) -> CallRecord | None:
                if monotonic() - started > deadline_s:
                    return None
                return _call(fetch, kind, path_for(instrument), monotonic)[0]

            outcomes = list(pool.map(one, items))
            return [o for o in outcomes if o is not None], sum(1 for o in outcomes if o is None)

        stored = [i for i in instruments if i.newest_open_time_ms is not None]
        mark = monotonic()
        candle_calls, candle_not_asked = phase(stored, "candle", candle_path)
        phases["candle"], mark = monotonic() - mark, monotonic()
        oi_calls, oi_not_asked = phase(instruments, "oi", oi_path)
        phases["oi"], mark = monotonic() - mark, monotonic()
        funding_calls: list[CallRecord] = []
        funding_not_asked = 0
        if funding_window_ is not None:
            fwindow = funding_window_
            funding_calls, funding_not_asked = phase(instruments, "funding", lambda i: funding_path(i, fwindow))
        phases["funding"] = monotonic() - mark if funding_window_ is not None else 0.0

    calls = (health_record, instruments_record, *candle_calls, *oi_calls, *funding_calls)
    return RefreshResult(
        duration_s=monotonic() - started,
        calls=calls,
        candle_not_asked=candle_not_asked,
        oi_not_asked=oi_not_asked,
        failed=sum(1 for call in calls if call.status != 200),
        funding_not_asked=funding_not_asked,
        phases=phases,
    )


def _microseconds(seconds: float) -> int:
    return round(seconds * 1_000_000)


def summarize(calls: Sequence[CallRecord]) -> dict[str, dict[str, float]]:
    """Per kind, over the **successful** calls only: ``n``, ``p50``, ``p95``, ``p99``, ``max``, ``mean`` in ms."""
    by_kind: dict[str, list[int]] = {}
    for call in calls:
        if call.status == 200:
            by_kind.setdefault(call.kind, []).append(_microseconds(call.duration_s))
    summary: dict[str, dict[str, float]] = {}
    for kind, values in by_kind.items():
        values.sort()
        summary[kind] = {
            "n": len(values),
            "p50": nearest_rank(values, 50) / 1000,
            "p95": nearest_rank(values, 95) / 1000,
            "p99": nearest_rank(values, 99) / 1000,
            "max": values[-1] / 1000,
            "mean": sum(values) / len(values) / 1000,
        }
    return summary


def evaluate(
    results: Sequence[RefreshResult], *, series: int, l_tunnel_ms: float | None, funding: bool = False
) -> dict[str, Any]:
    """AC1, AC2, AC3, AC6 and AC7 of spec 7.2 over a list of fresh refreshes.

    With ``funding`` the refreshes have a third phase: AC7 uses the factor 3, the mean latency includes the funding
    calls and AC2 also counts the funding symbols not asked."""
    if not results:
        raise ValueError("no refresh results")
    calls = [call for result in results for call in result.calls]
    longest = max(result.duration_s for result in results)
    kinds = (*COVERAGE_KINDS, "funding") if funding else COVERAGE_KINDS
    coverage = [_microseconds(c.duration_s) for c in calls if c.kind in kinds and c.status == 200]
    l_mean_ms = sum(coverage) / len(coverage) / 1000 if coverage else None
    d_pred: float | None = None
    if l_tunnel_ms is not None and l_mean_ms is not None:
        d_pred = (2 + math.ceil(series / 4) * (3 if funding else 2)) * (l_mean_ms + l_tunnel_ms) / 1000
    counts = {
        "failed": sum(result.failed for result in results),
        "candle_not_asked": sum(result.candle_not_asked for result in results),
        "oi_not_asked": sum(result.oi_not_asked for result in results),
    }
    if funding:
        counts["funding_not_asked"] = sum(result.funding_not_asked for result in results)
    largest = max((call.body_bytes for call in calls), default=0)
    return {
        "AC1": {"value": longest, "pass": longest <= AC1_LIMIT_S},
        "AC2": {"value": counts, "pass": not any(counts.values())},
        "AC3": {"value": summarize(calls), "pass": None},
        "AC6": {
            "value": {"max_body_bytes": largest, "max_decode_ms": max((c.decode_s for c in calls), default=0.0) * 1000},
            "pass": largest <= AC6_LIMIT_BYTES,
        },
        "AC7": {"value": d_pred, "pass": None if d_pred is None else d_pred <= AC1_LIMIT_S},
    }


def _stats(values: Sequence[float]) -> dict[str, float]:
    """``min``, ``p50``, ``p95``, ``max`` and ``mean`` of ``values`` (seconds)."""
    ordered = sorted(_microseconds(v) for v in values)
    return {
        "min": ordered[0] / 1_000_000,
        "p50": nearest_rank(ordered, 50) / 1_000_000,
        "p95": nearest_rank(ordered, 95) / 1_000_000,
        "max": ordered[-1] / 1_000_000,
        "mean": sum(ordered) / len(ordered) / 1_000_000,
    }


def breakdown(results: Sequence[RefreshResult]) -> dict[str, Any]:
    """The total time of a refresh and what each phase of it took (the user's request of 2026-11-04).

    ``total`` and every phase carry ``min``, ``p50``, ``p95``, ``max``, ``mean`` in seconds; ``share`` is the mean
    time of the phase over the mean total; ``calls`` is the mean number of calls the phase made.
    """
    if not results:
        raise ValueError("no refresh results")
    total = _stats([r.duration_s for r in results])
    phases: dict[str, Any] = {}
    for name in PHASES:
        times = [r.phases.get(name, 0.0) for r in results]
        kind = {"head": None, "candle": "candle", "oi": "oi", "funding": "funding"}[name]
        made = [
            sum(1 for c in r.calls if (c.kind in ("health", "instruments") if kind is None else c.kind == kind))
            for r in results
        ]
        stats = _stats(times)
        phases[name] = {
            **stats,
            "share": stats["mean"] / total["mean"] if total["mean"] else 0.0,
            "calls": sum(made) / len(made),
        }
    return {"refreshes": len(results), "total": total, "phases": phases}


def funding_cost(without: Sequence[RefreshResult], with_funding: Sequence[RefreshResult]) -> dict[str, float]:
    """What the funding phase added, measured in one run: the difference of the mean totals (seconds) and the mean
    funding call (milliseconds)."""
    off = sum(r.duration_s for r in without) / len(without)
    on = sum(r.duration_s for r in with_funding) / len(with_funding)
    calls = [c.duration_s for r in with_funding for c in r.calls if c.kind == "funding" and c.status == 200]
    return {
        "mean_total_without_s": off,
        "mean_total_with_s": on,
        "added_s": on - off,
        "added_share_of_with": (on - off) / on if on else 0.0,
        "mean_funding_call_ms": sum(calls) / len(calls) * 1000 if calls else 0.0,
    }


def format_breakdown(title: str, data: Mapping[str, Any]) -> str:
    """A table of ``breakdown`` for a human (printed to stderr; the JSON line on stdout has the same numbers)."""
    lines = [
        f"{title} ({data['refreshes']} refreshes)",
        f"{'':8}{'min':>9}{'p50':>9}{'p95':>9}{'max':>9}{'mean':>9}{'share':>8}{'calls':>8}",
    ]
    total = data["total"]
    lines.append(
        f"{'TOTAL':8}{total['min']:9.2f}{total['p50']:9.2f}{total['p95']:9.2f}{total['max']:9.2f}{total['mean']:9.2f}{'100%':>8}{'':>8}"
    )
    for name, p in data["phases"].items():
        lines.append(
            f"{name:8}{p['min']:9.2f}{p['p50']:9.2f}{p['p95']:9.2f}{p['max']:9.2f}{p['mean']:9.2f}{p['share'] * 100:7.1f}%{p['calls']:8.0f}"
        )
    return "\n".join(lines) + "\n(seconds; share = mean phase time over mean total)"


def evaluate_sustained(
    probe_ms: Sequence[float], http_503: int, server_peak_rss_bytes: int, problems: int = 0
) -> dict[str, Any]:
    """AC4 (the ``/health/ready`` probe during the sustained run), AC5 (the server's peak resident set) and AC2 over
    the sustained refreshes (``problems``: failed calls plus calls not asked)."""
    p99_ms: float | None = None
    passed: bool | None = False if http_503 else None
    if probe_ms:
        p99_ms = nearest_rank(sorted(round(ms * 1000) for ms in probe_ms), 99) / 1000
        passed = p99_ms <= AC4_P99_LIMIT_MS and http_503 == 0
    return {
        "AC4": {"value": {"p99_ms": p99_ms, "http_503": http_503}, "pass": passed},
        "AC5": {"value": server_peak_rss_bytes, "pass": server_peak_rss_bytes <= AC5_LIMIT_BYTES},
        "AC2_sustained": {"value": problems, "pass": problems == 0},
    }


def writer_tick(conn: sqlite3.Connection, symbols: Sequence[str], candle_open_ms: int, oi_time_ms: int) -> int:
    """One candle and one open interest row per symbol, in transactions of at most 100 rows; returns the row count."""
    ingested = time.time_ns() // 1_000_000
    rows = 0
    in_transaction = 0
    for symbol in symbols:
        if in_transaction == 0:
            conn.execute("BEGIN")
        conn.execute(_INSERT_CANDLE, (VENUE, symbol, candle_open_ms, ingested))
        conn.execute(INSERT_OPEN_INTEREST, (VENUE, symbol, "5m", oi_time_ms, "95000.5", "7000000000.25", ingested))
        rows += 2
        in_transaction += 2
        if in_transaction >= WRITER_MAX_ROWS:
            conn.execute("COMMIT")
            in_transaction = 0
    if in_transaction:
        conn.execute("COMMIT")
    return rows


# --- the generated database ------------------------------------------------------------------------------------------

PROFILES: Final = {"smoke": 9_000, "target": 100_000}  # candle rows per series; ``target`` is the bench-health depth
HOLE_OFFSETS: Final = (100, 101, 5_000)  # slots back from the newest, on every 7th series
CONFLICT_OFFSET: Final = 200
SHORT_HISTORY_SLOTS: Final = 3 * 288  # one series whose open interest starts three days ago
SUSTAINED_PAUSE_S: Final = 15.0  # the overview tool's snapshot cache interval: the sustained case is D out of D + 15 s
PROBE_INTERVAL_S: Final = 0.5
PROBE_TIMEOUT_S: Final = 10.0
_INSERT_CONFLICT: Final = (
    "INSERT INTO open_interest_conflicts (venue, symbol, period, time_ms, open_interest, open_interest_value,"
    " first_seen_at_ms) VALUES (?, ?, '5m', ?, ?, ?, ?)"
)


def symbol_name(index: int) -> str:
    return f"SYM{index:04d}/USDT:USDT"


def _open_interest_rows(
    symbol: str, newest: int, count: int, skip: set[int], *, stress: bool, ingested: int
) -> Iterator[tuple[object, ...]]:
    for k in range(count):
        if k in skip or (stress and k % 2 == 1):
            continue
        yield (VENUE, symbol, "5m", newest - k * TF_MS, f"{95_000 + k % 997}.5", f"{7_000_000_000 + k}.25", ingested)


_INSERT_FUNDING: Final = (
    "INSERT INTO funding_rates (venue, symbol, funding_time_ms, rate_type, funding_rate, mark_price, ingested_at_ms)"
    " VALUES (?, ?, ?, 'Regular', ?, ?, ?)"
)
_INSERT_FUNDING_SPAN: Final = (
    "INSERT INTO funding_rate_verified (venue, symbol, from_ms, through_ms, verified_at_ms) VALUES (?, ?, ?, ?, ?)"
)
_INSERT_FUNDING_CONFLICT: Final = (
    "INSERT INTO funding_rate_conflicts (venue, symbol, funding_time_ms, rate_type, funding_rate, mark_price,"
    " first_seen_at_ms) VALUES (?, ?, ?, 'Regular', ?, ?, ?)"
)
HOUR_MS: Final = 3_600_000


type FundingRows = tuple[
    list[tuple[str, str, int, str, str, int]], tuple[str, str, int, int, int], list[tuple[str, str, int, str, str, int]]
]


def funding_rows(index: int, symbol: str, now_ms: int) -> FundingRows:
    """The funding history of one generated series: ``(rows, span, conflicts)``.

    A healthy series has a span from 31 days back to an hour ago and 30 days of settlements (every 8 h, every 5th
    series every hour). Series 1 is shallow (its span starts 3 days back), series 2 is behind (its span ended 5 h
    ago), series 3 is quiet (a span, no row), every 7th series has a conflict record on its newest settlement.
    """
    through = now_ms - (5 * HOUR_MS if index == 2 else HOUR_MS)
    start = through - (3 * 86_400_000 if index == 1 else 31 * 86_400_000)
    step = HOUR_MS if index % 5 == 0 else 8 * HOUR_MS
    newest = through - 5
    count = 0 if index == 3 else 30 * 24 * HOUR_MS // step
    rows = [
        (VENUE, symbol, newest - k * step, f"0.0000{k % 9 + 1}", f"{95_000 + k % 997}.5", now_ms) for k in range(count)
    ]
    span = (VENUE, symbol, start, through, now_ms)
    conflicts = [(VENUE, symbol, newest, "0.0009", "95001.5", now_ms)] if index % 7 == 0 and count else []
    return rows, span, conflicts


def build_database(
    path: Path,
    *,
    series: int,
    candle_rows: int,
    oi_rows: int = 8_640,
    stress: bool = False,
    now_ms: int,
    funding: bool = False,
) -> list[str]:
    """Candles and open interest for ``series`` symbols ending at the last closed period before ``now_ms``.

    Every 7th series has a few holes in both and one open interest conflict record; series 1 has only three days of
    open interest. With ``stress`` every even open interest slot has a row **and** a conflict record and every odd
    slot has no row, so an answer alternates ``conflicted`` and ``missing`` (the largest number of runs). Returns the
    symbols. With ``funding`` every series also gets the history of ``funding_rows``. Plain SQL, canonical decimal text,
    one transaction per series.
    """
    newest_oi = now_ms // TF_MS * TF_MS  # the end of the last closed period
    newest_candle = newest_oi - TF_MS  # the open of the candle it belongs to
    symbols = [symbol_name(i) for i in range(series)]
    conn = open_writer(path)
    try:
        migrate(conn, applied_at_ms=now_ms)
        for index, symbol in enumerate(symbols):
            holes = set(HOLE_OFFSETS) if index % 7 == 0 else set()
            count = SHORT_HISTORY_SLOTS if index == 1 else oi_rows
            conn.execute("BEGIN")
            conn.executemany(
                _INSERT_CANDLE,
                ((VENUE, symbol, newest_candle - k * TF_MS, now_ms) for k in range(candle_rows) if k not in holes),
            )
            conn.executemany(
                INSERT_OPEN_INTEREST,
                _open_interest_rows(symbol, newest_oi, count, holes, stress=stress, ingested=now_ms),
            )
            if stress:
                conflicts = [(VENUE, symbol, newest_oi - k * TF_MS, "1.5", "2.5", now_ms) for k in range(0, count, 2)]
            elif holes:
                conflicts = [(VENUE, symbol, newest_oi - CONFLICT_OFFSET * TF_MS, "95001.5", "7000000001.25", now_ms)]
            else:
                conflicts = []
            conn.executemany(_INSERT_CONFLICT, conflicts)
            if funding:
                rows, span, fconflicts = funding_rows(index, symbol, now_ms)
                conn.executemany(_INSERT_FUNDING, rows)
                conn.execute(_INSERT_FUNDING_SPAN, span)
                conn.executemany(_INSERT_FUNDING_CONFLICT, fconflicts)
            conn.execute("COMMIT")
    finally:
        conn.close()
    return symbols


# --- the client ------------------------------------------------------------------------------------------------------


def make_fetch(base_url: str) -> Fetch:
    """A ``Fetch`` with a new connection per call; the tool's ``http_fetch`` serves the paths it allows."""
    tool_fetch = http_fetch(base_url)
    parts = urlsplit(base_url)
    if parts.scheme != "http" or parts.hostname is None:
        raise ValueError(f"not an http URL: {base_url!r}")
    host, port = parts.hostname, parts.port

    def fetch(path_and_query: str) -> RawResponse:
        if path_and_query.partition("?")[0] != OI_PATH:
            return tool_fetch(path_and_query)
        connection = http.client.HTTPConnection(host, port, timeout=10.0)
        try:
            connection.request("GET", path_and_query, headers={"Accept": "application/json"})
            response = connection.getresponse()
            body = response.read(MAX_BODY_BYTES + 1)
            return RawResponse(response.status, response.getheader("Content-Type"), body)
        except TimeoutError:
            raise TransportError("timeout") from None
        except (OSError, http.client.HTTPException):
            raise TransportError("connection") from None
        finally:
            connection.close()

    return fetch


def _probe(base_url: str, stop: threading.Event, samples: list[float], counters: dict[str, int]) -> None:
    """``GET /health/ready`` every 0.5 s; a transport failure is recorded as the full timeout so AC4 sees it."""
    parts = urlsplit(base_url)
    host = parts.hostname
    if host is None:
        raise ValueError(f"not an http URL: {base_url!r}")
    while not stop.is_set():
        started = time.perf_counter()
        connection = http.client.HTTPConnection(host, parts.port, timeout=PROBE_TIMEOUT_S)
        try:
            connection.request("GET", "/health/ready")
            response = connection.getresponse()
            response.read()
            if response.status == 503:
                counters["http_503"] += 1
            samples.append((time.perf_counter() - started) * 1000)
        except (OSError, http.client.HTTPException):
            samples.append(PROBE_TIMEOUT_S * 1000)
        finally:
            connection.close()
        stop.wait(PROBE_INTERVAL_S)


def _writer(
    db: Path, symbols: Sequence[str], interval_s: float, stop: threading.Event, counters: dict[str, int]
) -> None:
    """Writes the next period for every symbol every ``interval_s`` of the real clock, on its own connection."""
    conn = open_writer(db)
    try:
        while not stop.wait(interval_s):
            oi_time = time.time_ns() // 1_000_000 // TF_MS * TF_MS
            try:
                counters["writer_rows"] += writer_tick(conn, symbols, oi_time - TF_MS, oi_time)
                counters["writer_ticks"] += 1
            except sqlite3.Error:
                counters["writer_errors"] += 1
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
    finally:
        conn.close()


def run_sustained(
    base_url: str, symbols: Sequence[str], db: Path, *, minutes: float, writer_interval_s: float, oi_mode: OiMode
) -> dict[str, Any]:
    """Refreshes for ``minutes`` with a ``/health/ready`` probe and a concurrent writer (spec 7.2, AC4).

    One refresh follows another after the 15 s of the tool's snapshot cache, the heaviest load the tool can put on the
    API. The server's peak memory is read by the caller after the server is reaped.
    """
    fetch = make_fetch(base_url)
    stop = threading.Event()
    probe_ms: list[float] = []
    counters = {"http_503": 0, "writer_rows": 0, "writer_ticks": 0, "writer_errors": 0}
    threads = [
        threading.Thread(target=_probe, args=(base_url, stop, probe_ms, counters), daemon=True),
        threading.Thread(target=_writer, args=(db, symbols, writer_interval_s, stop, counters), daemon=True),
    ]
    for thread in threads:
        thread.start()
    durations: list[float] = []
    problems = 0
    end = time.monotonic() + minutes * 60
    try:
        while time.monotonic() < end:
            refreshed = run_refresh(fetch, oi_mode=oi_mode)
            durations.append(refreshed.duration_s)
            problems += refreshed.failed + refreshed.candle_not_asked + refreshed.oi_not_asked
            stop.wait(SUSTAINED_PAUSE_S)
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=30)
    return {
        "refreshes": len(durations),
        "durations_s": durations,
        "problems": problems,
        "probe_ms": probe_ms,
        **counters,
    }


# --- the command -----------------------------------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bench_overview_refresh", description=__doc__)
    parser.add_argument("--series", type=int, default=300, help="number of symbols (default %(default)s)")
    parser.add_argument("--profile", choices=sorted(PROFILES), default="smoke")
    parser.add_argument("--oi-mode", choices=("real", "proxy"), default="real")
    parser.add_argument(
        "--funding-mode",
        choices=("off", "real"),
        default="off",
        help="real: the API runs with [funding_rate], every refresh is also run with the funding phase (FR-6)",
    )
    parser.add_argument("--refreshes", type=int, default=10)
    parser.add_argument("--sustained-minutes", type=float, default=10.0, help="0 skips the sustained run")
    parser.add_argument("--writer-interval-s", type=float, default=300.0)
    parser.add_argument(
        "--l-tunnel-ms", type=float, default=None, help="median of >= 20 trivial calls through the tunnel"
    )
    parser.add_argument("--stress", action="store_true", help="every answer has the largest number of runs")
    return parser


def environment(series: int) -> dict[str, Any]:
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "date": datetime.now(UTC).isoformat(timespec="seconds"),
        "open_interest_cadence": open_interest_cadence(series, 400),
        "note": "one machine, a generated database, the operating system's file cache is warm after the build",
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.refreshes < 1:
        parser.error("--refreshes must be at least 1")
    candle_rows = PROFILES[args.profile]
    tmp = Path(tempfile.mkdtemp(prefix="quasar-overview-bench-"))
    try:
        symbols = build_database(
            tmp / "bench.db",
            series=args.series,
            candle_rows=candle_rows,
            stress=args.stress,
            now_ms=time.time_ns() // 1_000_000,
            funding=args.funding_mode == "real",
        )
        proc, port = start_api(tmp / "bench.db", symbols, tmp / "api.log", funding=args.funding_mode == "real")
        base_url = f"http://127.0.0.1:{port}"
        try:
            fetch = make_fetch(base_url)
            without: list[RefreshResult] = []
            results: list[RefreshResult] = []
            if args.funding_mode == "real":  # one discarded refresh: the first one meets cold caches
                run_refresh(fetch, oi_mode=args.oi_mode, funding_mode="off")
            for index in range(args.refreshes):  # alternating, and the order inside a pair alternates too (ABBA)
                if args.funding_mode != "real":
                    results.append(run_refresh(fetch, oi_mode=args.oi_mode, funding_mode="off"))
                    continue
                variants: tuple[FundingMode, FundingMode] = ("off", "real") if index % 2 == 0 else ("real", "off")
                for variant in variants:
                    refreshed = run_refresh(fetch, oi_mode=args.oi_mode, funding_mode=variant)
                    (results if variant == "real" else without).append(refreshed)
            sustained = (
                run_sustained(
                    base_url,
                    symbols,
                    tmp / "bench.db",
                    minutes=args.sustained_minutes,
                    writer_interval_s=args.writer_interval_s,
                    oi_mode=args.oi_mode,
                )
                if args.sustained_minutes > 0
                else None
            )
        except BaseException:
            if proc.poll() is None:
                reap(proc)
            raise
        stopped, usage = reap(proc)
        peak = peak_rss_bytes(usage.ru_maxrss)
        output: dict[str, Any] = {
            "environment": environment(args.series),
            "parameters": {
                "series": args.series,
                "profile": args.profile,
                "candle_rows": candle_rows,
                "oi_mode": args.oi_mode,
                "funding_mode": args.funding_mode,
                "refreshes": args.refreshes,
                "sustained_minutes": args.sustained_minutes,
                "writer_interval_s": args.writer_interval_s,
                "l_tunnel_ms": args.l_tunnel_ms,
                "stress": args.stress,
            },
            "evaluate": evaluate(
                results, series=args.series, l_tunnel_ms=args.l_tunnel_ms, funding=args.funding_mode == "real"
            ),
            "breakdown": breakdown(results),
            "durations_s": [result.duration_s for result in results],
            "server": {"stopped": stopped, "peak_rss_bytes": peak},
            "sustained": None,
        }
        print(format_breakdown(f"refresh with funding={args.funding_mode}", output["breakdown"]), file=sys.stderr)
        if without:
            output["without_funding"] = {
                "evaluate": evaluate(without, series=args.series, l_tunnel_ms=args.l_tunnel_ms),
                "breakdown": breakdown(without),
            }
            output["funding_cost"] = funding_cost(without, results)
            print(
                format_breakdown(
                    "the same refreshes without the funding phase", output["without_funding"]["breakdown"]
                ),
                file=sys.stderr,
            )
            cost = output["funding_cost"]
            print(
                f"funding added {cost['added_s']:.2f} s to a refresh of {cost['mean_total_without_s']:.2f} s "
                f"({cost['added_share_of_with'] * 100:.1f} % of the refresh with funding); "
                f"mean funding call {cost['mean_funding_call_ms']:.1f} ms",
                file=sys.stderr,
            )
        if sustained is not None:
            output["sustained"] = {
                **sustained,
                **evaluate_sustained(sustained["probe_ms"], sustained["http_503"], peak, sustained["problems"]),
            }
        print(json.dumps(output, ensure_ascii=False))
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
