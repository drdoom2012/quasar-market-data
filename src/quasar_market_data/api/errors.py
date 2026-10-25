"""The one error body of the API (contract section 2) and its constructors."""

from collections.abc import Mapping
from typing import Final, Literal

type ErrorCode = Literal[
    "invalid_parameter", "unknown_series", "not_found", "method_not_allowed", "internal_error", "not_ready"
]
type NotReadyReason = Literal["database_missing", "schema_mismatch", "database_unreadable"]

RECEIVED_MAX_CHARS: Final = 64


def cut_received(raw: str) -> str:
    return raw[:RECEIVED_MAX_CHARS]


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: ErrorCode,
        message: str,
        details: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details: dict[str, object] = dict(details or {})
        self.headers: dict[str, str] = dict(headers or {})

    def body(self) -> dict[str, object]:
        return {"error": {"code": self.code, "message": self.message, "details": dict(self.details)}}


def invalid_parameter(
    message: str, *, parameter: str | None = None, received: str | None = None, maximum: int | None = None
) -> ApiError:
    details: dict[str, object] = {}
    if parameter is not None:
        details["parameter"] = parameter
    if received is not None:
        details["received"] = cut_received(received)
    if maximum is not None:
        details["maximum"] = maximum
    return ApiError(400, "invalid_parameter", message, details)


def unknown_series(venue: str, symbol: str, timeframe: str) -> ApiError:
    return ApiError(
        404,
        "unknown_series",
        "no candle of this series has been stored",
        {"venue": venue, "symbol": symbol, "timeframe": timeframe},
    )


def not_found() -> ApiError:
    return ApiError(404, "not_found", "unknown path")


def method_not_allowed(allow: str) -> ApiError:
    return ApiError(405, "method_not_allowed", "method not allowed", {"allow": allow}, {"Allow": allow})


def internal_error() -> ApiError:
    return ApiError(500, "internal_error", "internal error")


def not_ready(reason: NotReadyReason) -> ApiError:
    headers = {} if reason == "schema_mismatch" else {"Retry-After": "5"}
    return ApiError(503, "not_ready", "the database is not ready", {"reason": reason}, headers)
