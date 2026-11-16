"""One detail of one series: the calls, the paging and the cache (design 5.2, 5.5).

A detail is read only when somebody asks. One detail refresh runs at a time (the API is one process and the tool's
container is small); a request that waits too long gets ``busy``. The three parts (candles, open interest, funding)
are loaded independently and each fails closed on its own: a part is ``ok`` only when every page of its range was
read and validated.
"""

import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from typing import Any

from tools.symbol_overview.detail import (
    DetailWindows,
    build_detail,
    candle_change,
    detail_windows,
    open_interest_change,
    part_disabled,
    part_empty,
    part_error,
    part_not_collected,
    part_ok,
)
from tools.symbol_overview.detail_validate import (
    candle_page_result,
    funding_page_result,
    open_interest_page_result,
)
from tools.symbol_overview.fetch import (
    Fetch,
    RawResponse,
    TransportError,
    candles_path,
    funding_rows_path,
    open_interest_rows_path,
)
from tools.symbol_overview.judge import page_error
from tools.symbol_overview.model import (
    MAX_PAGES,
    RANGE_TTL_S,
    TF_MS,
    TIMEFRAME,
    VENUE,
    InstrumentRow,
    Invalid,
)
from tools.symbol_overview.validate import (
    funding_health,
    health_result,
    instruments_result,
    open_interest_health,
)

_UNKNOWN_SERIES = Invalid("api_error", "unknown_series")
_FAILURE_TTL_S = 5.0  # a failed or partly failed detail is kept only briefly: a retry should reach the API soon
_FAILURE_KEYS = 64


@dataclass(frozen=True, slots=True)
class DetailAnswer:
    status: int
    body: bytes  # the JSON, UTF-8, already encoded: the cache holds bytes, not dicts


def _answer(status: int, document: object) -> DetailAnswer:
    return DetailAnswer(status, json.dumps(document, ensure_ascii=False).encode())


def _api_error(status: int, code: str, detail: str = "") -> DetailAnswer:
    return _answer(status, {"api_error": {"code": code, "detail": detail}})


Key = tuple[str, str, str, str]


class Detailer:
    def __init__(
        self,
        fetch: Fetch,
        *,
        deadline_s: float = 30.0,
        wait_s: float = 45.0,
        cache_bytes: int = 16 * 1024 * 1024,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch
        self._deadline_s = deadline_s
        self._wait_s = wait_s
        self._cache_bytes = cache_bytes
        self._monotonic = monotonic
        self._work = threading.Lock()  # one detail refresh at a time
        self._cache_lock = threading.Lock()
        self._good: OrderedDict[Key, tuple[float, DetailAnswer]] = OrderedDict()
        self._good_size = 0
        self._failed: OrderedDict[Key, tuple[float, DetailAnswer]] = OrderedDict()

    def get(self, symbol: str, venue: str, timeframe: str, range_name: str) -> DetailAnswer:
        if venue != VENUE or timeframe != TIMEFRAME:
            return _api_error(404, "unsupported_series")
        key: Key = (symbol, venue, timeframe, range_name)
        cached = self._cached(key)
        if cached is not None:
            return cached
        if not self._work.acquire(timeout=self._wait_s):
            return _api_error(503, "busy")
        try:
            cached = self._cached(key)  # somebody may have loaded the same detail while this request waited
            if cached is not None:
                return cached
            answer, complete = self._build(key)
            self._store(key, answer, complete)
            return answer
        finally:
            self._work.release()

    # --- cache -------------------------------------------------------------------------------------------------

    def _cached(self, key: Key) -> DetailAnswer | None:
        now = self._monotonic()
        with self._cache_lock:
            for table in (self._good, self._failed):
                entry = table.get(key)
                if entry is not None and now < entry[0]:
                    return entry[1]
        return None

    def _store(self, key: Key, answer: DetailAnswer, complete: bool) -> None:
        now = self._monotonic()
        with self._cache_lock:
            self._drop(key)
            if answer.status == 503:
                return
            if answer.status == 200 and complete:
                size = len(answer.body)
                if size > self._cache_bytes:
                    return  # served, never kept
                while self._good and self._good_size + size > self._cache_bytes:
                    _, (_, old) = self._good.popitem(last=False)
                    self._good_size -= len(old.body)
                self._good[key] = (now + RANGE_TTL_S[key[3]], answer)
                self._good_size += size
            else:
                self._failed[key] = (now + _FAILURE_TTL_S, answer)
                while len(self._failed) > _FAILURE_KEYS:
                    self._failed.popitem(last=False)

    def _drop(self, key: Key) -> None:
        old = self._good.pop(key, None)
        if old is not None:
            self._good_size -= len(old[1].body)
        self._failed.pop(key, None)

    # --- one refresh -------------------------------------------------------------------------------------------

    def _call(self, path: str) -> RawResponse | Invalid:
        try:
            return self._fetch(path)
        except TransportError as error:
            return Invalid("transport", error.code)

    def _build(self, key: Key) -> tuple[DetailAnswer, bool]:
        symbol, venue, timeframe, range_name = key
        started = self._monotonic()
        health_raw = self._call("/health/ingest")
        if isinstance(health_raw, Invalid):
            return _answer(502, page_error(health_raw)), False
        health = health_result(health_raw.status, health_raw.content_type, health_raw.body)
        if isinstance(health, Invalid):
            return _answer(502, page_error(health)), False
        listed = self._call("/v1/instruments")
        instruments = (
            listed
            if isinstance(listed, Invalid)
            else instruments_result(listed.status, listed.content_type, listed.body)
        )
        if isinstance(instruments, Invalid):
            return _answer(502, page_error(instruments)), False
        instrument = next(
            (i for i in instruments if (i.venue, i.symbol, i.timeframe) == (venue, symbol, timeframe)), None
        )
        if instrument is None:
            return _api_error(404, "unknown_series"), False
        try:
            windows = detail_windows(health.as_of_ms, range_name)
        except ValueError:
            return _answer(502, page_error(Invalid("schema", "window"))), False

        oi_health = open_interest_health(health_raw.status, health_raw.content_type, health_raw.body, health=health)
        funding_state = funding_health(health_raw.status, health_raw.content_type, health_raw.body, health=health)
        with ThreadPoolExecutor(max_workers=3) as pool:
            candles = pool.submit(self._candles, instrument, windows, started)
            interest = pool.submit(self._open_interest, instrument, windows, started, oi_health)
            funding = pool.submit(self._funding, instrument, windows, started, funding_state)
            parts = (candles.result(), interest.result(), funding.result())
        document = build_detail(
            instrument=instrument,
            range_name=range_name,
            as_of_ms=health.as_of_ms,
            windows=windows,
            candles=parts[0],
            open_interest=parts[1],
            funding_rate=parts[2],
        )
        return _answer(200, document), all(part["status"] != "error" for part in parts)

    def _read_pages(
        self,
        started: float,
        start: int,
        end: int,
        make_path: Callable[[InstrumentRow, int, int], str],
        validate: Callable[..., Any],
        instrument: InstrumentRow,
    ) -> list[Any] | Invalid:
        """All pages of ``[start, end)`` in order, or the first failure: a part is read completely or not at all."""
        pages: list[Any] = []
        cursor = start
        for _ in range(MAX_PAGES):
            if self._monotonic() - started > self._deadline_s:
                return Invalid("deadline")
            raw = self._call(make_path(instrument, cursor, end))
            if isinstance(raw, Invalid):
                return raw
            page = validate(raw.status, raw.content_type, raw.body, instrument=instrument, start=cursor, end=end)
            if isinstance(page, Invalid):
                return page
            pages.append(page)
            if page.next is None:
                return pages
            if page.next <= cursor:
                return Invalid("schema", "pages")  # a page that does not advance would loop
            cursor = page.next
        return Invalid("schema", "pages")

    def _candles(self, instrument: InstrumentRow, windows: DetailWindows, started: float) -> dict[str, object]:
        if instrument.newest_open_time_ms is None:
            return part_empty()
        pages = self._read_pages(
            started, windows.candles.start, windows.candles.end, candles_path, candle_page_result, instrument
        )
        if isinstance(pages, Invalid):
            # candles are never deleted: a series the instruments list says is stored cannot be unknown (C§5.2)
            return part_error(Invalid("inconsistent_with_instruments") if pages == _UNKNOWN_SERIES else pages)
        rows = [row for page in pages for row in page.rows]
        shown = [row for row in rows if row.t >= windows.shown.start]
        return part_ok(
            rows=[asdict(row) for row in shown],
            change=candle_change(rows),
            oldest_ms=pages[-1].oldest_ms,
            newest_ms=pages[-1].newest_ms,
        )

    def _open_interest(
        self,
        instrument: InstrumentRow,
        windows: DetailWindows,
        started: float,
        health: object,
    ) -> dict[str, object]:
        if health is None:
            return part_disabled()
        if isinstance(health, Invalid):
            return part_error(health)
        if instrument.open_interest_not_collected:  # ADR-0003 A-6: an explicit null period, no call is made
            return part_not_collected()
        window = windows.open_interest
        pages = self._read_pages(
            started, window.start, window.end, open_interest_rows_path, open_interest_page_result, instrument
        )
        if isinstance(pages, Invalid):
            return part_empty() if pages == _UNKNOWN_SERIES else part_error(pages)
        rows = [row for page in pages for row in page.rows]
        first_shown = windows.shown.start + TF_MS  # a row at the END of a period belongs to the candle before it
        return part_ok(
            rows=[asdict(row) for row in rows if row.t >= first_shown],
            change=open_interest_change(rows),
            window={"start": first_shown, "end": window.end},
            oldest_ms=pages[-1].oldest_ms,
            newest_ms=pages[-1].newest_ms,
        )

    def _funding(
        self,
        instrument: InstrumentRow,
        windows: DetailWindows,
        started: float,
        health: object,
    ) -> dict[str, object]:
        if health is None:
            return part_disabled()
        if isinstance(health, Invalid):
            return part_error(health)
        window = windows.funding
        pages = self._read_pages(started, window.start, window.end, funding_rows_path, funding_page_result, instrument)
        if isinstance(pages, Invalid):
            return part_empty() if pages == _UNKNOWN_SERIES else part_error(pages)
        rows = [row for page in pages for row in page.rows]
        return part_ok(
            rows=[asdict(row) for row in rows if row.t >= windows.shown.start],
            latest=asdict(rows[-1]) if rows else None,
            oldest_ms=pages[-1].oldest_ms,
            newest_ms=pages[-1].newest_ms,
        )
