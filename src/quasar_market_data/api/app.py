"""The HTTP application: ``GET /v1/candles``, ``GET /v1/coverage``, ``GET /v1/indicators``, ``GET /v1/instruments``,
``GET /v1/open-interest``, health and the one error body (contract api-v1 sections 1, 2, 5, 6.1 to 6.5).

The handler parses the raw query string by hand so the contract's strictness holds (``400``, not FastAPI's ``422``;
repeated, unknown and empty parameters are visible). Documentation endpoints and redirects are off, so an unknown
path is ``404 not_found`` and a trailing slash is a different path. SQLite runs in a worker thread.
"""

import json
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Final
from uuid import uuid4

from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import Response

from quasar_market_data.api.candle_reader import CandlesPage
from quasar_market_data.api.coverage_reader import CoverageReport
from quasar_market_data.api.database import (
    ConnectionFactory,
    read_candles,
    read_coverage,
    read_indicators,
    read_ingest,
    read_ingest_open_interest,
    read_instruments,
    read_open_interest,
    read_ready,
)
from quasar_market_data.api.errors import (
    ApiError,
    internal_error,
    method_not_allowed,
    not_found,
    open_interest_disabled,
)
from quasar_market_data.api.float_text import float_text
from quasar_market_data.api.health_reader import OpenInterestFacts, StoredOpenInterestSeries, StoredSeries
from quasar_market_data.api.indicator_reader import IndicatorInputs
from quasar_market_data.api.indicator_results import indicator_results
from quasar_market_data.api.instrument_reader import InstrumentNewest
from quasar_market_data.api.open_interest_page import OpenInterestPage
from quasar_market_data.api.params import (
    indicators_window_end,
    parse_candles_request,
    parse_coverage_request,
    parse_indicators_request,
    parse_open_interest_request,
    require_no_query,
)
from quasar_market_data.api.rule import checked_rule
from quasar_market_data.config.settings import Instrument
from quasar_market_data.domain.freshness import Freshness, series_freshness
from quasar_market_data.domain.indicator_series import IndicatorPoint, IndicatorSeries, read_start_ms
from quasar_market_data.domain.indicators import IndicatorSpec
from quasar_market_data.domain.open_interest_coverage import OpenInterestRule, open_interest_freshness
from quasar_market_data.domain.timed_coverage import ExpectationRule
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.domain.timestamps import ensure_timestamp_ms
from quasar_market_data.storage.connection import open_reader

logger = logging.getLogger(__name__)

_MEDIA_TYPE = "application/json; charset=utf-8"

HEALTH_BEHIND_MAX: Final = 50  # contract 6.2.3: at most this many non-current series are listed
HEALTH_MISSING_MAX: Final = 50  # contract 6.2.3: at most this many missing configured instruments are listed
CONFLICT_WINDOW_MS: Final = 86_400_000  # contract 6.5.8: a conflict counts when its slot is newer than as_of - 24 h


def system_now_ms() -> int:
    """Epoch milliseconds of the host clock."""
    return time.time_ns() // 1_000_000


def create_app(
    db_path: Path,
    *,
    rule: ExpectationRule,
    universe: Sequence[Instrument],
    open_interest: OpenInterestRule | None = None,
    open_connection: ConnectionFactory = open_reader,
    now_ms: Callable[[], int] = system_now_ms,
) -> FastAPI:
    universe = tuple(sorted(universe))  # the configured instruments, held for the endpoints that report them
    rule = checked_rule(rule.publication_delay_ms, rule.lag_threshold_ms)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)

    @app.get("/v1/candles")
    async def candles(request: Request) -> Response:
        parsed = parse_candles_request(request.scope["query_string"])
        page = await run_in_threadpool(read_candles, db_path, parsed, open_connection=open_connection)
        return _json(200, _page_body(page))

    @app.get("/v1/coverage")
    async def coverage(request: Request) -> Response:
        parsed = parse_coverage_request(request.scope["query_string"])
        as_of_ms = ensure_timestamp_ms(now_ms(), "as_of_ms")  # a wrong host clock is a 500, not a 503
        report = await run_in_threadpool(
            read_coverage, db_path, parsed, as_of_ms=as_of_ms, rule=rule, open_connection=open_connection
        )
        return _json(200, _coverage_body(report))

    @app.get("/v1/indicators")
    async def indicators(request: Request) -> Response:
        # contract 6.3.7: parameters -> host clock -> window of a defaulted end -> database -> series
        parsed = parse_indicators_request(request.scope["query_string"])
        as_of_ms = ensure_timestamp_ms(now_ms(), "as_of_ms")  # a wrong host clock is a 500, not a 503
        end = indicators_window_end(parsed, as_of_ms=as_of_ms, publication_delay_ms=rule.publication_delay_ms)
        timeframe_ms = parsed.timeframe.milliseconds
        read_start = min(read_start_ms(parsed.start, spec.warmup_slots, timeframe_ms) for spec in parsed.indicators)
        inputs = await run_in_threadpool(
            read_indicators, db_path, parsed, end=end, read_start=read_start, open_connection=open_connection
        )
        series = await run_in_threadpool(indicator_results, inputs, parsed.indicators, as_of_ms=as_of_ms, rule=rule)
        return await run_in_threadpool(_indicators_response, inputs, parsed.indicators, series, as_of_ms, rule)

    @app.get("/v1/open-interest")
    async def open_interest_endpoint(request: Request) -> Response:
        # contract 6.5.5: parameters -> enabled in the configuration -> host clock -> database -> series
        parsed = parse_open_interest_request(request.scope["query_string"])
        if open_interest is None:
            raise open_interest_disabled()
        as_of_ms = ensure_timestamp_ms(now_ms(), "as_of_ms")  # a wrong host clock is a 500, not a 503
        page = await run_in_threadpool(
            read_open_interest, db_path, parsed, as_of_ms=as_of_ms, rule=open_interest, open_connection=open_connection
        )
        return _json(200, _open_interest_body(page, as_of_ms, open_interest))

    @app.get("/v1/instruments")
    async def instruments(request: Request) -> Response:
        require_no_query(request.scope["query_string"])
        found = await run_in_threadpool(read_instruments, db_path, universe, open_connection=open_connection)
        return _json(200, {"instruments": [_instrument_body(item) for item in found]})

    @app.get("/health")
    async def liveness(request: Request) -> Response:
        require_no_query(request.scope["query_string"])
        return _json(200, {"status": "alive"})

    @app.get("/health/ready")
    async def readiness(request: Request) -> Response:
        require_no_query(request.scope["query_string"])
        version = await run_in_threadpool(read_ready, db_path, open_connection=open_connection)
        return _json(200, {"status": "ready", "schema_version": version})

    @app.get("/health/ingest")
    async def ingest(request: Request) -> Response:
        require_no_query(request.scope["query_string"])
        as_of_ms = ensure_timestamp_ms(now_ms(), "as_of_ms")  # a wrong host clock is a 500, not a 503
        if open_interest is None:
            stored = await run_in_threadpool(read_ingest, db_path, open_connection=open_connection)
            return _json(200, _ingest_body(stored, as_of_ms, rule, universe))
        stored, facts = await run_in_threadpool(
            read_ingest_open_interest,
            db_path,
            conflicts_after_ms=as_of_ms - CONFLICT_WINDOW_MS,
            open_connection=open_connection,
        )
        return _json(200, _ingest_body(stored, as_of_ms, rule, universe, open_interest, facts))

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError) -> Response:
        return _json(exc.status, exc.body(), exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def http_exception(request: Request, exc: StarletteHTTPException) -> Response:
        if exc.status_code == 404:
            error = not_found()
        elif exc.status_code == 405:
            error = method_not_allowed(_header(exc.headers, "allow", "GET"))
        else:
            error = internal_error()
        return _json(error.status, error.body(), error.headers)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> Response:
        correlation_id = uuid4().hex
        logger.error("unhandled error (correlation id %s)", correlation_id, exc_info=exc)
        error = internal_error()
        return _json(error.status, error.body(), error.headers)

    return app


def _header(headers: Mapping[str, str] | None, name: str, default: str) -> str:
    """Starlette keeps the case the raising code used (``Allow``), so look the name up case-insensitively."""
    for key, value in (headers or {}).items():
        if key.lower() == name:
            return value
    return default


def _json(status: int, body: dict[str, object], headers: dict[str, str] | None = None) -> Response:
    content = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return Response(
        content,
        status_code=status,
        media_type=_MEDIA_TYPE,
        headers={"Cache-Control": "no-store", **(headers or {})},
    )


def _page_body(page: CandlesPage) -> dict[str, object]:
    coverage = page.coverage
    return {
        "venue": page.venue,
        "symbol": page.symbol,
        "timeframe": page.timeframe,
        "start": page.start,
        "end": page.end,
        "series": {
            "oldest_open_time_ms": page.oldest_open_time_ms,
            "newest_open_time_ms": page.newest_open_time_ms,
        },
        "candles": [
            {
                "open_time_ms": c.open_time_ms,
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
                "turnover": c.turnover,
                "conflicted": c.conflicted,
            }
            for c in page.candles
        ],
        "next": page.next,
        "coverage": {
            "span": {"start": page.span_start, "end": page.span_end},
            "slots": coverage.slots,
            "present_slots": coverage.present,
            "conflicted_slots": coverage.conflicted,
            "unavailable_slots": coverage.unavailable,
            "missing_slots": coverage.missing,
            "beyond_stored_slots": coverage.beyond_stored,
            "gap_free": coverage.gap_free,
        },
    }


def _open_interest_body(page: OpenInterestPage, as_of_ms: int, rule: OpenInterestRule) -> dict[str, object]:
    """Keys in the order of contract 6.5.4."""
    c = page.coverage
    return {
        "venue": page.venue,
        "symbol": page.symbol,
        "period": page.period,
        "time_semantics": "period_end",
        "start": page.start,
        "end": page.end,
        "as_of_ms": as_of_ms,
        "rule": {
            "cadence": rule.cadence,
            "expected_after_ms": rule.expected_after_ms,
            "lag_threshold_ms": rule.lag_threshold_ms,
            "retrieval_horizon_ms": rule.retrieval_horizon_ms,
        },
        "series": {"oldest_time_ms": page.oldest_time_ms, "newest_time_ms": page.newest_time_ms},
        "rows": [
            {
                "time_ms": row.time_ms,
                "open_interest": row.open_interest,
                "open_interest_value": row.open_interest_value,
                "conflicted": row.conflicted,
            }
            for row in page.rows
        ],
        "next": page.next,
        "coverage": {
            "span": {"start": page.span_start, "end": page.span_end},
            "slots": c.slots,
            "present_slots": c.present,
            "conflicted_slots": c.conflicted,
            "not_yet_expected_slots": c.not_yet_expected,
            "no_longer_recoverable_slots": c.no_longer_recoverable,
            "due_slots": c.due,
            "stale_slots": c.stale,
            "missing_slots": c.missing,
            "gap_free": c.gap_free,
        },
    }


def _coverage_body(report: CoverageReport) -> dict[str, object]:
    c = report.coverage
    timeframe_ms = Timeframe(report.timeframe).milliseconds
    return {
        "venue": report.venue,
        "symbol": report.symbol,
        "timeframe": report.timeframe,
        "start": report.start,
        "end": report.end,
        "as_of_ms": c.as_of_ms,
        "rule": {
            "publication_delay_ms": c.rule.publication_delay_ms,
            "lag_threshold_ms": c.rule.lag_threshold_ms,
        },
        "series": {
            "oldest_open_time_ms": report.oldest_open_time_ms,
            "newest_open_time_ms": report.newest_open_time_ms,
        },
        "stored_ahead_of_as_of": c.stored_ahead_of_as_of,
        "coverage": {
            "slots": c.slots,
            "present_slots": c.present,
            "conflicted_slots": c.conflicted,
            "unavailable_slots": c.unavailable,
            "missing_slots": c.missing,
            "not_yet_expected_slots": c.not_yet_expected,
            "due_slots": c.due,
            "stale_slots": c.stale,
            "fully_present": c.fully_present,
        },
        "runs": [
            {
                "state": run.state.value,
                "start": run.start_ms,
                "end": run.end_ms,
                "slots": (run.end_ms - run.start_ms) // timeframe_ms,
            }
            for run in c.runs
        ],
    }


def _indicators_response(
    inputs: IndicatorInputs,
    specs: Sequence[IndicatorSpec],
    series: Sequence[IndicatorSeries],
    as_of_ms: int,
    rule: ExpectationRule,
) -> Response:
    """Build and encode the body in the thread pool: the largest body must not stall the event loop (J7)."""
    return _json(200, _indicators_body(inputs, specs, series, as_of_ms, rule))


def _indicators_body(
    inputs: IndicatorInputs,
    specs: Sequence[IndicatorSpec],
    series: Sequence[IndicatorSeries],
    as_of_ms: int,
    rule: ExpectationRule,
) -> dict[str, object]:
    """Keys in the order of contract 6.3.4; ``float_text`` raises on a non-finite value, which ends as a ``500``."""
    return {
        "venue": inputs.venue,
        "symbol": inputs.symbol,
        "timeframe": inputs.timeframe,
        "start": inputs.start,
        "end": inputs.end,
        "as_of_ms": as_of_ms,
        "rule": {"publication_delay_ms": rule.publication_delay_ms, "lag_threshold_ms": rule.lag_threshold_ms},
        "series": {
            "oldest_open_time_ms": inputs.oldest_open_time_ms,
            "newest_open_time_ms": inputs.newest_open_time_ms,
        },
        "indicators": [_indicator_body(spec, one) for spec, one in zip(specs, series, strict=True)],
    }


def _indicator_body(spec: IndicatorSpec, series: IndicatorSeries) -> dict[str, object]:
    points = [_point_body(spec.outputs, point) for point in series.points]
    return {
        "id": spec.id,
        "name": spec.name,
        "params": dict(spec.params),
        "outputs": list(spec.outputs),
        "warmup_slots": spec.warmup_slots,
        "anchor_open_time_ms": series.anchor_open_time_ms,
        "latest": points[-1],
        "points": points,
    }


def _point_body(outputs: Sequence[str], point: IndicatorPoint) -> dict[str, object]:
    values = (
        None
        if point.values is None
        else {name: float_text(value) for name, value in zip(outputs, point.values, strict=True)}
    )
    return {"open_time_ms": point.open_time_ms, "values": values, "reason": point.reason}


def _ingest_body(
    stored: Sequence[StoredSeries],
    as_of_ms: int,
    rule: ExpectationRule,
    universe: Sequence[Instrument],
    open_interest: OpenInterestRule | None = None,
    facts: OpenInterestFacts | None = None,
) -> dict[str, object]:
    """The S17 fields over every stored series, then ``universe``, then ``open_interest`` (last key, contract 6.2.3).

    All come from the one snapshot. ``open_interest`` is ``None`` (JSON ``null``) when open interest is not enabled.
    """
    configured = {(item.venue, item.symbol, item.timeframe) for item in universe}
    stored_triples = {(series.venue, series.symbol, series.timeframe) for series in stored}
    missing = [item for item in universe if (item.venue, item.symbol, item.timeframe) not in stored_triples]
    configured_stored = len(configured & stored_triples)
    counts = dict.fromkeys(Freshness, 0)
    non_current: list[tuple[StoredSeries, Freshness, int]] = []
    for series in stored:
        judged = series_freshness(series.newest_open_time_ms, as_of_ms, Timeframe(series.timeframe), rule)
        counts[judged.state] += 1
        if judged.state is not Freshness.CURRENT:
            non_current.append((series, judged.state, judged.lag_slots))
    non_current.sort(
        key=lambda item: (
            item[1] is not Freshness.STALE,
            -item[2],
            item[0].venue,
            item[0].symbol,
            item[0].timeframe,
        )
    )
    if not stored:
        status = "no_series"
    elif counts[Freshness.STALE]:
        status = "behind"
    elif counts[Freshness.DUE]:
        status = "catching_up"
    else:
        status = "current"
    return {
        "as_of_ms": as_of_ms,
        "rule": {"publication_delay_ms": rule.publication_delay_ms, "lag_threshold_ms": rule.lag_threshold_ms},
        "scope": "stored_series",
        "status": status,
        "series_count": len(stored),
        "current": counts[Freshness.CURRENT],
        "due": counts[Freshness.DUE],
        "stale": counts[Freshness.STALE],
        "behind": [
            {
                "venue": series.venue,
                "symbol": series.symbol,
                "timeframe": series.timeframe,
                "newest_open_time_ms": series.newest_open_time_ms,
                "lag_slots": lag_slots,
                "state": state.value,
                "configured": (series.venue, series.symbol, series.timeframe) in configured,
            }
            for series, state, lag_slots in non_current[:HEALTH_BEHIND_MAX]
        ],
        "truncated": len(non_current) > HEALTH_BEHIND_MAX,
        "universe": {
            "configured_count": len(configured),
            "configured_stored": configured_stored,
            "unconfigured_stored": len(stored) - configured_stored,
            "missing_count": len(missing),
            "missing": [
                {"venue": item.venue, "symbol": item.symbol, "timeframe": item.timeframe}
                for item in missing[:HEALTH_MISSING_MAX]
            ],
            "missing_truncated": len(missing) > HEALTH_MISSING_MAX,
        },
        "open_interest": None
        if open_interest is None or facts is None
        else _open_interest_health(facts, as_of_ms, open_interest, universe),
    }


def _open_interest_health(
    facts: OpenInterestFacts, as_of_ms: int, rule: OpenInterestRule, universe: Sequence[Instrument]
) -> dict[str, object]:
    """The ``open_interest`` object of contract 6.5.8; a series it cannot judge is counted, never a ``500`` (OA9)."""
    configured = {(item.venue, item.symbol) for item in universe}
    step = rule.period.milliseconds
    counts = dict.fromkeys(Freshness, 0)
    unjudged = 0
    non_current: list[tuple[StoredOpenInterestSeries, Freshness, int]] = []
    for series in facts.series:
        if series.period != rule.period.label or series.newest_time_ms % step:
            unjudged += 1
            continue
        judged = open_interest_freshness(series.newest_time_ms, as_of_ms, rule)
        counts[judged.state] += 1
        if judged.state is not Freshness.CURRENT:
            non_current.append((series, judged.state, judged.lag_slots))
    non_current.sort(
        key=lambda item: (
            item[1] is not Freshness.STALE,
            -item[2],
            item[0].venue,
            item[0].symbol,
            item[0].period,
        )
    )
    conflicted_series = len(facts.conflicted)
    if not facts.series:
        status = "no_series"
    elif conflicted_series or unjudged:
        status = "degraded"
    elif counts[Freshness.STALE]:
        status = "behind"
    elif counts[Freshness.DUE]:
        status = "catching_up"
    else:
        status = "current"
    return {
        "as_of_ms": as_of_ms,
        "rule": {
            "cadence": rule.cadence,
            "expected_after_ms": rule.expected_after_ms,
            "lag_threshold_ms": rule.lag_threshold_ms,
        },
        "scope": "stored_series",
        "status": status,
        "series_count": len(facts.series),
        "current": counts[Freshness.CURRENT],
        "due": counts[Freshness.DUE],
        "stale": counts[Freshness.STALE],
        "unjudged_series": unjudged,
        "conflicted_series": conflicted_series,
        "behind": [
            {
                "venue": series.venue,
                "symbol": series.symbol,
                "period": series.period,
                "newest_time_ms": series.newest_time_ms,
                "lag_slots": lag_slots,
                "state": state.value,
                "configured": (series.venue, series.symbol) in configured,
            }
            for series, state, lag_slots in non_current[:HEALTH_BEHIND_MAX]
        ],
        "truncated": len(non_current) > HEALTH_BEHIND_MAX,
    }


def _instrument_body(item: InstrumentNewest) -> dict[str, object]:
    return {
        "venue": item.instrument.venue,
        "symbol": item.instrument.symbol,
        "timeframe": item.instrument.timeframe,
        "newest_open_time_ms": item.newest_open_time_ms,
    }
