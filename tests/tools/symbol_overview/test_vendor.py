"""The vendored chart library is exactly the recorded file and ships with its license and notice (spec 5.4)."""

import hashlib
from pathlib import Path

VENDOR = Path(__file__).resolve().parents[3] / "tools" / "symbol_overview" / "vendor"
VENDOR_SHA256 = "e21cc5caa0226ef30bd8549c50b9ef926615f2a4ee6b4e486353477a55f598cf"


def test_the_vendored_library_is_the_recorded_file() -> None:
    assert hashlib.sha256((VENDOR / "lightweight-charts.js").read_bytes()).hexdigest() == VENDOR_SHA256
    assert VENDOR_SHA256 in (VENDOR / "README.md").read_text(encoding="utf-8")


def test_the_license_ships_with_the_library() -> None:
    assert "Apache License" in (VENDOR / "LICENSE").read_text(encoding="utf-8")


def test_the_readme_names_the_version() -> None:
    assert "5.2.1" in (VENDOR / "README.md").read_text(encoding="utf-8")


def test_the_notice_names_tradingview() -> None:
    notice = (VENDOR / "NOTICE").read_text(encoding="utf-8")
    assert "TradingView" in notice and "https://www.tradingview.com/" in notice
