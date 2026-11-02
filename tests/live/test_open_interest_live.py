"""Live check of ``quasar-collector fetch-oi-once`` against Binance USDT-M public endpoints (``make live``).

Excluded from ``make check``. A failure here (exit 1, 3 or 4, or a conflict) is a finding about the live path, the
request rules or the async CCXT client, not something to loosen. A round trip over the ADR-0001 limit is reported
as a skip with the reason, because it says something about the network, not the code. Public endpoints only; no
credentials. The command makes three requests at most (server time, history, and the same again on the second run).
"""

import sqlite3
import time
from pathlib import Path

import pytest

from quasar_market_data.collector.cli import main
from quasar_market_data.domain.open_interest import DEFAULT_OPEN_INTEREST_PUBLICATION_DELAY_MS

SYMBOL = "BTC/USDT:USDT"
FIVE_MINUTES_MS = 300_000
SLOW_NETWORK = ("RTT_TOO_LARGE", "UNCERTAINTY_TOO_LARGE")  # ADR-0001 P-4 limits: 2000 ms round trip


def rows(db: Path) -> list[tuple[int, str, str]]:
    conn = sqlite3.connect(db)
    try:
        return [
            (t, oi, value)
            for t, oi, value in conn.execute(
                "SELECT time_ms, open_interest, open_interest_value FROM open_interest ORDER BY time_ms"
            )
        ]
    finally:
        conn.close()


def fetch(argv: list[str], capsys: pytest.CaptureFixture[str]) -> dict[str, int]:
    code = main(argv)
    captured = capsys.readouterr()
    if code == 1 and any(f"reason={reason}" in captured.out for reason in SLOW_NETWORK):
        pytest.skip(f"network too slow for trusted server time (ADR-0001 P-4): {captured.out.splitlines()[1]}")
    assert code == 0, captured.err
    counts: dict[str, int] = {}
    for token in captured.out.splitlines()[-1].split():
        key, _, value = token.partition("=")
        counts[key] = int(value)
    return counts


@pytest.mark.live
def test_fetch_oi_once_against_binance_usdm(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "market.db"
    argv = ["fetch-oi-once", "--db", str(db), "--symbol", SYMBOL, "--periods", "3"]

    first = fetch(argv, capsys)
    before = rows(db)
    # The two older period ends are at least five minutes old, far beyond the observed publication time (186.8 s).
    assert 2 <= len(before) <= 3
    assert first["inserted"] == len(before)
    now_ms = time.time_ns() // 1_000_000
    for time_ms, open_interest, value in before:
        assert time_ms % FIVE_MINUTES_MS == 0
        assert time_ms + DEFAULT_OPEN_INTEREST_PUBLICATION_DELAY_MS <= now_ms  # no row from the future
        assert float(open_interest) >= 0 and float(value) >= 0  # decimal text, never computed by us
    assert [t for t, _, _ in before] == sorted({t for t, _, _ in before})

    # The second run can cross a 5-minute boundary and add a newer row; existing rows and conflicts are what counts.
    second = fetch(argv, capsys)
    after = rows(db)
    assert all(row in after for row in before)  # nothing overwritten
    assert second["duplicates"] >= 1
    assert second["conflicts"] == 0
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT count(*) FROM open_interest_conflicts").fetchone() == (0,)
    finally:
        conn.close()
