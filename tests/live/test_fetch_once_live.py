"""Live check of ``quasar-collector fetch-once`` against Binance USDT-M public endpoints (``make live``).

Excluded from ``make check``. A failure here (exit 1, 3 or 4, or a conflict) is a finding about the live path
or the async CCXT client, not something to loosen. The one exception is a round trip over the ADR-0001 limit:
that is reported as a skip with the reason, because it says something about the network, not the code. Public endpoints only; no credentials.
"""

import sqlite3
import time
from pathlib import Path

import pytest

from quasar_market_data.collector.cli import main
from quasar_market_data.domain.closed_candles import DEFAULT_PUBLICATION_DELAY_MS

SYMBOL = "BTC/USDT:USDT"
OPEN_TIME, TURNOVER = 3, 9  # column positions in SELECT * FROM candles
FIVE_MINUTES_MS = 300_000


def snapshot(db: Path) -> list[tuple[object, ...]]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT * FROM candles ORDER BY open_time_ms").fetchall()
    finally:
        conn.close()


def counts(stdout: str) -> dict[str, int]:
    parsed: dict[str, int] = {}
    for token in stdout.splitlines()[-1].split():
        key, _, value = token.partition("=")
        parsed[key] = int(value)
    return parsed


SLOW_NETWORK = ("RTT_TOO_LARGE", "UNCERTAINTY_TOO_LARGE")  # ADR-0001 P-4 limits: 2000 ms round trip


def fetch(argv: list[str], capsys: pytest.CaptureFixture[str]) -> dict[str, int]:
    """Run the command; a round trip over the ADR-0001 limit is the network, not the code: skip with the reason."""
    code = main(argv)
    captured = capsys.readouterr()
    if code == 1 and any(f"reason={reason}" in captured.out for reason in SLOW_NETWORK):
        pytest.skip(f"network too slow for trusted server time (ADR-0001 P-4): {captured.out.splitlines()[1]}")
    assert code == 0, captured.err
    return counts(captured.out)


def assert_closed(rows: list[tuple[object, ...]]) -> None:
    now_ms = time.time_ns() // 1_000_000
    for row in rows:
        open_time = row[OPEN_TIME]
        assert isinstance(open_time, int)
        assert open_time + FIVE_MINUTES_MS + DEFAULT_PUBLICATION_DELAY_MS <= now_ms  # no forming candle
        assert isinstance(row[TURNOVER], str)  # Binance provides the quote volume


@pytest.mark.live
def test_fetch_once_against_binance_usdm(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "market.db"
    argv = ["fetch-once", "--db", str(db), "--symbol", SYMBOL, "--limit", "20"]

    first = fetch(argv, capsys)
    before = snapshot(db)
    assert 1 <= len(before) <= 20
    assert first["inserted"] == len(before)
    assert_closed(before)

    # The second run can cross a 5-minute boundary and legitimately insert one newer candle (and shift the
    # 20-row window), so the assertions are about existing rows and conflicts, not about exact counts.
    second = fetch(argv, capsys)
    after = snapshot(db)

    assert all(row in after for row in before)  # nothing overwritten; ingested_at_ms is unchanged too
    assert len(after) - len(before) == second["inserted"]
    newest_before = max(row[OPEN_TIME] for row in before)  # type: ignore[type-var]
    added = [row for row in after if row not in before]
    assert all(row[OPEN_TIME] > newest_before for row in added)  # type: ignore[operator]
    assert_closed(added)
    assert second["duplicates"] >= 1
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT count(*) FROM candle_conflicts").fetchone() == (0,)
    finally:
        conn.close()
