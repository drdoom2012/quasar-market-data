"""Structural validation of real Binance USD-M kline fixtures.

This checks **structural validity only**: every recorded row maps to a ``Candle`` and passes
``require_valid``. It does **not** establish that a candle is eligible to be stored as closed; closed-candle
eligibility (publication delay, boundary equality, clock uncertainty) belongs to S5 and uses the rules in
ADR-0001. Structural validity does not depend on closure, so the forming rows are checked as well.

Raw index mapping per ADR-0001: ``open_time=row[0]``, open/high/low/close=``row[1..4]``, volume=``row[5]``,
turnover=``row[7]``.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from quasar_market_data.domain.candle import Candle, CandleKey
from quasar_market_data.domain.decimal_text import canonical_decimal
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.domain.validation import CandleValidationError, ViolationCode, require_valid

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "binance"
VENUE = "binance-usdm"

# (raw fixture, meta fixture)
CLOSED_FIXTURES = (
    ("btcusdt_5m_closed_raw.json", "btcusdt_5m_closed.meta.json"),
    ("smallprice_5m_closed_raw.json", "smallprice_5m_closed.meta.json"),
)
FORMING_FIXTURES = (("btcusdt_5m_forming_raw.json", "btcusdt_5m_forming.meta.json"),)
ALL_FIXTURES = CLOSED_FIXTURES + FORMING_FIXTURES


def load_rows(raw_name: str, meta_name: str) -> tuple[list[list[Any]], dict[str, Any]]:
    rows: list[list[Any]] = json.loads((FIXTURES / raw_name).read_text())
    meta: dict[str, Any] = json.loads((FIXTURES / meta_name).read_text())
    return rows, meta


def raw_texts(row: list[Any]) -> dict[str, str]:
    return {
        "open": row[1],
        "high": row[2],
        "low": row[3],
        "close": row[4],
        "volume": row[5],
        "turnover": row[7],
    }


def candle_from_row(row: list[Any], symbol: str) -> Candle:
    texts = raw_texts(row)
    return Candle(
        key=CandleKey(VENUE, symbol, Timeframe("5m"), row[0]),
        open=texts["open"],
        high=texts["high"],
        low=texts["low"],
        close=texts["close"],
        volume=texts["volume"],
        turnover=texts["turnover"],
    )


@pytest.mark.parametrize(("raw_name", "meta_name"), ALL_FIXTURES)
def test_all_fixture_rows_are_structurally_valid(raw_name: str, meta_name: str) -> None:
    rows, meta = load_rows(raw_name, meta_name)
    assert meta["interval"] == "5m"
    assert rows
    for row in rows:
        candle = candle_from_row(row, meta["instrument"])
        assert require_valid(candle) is candle


@pytest.mark.parametrize(("raw_name", "meta_name"), ALL_FIXTURES)
def test_fixture_rows_keep_canonical_text(raw_name: str, meta_name: str) -> None:
    rows, meta = load_rows(raw_name, meta_name)
    for row in rows:
        candle = candle_from_row(row, meta["instrument"])
        for name, raw_text in raw_texts(row).items():
            assert getattr(candle, name) == canonical_decimal(raw_text), (row[0], name)


def test_fixture_row_counts() -> None:
    counts = {raw: len(load_rows(raw, meta)[0]) for raw, meta in ALL_FIXTURES}
    assert counts == {
        "btcusdt_5m_closed_raw.json": 100,
        "smallprice_5m_closed_raw.json": 100,
        "btcusdt_5m_forming_raw.json": 2,
    }


def test_mutated_fixture_row_is_rejected_by_the_same_helper() -> None:
    rows, meta = load_rows(*CLOSED_FIXTURES[0])
    row = list(rows[0])
    assert row[2] != row[3]
    row[2], row[3] = row[3], row[2]  # swap high and low: high < low
    with pytest.raises(CandleValidationError) as excinfo:
        require_valid(candle_from_row(row, meta["instrument"]))
    assert ViolationCode.HIGH_BELOW_LOW in {v.code for v in excinfo.value.violations}
