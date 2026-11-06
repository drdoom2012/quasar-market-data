"""The pure core of the symbol detail: windows, exact change figures, the document (design 5.2)."""

import pytest

from tests.tools.symbol_overview.helpers import T0, instrument
from tools.symbol_overview.detail import (
    build_detail,
    candle_change,
    detail_windows,
    open_interest_change,
    part_disabled,
    part_empty,
    part_error,
    part_ok,
    percent_change,
)
from tools.symbol_overview.model import TF_MS, CandleRow, Invalid, OiRow, Window


def candle(t: int, close: str = "100") -> CandleRow:
    return CandleRow(t, close, close, close, close, "1", None, False)


def oi_row(t: int, value: str = "100") -> OiRow:
    return OiRow(t, "1", value, False)


def test_windows_of_every_range() -> None:
    w = detail_windows(T0 + 1000, "1h")
    end = T0 + TF_MS
    assert w.shown == Window(end - 12 * TF_MS, end)
    assert w.candles == Window(w.shown.start - TF_MS, end)
    assert w.open_interest == Window(w.shown.start + TF_MS - 12 * TF_MS, end + TF_MS)
    assert w.funding.start == end - 2016 * TF_MS and w.funding.end == T0 + 1000

    long = detail_windows(T0 + 1000, "30d")
    assert long.funding.start == long.shown.start
    assert (long.candles.end - long.candles.start) // TF_MS == 8641  # at most 10 000 slots per request
    assert (long.open_interest.end - long.open_interest.start) // TF_MS == 8652


@pytest.mark.parametrize(("as_of", "range_name"), [(T0, "2h"), (10, "1h"), ("x", "1h"), (True, "1h"), (T0, "")])
def test_an_unknown_range_or_bad_clock_is_a_value_error(as_of: object, range_name: str) -> None:
    with pytest.raises(ValueError):
        detail_windows(as_of, range_name)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("new", "old", "expected"),
    [
        ("0.3", "0.1", "+200.00"),
        ("82744.5", "82709", "+0.04"),
        ("99", "100", "-1.00"),
        ("1", "0", None),
        ("1.00001", "1", "0.00"),
        ("0.99999", "1", "0.00"),
        ("1.000", "1.004", "-0.40"),
        ("x", "1", None),
        ("1", "x", None),
    ],
)
def test_percent_change_is_exact_decimal(new: str, old: str, expected: str | None) -> None:
    assert percent_change(new, old) == expected


@pytest.mark.parametrize(("new", "old"), [("1e999999", "1"), ("NaN", "1"), ("", "1"), ("Infinity", "1"), ("1", "sNaN")])
def test_percent_change_never_raises(new: str, old: str) -> None:
    assert percent_change(new, old) is None


def test_candle_change_needs_the_adjacent_slot() -> None:
    t = T0
    assert candle_change([candle(t, "100"), candle(t + TF_MS, "101")]) == {"percent": "+1.00", "previous_close": "100"}
    assert candle_change([candle(t, "100"), candle(t + 2 * TF_MS, "101")]) is None
    assert candle_change([candle(t)]) is None
    assert candle_change([]) is None


def test_open_interest_change_uses_the_row_one_hour_earlier() -> None:
    reference = T0
    rows = [oi_row(reference, "200"), oi_row(reference + 6 * TF_MS, "210"), oi_row(reference + 12 * TF_MS, "220")]
    assert open_interest_change(rows) == {"percent": "+10.00", "reference_ms": reference}
    assert open_interest_change(rows[1:]) is None
    assert open_interest_change([]) is None


def test_part_helpers_have_the_fixed_shapes() -> None:
    assert part_error(Invalid("api_error", "unknown_series")) == {
        "status": "error",
        "code": "api_error",
        "detail": "unknown_series",
    }
    assert part_ok(rows=[]) == {"status": "ok", "code": "", "detail": "", "rows": []}
    assert part_empty()["status"] == "empty" and part_disabled()["status"] == "disabled"


def test_build_detail_has_the_documented_keys() -> None:
    windows = detail_windows(T0, "24h")
    document = build_detail(
        instrument=instrument(),
        range_name="24h",
        as_of_ms=T0,
        windows=windows,
        candles=part_empty(),
        open_interest=part_disabled(),
        funding_rate=part_empty(),
    )
    assert set(document) == {
        "symbol",
        "venue",
        "timeframe",
        "range",
        "as_of_ms",
        "window",
        "candles",
        "open_interest",
        "funding_rate",
    }
    assert document["window"] == {"start": windows.shown.start, "end": windows.shown.end}
