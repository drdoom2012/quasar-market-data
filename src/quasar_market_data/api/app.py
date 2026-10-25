"""The HTTP application: ``GET /v1/candles`` and the one error body (contract api-v1 sections 1, 2 and 5).

The handler parses the raw query string by hand so the contract's strictness holds (``400``, not FastAPI's ``422``;
repeated, unknown and empty parameters are visible). Documentation endpoints and redirects are off, so an unknown
path is ``404 not_found`` and a trailing slash is a different path. SQLite runs in a worker thread.
"""

import json
import logging
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import Response

from quasar_market_data.api.candle_reader import CandlesPage
from quasar_market_data.api.database import ConnectionFactory, read_candles
from quasar_market_data.api.errors import ApiError, internal_error, method_not_allowed, not_found
from quasar_market_data.api.params import parse_candles_request
from quasar_market_data.storage.connection import open_reader

logger = logging.getLogger(__name__)

_MEDIA_TYPE = "application/json; charset=utf-8"


def create_app(db_path: Path, *, open_connection: ConnectionFactory = open_reader) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)

    @app.get("/v1/candles")
    async def candles(request: Request) -> Response:
        parsed = parse_candles_request(request.scope["query_string"])
        page = await run_in_threadpool(read_candles, db_path, parsed, open_connection=open_connection)
        return _json(200, _page_body(page))

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError) -> Response:
        return _json(exc.status, exc.body(), exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def http_exception(request: Request, exc: StarletteHTTPException) -> Response:
        if exc.status_code == 404:
            error = not_found()
        elif exc.status_code == 405:
            error = method_not_allowed((exc.headers or {}).get("allow", "GET"))
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
