import itertools
import json
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "binance"
NAMES = ["btcusdt", "smallprice"]
FIVE_MIN_MS = 300_000
REQUIRED_META_KEYS = {
    "instrument",
    "market_id",
    "market_flags",
    "interval",
    "start_time_ms",
    "end_time_ms",
    "limit",
    "method",
    "params",
    "requests",
    "time_observations",
    "ccxt_version",
    "python_version",
}
MARKET_FLAG_KEYS = {"swap", "linear", "settle", "quote", "active", "expiry"}


def load_rows(name: str) -> list[list[object]]:
    return cast(list[list[object]], json.loads((FIXTURES / name).read_text()))


def load_obj(name: str) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads((FIXTURES / name).read_text()))


def query_of(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def request_labelled(meta: dict[str, Any], label: str) -> dict[str, Any]:
    matches = [r for r in cast(list[dict[str, Any]], meta["requests"]) if r["label"] == label]
    assert len(matches) == 1, f"expected exactly one {label!r} request"
    return matches[0]


@pytest.mark.parametrize("name", NAMES)
def test_raw_rows_have_binance_shape(name: str) -> None:
    rows = load_rows(f"{name}_5m_closed_raw.json")
    assert rows
    for row in rows:
        assert len(row) == 12
        assert isinstance(row[0], int) and isinstance(row[6], int)
        assert all(isinstance(row[i], str) for i in (1, 2, 3, 4, 5, 7))


@pytest.mark.parametrize("name", NAMES)
def test_closed_fixtures_are_complete_and_contiguous(name: str) -> None:
    meta = load_obj(f"{name}_5m_closed.meta.json")
    raw = load_rows(f"{name}_5m_closed_raw.json")
    unified = load_rows(f"{name}_5m_closed_unified.json")
    assert len(raw) == meta["limit"]
    opens = cast(list[int], [r[0] for r in raw])
    assert all(b - a == FIVE_MIN_MS for a, b in itertools.pairwise(opens))
    assert [r[0] for r in unified] == opens
    assert opens[0] == meta["start_time_ms"]


@pytest.mark.parametrize("name", NAMES)
def test_raw_and_unified_requests_target_the_same_market_and_range(name: str) -> None:
    meta = load_obj(f"{name}_5m_closed.meta.json")
    raw_req = request_labelled(meta, "raw_klines")
    uni_req = request_labelled(meta, "unified_klines")
    for req in (raw_req, uni_req):
        assert urlparse(req["url"]).path.startswith("/fapi/")
        assert urlparse(req["url"]).path.endswith("/klines")
    raw_q, uni_q = query_of(raw_req["url"]), query_of(uni_req["url"])
    assert raw_q["symbol"] == uni_q["symbol"] == meta["market_id"]
    for key in ("interval", "startTime", "endTime"):
        assert raw_q[key] == uni_q[key]
    flags = meta["market_flags"]
    assert flags["swap"] is True and flags["linear"] is True
    assert flags["settle"] == "USDT" and flags["quote"] == "USDT"


@pytest.mark.parametrize("name", NAMES)
def test_closed_fixture_close_time_is_exclusive_boundary_minus_one(name: str) -> None:
    meta = load_obj(f"{name}_5m_closed.meta.json")
    raw = load_rows(f"{name}_5m_closed_raw.json")
    for row in raw:
        assert cast(int, row[6]) == cast(int, row[0]) + FIVE_MIN_MS - 1
    assert cast(int, raw[-1][6]) < meta["time_observations"][0]["server_time_ms"]


@pytest.mark.parametrize("name", NAMES)
def test_unified_row_shape_is_recorded(name: str) -> None:
    for row in load_rows(f"{name}_5m_closed_unified.json"):
        assert len(row) == 6


def test_forming_fixture_last_row_is_not_closed_at_capture() -> None:
    meta = load_obj("btcusdt_5m_forming.meta.json")
    raw = load_rows("btcusdt_5m_forming_raw.json")
    assert len(raw) == meta["limit"] == 2
    assert cast(int, raw[-1][6]) >= meta["time_observations"][0]["server_time_ms"]


def all_meta_files() -> list[Path]:
    return sorted(FIXTURES.glob("*.meta.json"))


def test_time_observations_are_well_formed() -> None:
    files = all_meta_files()
    assert files
    for path in files:
        meta = cast(dict[str, Any], json.loads(path.read_text()))
        observations = cast(list[dict[str, Any]], meta["time_observations"])
        assert observations, path.name
        for obs in observations:
            assert obs["monotonic_after_ns"] >= obs["monotonic_before_ns"], path.name
            assert isinstance(obs["server_time_ms"], int), path.name
            assert "wall_before_ms" in obs and "wall_after_ms" in obs, path.name


def test_meta_has_required_keys() -> None:
    files = all_meta_files()
    assert files
    for path in files:
        meta = cast(dict[str, Any], json.loads(path.read_text()))
        assert REQUIRED_META_KEYS <= set(meta), (path.name, REQUIRED_META_KEYS - set(meta))
        assert MARKET_FLAG_KEYS <= set(meta["market_flags"]), path.name


def test_market_survey_mapping_is_lossless() -> None:
    survey = load_obj("usdm_perpetual_markets.json")
    markets = cast(list[dict[str, Any]], survey["markets"])
    assert markets
    symbols = [m["symbol"] for m in markets]
    ids = [m["id"] for m in markets]
    assert len(set(symbols)) == len(symbols)
    assert len(set(ids)) == len(ids)
    assert len({(m["symbol"], m["id"]) for m in markets}) == len(markets)
    for m in markets:
        assert m["settle"] == "USDT" and m["quote"] == "USDT" and m["expiry"] is None
    for name in NAMES:
        meta = load_obj(f"{name}_5m_closed.meta.json")
        assert (meta["instrument"], meta["market_id"]) in {(m["symbol"], m["id"]) for m in markets}
    assert {"captured_at_wall_ms", "ccxt_version", "excluded_counts"} <= set(survey)


def test_boundary_probe_fixture_shape() -> None:
    probe = load_obj("btcusdt_5m_boundary_probe.json")
    boundaries = cast(list[dict[str, Any]], probe["boundaries"])
    assert len(boundaries) == 3
    starts = [b["boundary_ms"] for b in boundaries]
    assert all(b - a == FIVE_MIN_MS for a, b in itertools.pairwise(starts))
    assert all(s % FIVE_MIN_MS == 0 for s in starts)
    for b in boundaries:
        assert [p["offset_s"] for p in b["polls"]] == [0.2, 1, 2, 3, 5, 8, 13]
        for p in b["polls"]:
            row = p["row"]
            assert row == "absent" or (len(row) == 12 and row[0] == b["boundary_ms"] - FIVE_MIN_MS)
            obs = p["time_observation"]
            assert obs["monotonic_after_ns"] >= obs["monotonic_before_ns"]


def test_boundary_probe_meta_counts_match_polls() -> None:
    meta = load_obj("btcusdt_5m_boundary_probe.meta.json")
    assert len(meta["time_observations"]) == 21
    assert len(meta["requests"]) == 42


def test_recheck_fixture_covers_the_same_range_one_hour_later() -> None:
    first_meta = load_obj("btcusdt_5m_closed.meta.json")
    meta = load_obj("btcusdt_5m_recheck.meta.json")
    first, again = load_rows("btcusdt_5m_closed_raw.json"), load_rows("btcusdt_5m_recheck_raw.json")
    assert [r[0] for r in again] == [r[0] for r in first]
    assert (meta["start_time_ms"], meta["end_time_ms"]) == (first_meta["start_time_ms"], first_meta["end_time_ms"])
    assert meta["market_id"] == first_meta["market_id"]
    assert meta["seconds_since_first_capture"] >= 3600
    assert all(len(row) == 12 for row in again)


def test_extended_boundary_probe_fixture_shape() -> None:
    probe = load_obj("btcusdt_5m_boundary_probe_extended.json")
    boundaries = cast(list[dict[str, Any]], probe["boundaries"])
    assert len(boundaries) == 10
    starts = [b["boundary_ms"] for b in boundaries]
    assert all(b - a == FIVE_MIN_MS for a, b in itertools.pairwise(starts))
    for b in boundaries:
        assert [p["offset_s"] for p in b["polls"]] == [0.2, 1, 2, 3, 5, 8, 13, 20, 30, 45, 60]
        for p in b["polls"]:
            row = p["row"]
            assert row == "absent" or (len(row) == 12 and row[0] == b["boundary_ms"] - FIVE_MIN_MS)


def test_extended_boundary_probe_meta_counts_match_polls() -> None:
    meta = load_obj("btcusdt_5m_boundary_probe_extended.meta.json")
    assert len(meta["time_observations"]) == 110
    assert len(meta["requests"]) == 220
