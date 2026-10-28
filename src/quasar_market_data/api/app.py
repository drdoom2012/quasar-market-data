"""The HTTP application: ``GET /v1/candles``, ``GET /v1/coverage`` and the one error body (contract api-v1 sections 1, 2, 5 and 6.1).

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
from quasar_market_data.api.database import ConnectionFactory, read_candles, read_coverage, read_ingest, read_ready
from quasar_market_data.api.errors import ApiError, internal_error, method_not_allowed, not_found
from quasar_market_data.api.health_reader import StoredSeries
from quasar_market_data.api.params import parse_candles_request, parse_coverage_request, require_no_query
from quasar_market_data.api.rule import DEFAULT_RULE, checked_rule
from quasar_market_data.domain.freshness import Freshness, series_freshness
from quasar_market_data.domain.timed_coverage import ExpectationRule
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.domain.timestamps import ensure_timestamp_ms
from quasar_market_data.storage.connection import open_reader

logger = logging.getLogger(__name__)

_MEDIA_TYPE = "application/json; charset=utf-8"

HEALTH_BEHIND_MAX: Final = 50  # contract 6.2.3: at most this many non-current series are listed


def system_now_ms() -> int:
    """Epoch milliseconds of the host clock."""
    return time.time_ns() // 1_000_000


def create_app(
    db_path: Path,
    *,
    open_connection: ConnectionFactory = open_reader,
    now_ms: Callable[[], int] = system_now_ms,
    rule: ExpectationRule = DEFAULT_RULE,
) -> FastAPI:
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
        stored = await run_in_threadpool(read_ingest, db_path, open_connection=open_connection)
        return _json(200, _ingest_body(stored, as_of_ms, rule))

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


def _ingest_body(stored: Sequence[StoredSeries], as_of_ms: int, rule: ExpectationRule) -> dict[str, object]:
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
            }
            for series, state, lag_slots in non_current[:HEALTH_BEHIND_MAX]
        ],
        "truncated": len(non_current) > HEALTH_BEHIND_MAX,
    }
