"""The only code that talks to the API: read-only GETs of four paths (spec 5.1, OI-6 design 8.1)."""

import http.client
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

from tools.symbol_overview.model import MAX_BODY_BYTES, OI_PATH, InstrumentRow, Window

ALLOWED_PATHS = frozenset({"/health/ingest", "/v1/instruments", "/v1/coverage", OI_PATH})


@dataclass(frozen=True)
class RawResponse:
    status: int
    content_type: str | None
    body: bytes


class TransportError(Exception):
    """The API could not be reached or did not answer in time. ``code`` is ``timeout`` or ``connection``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


Fetch = Callable[[str], RawResponse]  # argument: path plus query


def coverage_path(instrument: InstrumentRow, window: Window) -> str:
    """The coverage request with exactly the documented parameters (C§6.1.1).

    ``/`` and ``:`` are legal unencoded in a query string; everything else that is not safe is percent-encoded
    (C§1), including ``&``, ``+``, spaces and non-ASCII characters.
    """
    return (
        f"/v1/coverage?venue={quote(instrument.venue, safe='')}&symbol={quote(instrument.symbol, safe='/:')}"
        f"&timeframe={quote(instrument.timeframe, safe='')}&start={window.start}&end={window.end}"
    )


def open_interest_path(instrument: InstrumentRow, oi_window: Window) -> str:
    """The open interest coverage request with exactly the documented parameters (C§6.6.1); same encoding as above."""
    return (
        f"{OI_PATH}?venue={quote(instrument.venue, safe='')}&symbol={quote(instrument.symbol, safe='/:')}"
        f"&period={quote(instrument.timeframe, safe='')}&start={oi_window.start}&end={oi_window.end}"
    )


def http_fetch(base_url: str, timeout_s: float = 10.0) -> Fetch:
    """A ``Fetch`` for the API at ``base_url``: GET only, four allowed paths, no redirects, capped body.

    ``timeout_s`` applies to each socket operation, not to the whole call; the refresh deadline bounds the total.
    """
    parts = urlsplit(base_url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"not an http(s) URL: {base_url!r}")
    host, port, secure = parts.hostname, parts.port, parts.scheme == "https"

    def fetch(path_and_query: str) -> RawResponse:
        path = path_and_query.partition("?")[0]
        if path not in ALLOWED_PATHS:
            raise ValueError(f"path not allowed: {path!r}")
        connection_class = http.client.HTTPSConnection if secure else http.client.HTTPConnection
        connection = connection_class(host, port, timeout=timeout_s)
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
