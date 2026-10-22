"""THROWAWAY spike: capture Binance USDT-M fixtures (Task 3). Not product code.

Usage (network access, run by hand):
    uv run --group spike python spikes/capture_binance.py closed     # closed ranges + forming + survey
    uv run --group spike python spikes/capture_binance.py recheck    # >= 1 h after `closed`
    uv run --group spike python spikes/capture_binance.py probe      # ~15 min boundary smoke test

Request recording mechanism: `Exchange.fetch` is wrapped on the instance and every outgoing
(method, url) is appended to a log. CCXT's own `last_request_url` only holds the last request,
so it is not used. This is recorded in each meta file as `request_recording`.
"""

import json
import sys
import time
from pathlib import Path
from typing import Any

import ccxt

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "binance"
INTERVAL = "5m"
STEP_MS = 300_000
N = 100
BTC = "BTC/USDT:USDT"
PROBE_OFFSETS_S = [0.2, 1, 2, 3, 5, 8, 13]
PROBE_BOUNDARIES = 3
RECORDING = "wrapped Exchange.fetch on the instance; every outgoing (method, url) logged"


class Session:
    def __init__(self) -> None:
        self.ex = ccxt.binanceusdm()
        self.log: list[dict[str, str]] = []
        original = self.ex.fetch

        def recording_fetch(url: str, method: str = "GET", headers: Any = None, body: Any = None) -> Any:
            self.log.append({"method": method, "url": url})
            return original(url, method, headers, body)

        self.ex.fetch = recording_fetch  # type: ignore[method-assign]
        self.ex.load_markets()

    def mark(self) -> int:
        return len(self.log)

    def since(self, mark: int, label: str) -> dict[str, str]:
        new = self.log[mark:]
        if len(new) != 1:
            raise RuntimeError(f"{label}: expected exactly 1 outgoing request, saw {len(new)}: {new}")
        return {"label": label, **new[0]}

    def time_observation(self) -> tuple[dict[str, Any], dict[str, str]]:
        mark = self.mark()
        wall_before = time.time_ns() // 1_000_000
        mono_before = time.monotonic_ns()
        response = self.ex.fapiPublicGetTime()
        mono_after = time.monotonic_ns()
        wall_after = time.time_ns() // 1_000_000
        obs = {
            "wall_before_ms": wall_before,
            "wall_after_ms": wall_after,
            "monotonic_before_ns": mono_before,
            "monotonic_after_ns": mono_after,
            "server_time_ms": int(response["serverTime"]),
        }
        return obs, self.since(mark, "time")

    def market_flags(self, symbol: str) -> dict[str, Any]:
        m = self.ex.market(symbol)
        return {k: m.get(k) for k in ("swap", "linear", "settle", "quote", "active", "expiry")}


def write_json(name: str, data: Any) -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    (FIXTURES / name).write_text(json.dumps(data, indent=1) + "\n")
    print("wrote", name)


def base_meta(s: Session, symbol: str, **extra: Any) -> dict[str, Any]:
    return {
        "instrument": symbol,
        "market_id": s.ex.market(symbol)["id"],
        "market_flags": s.market_flags(symbol),
        "interval": INTERVAL,
        "ccxt_version": ccxt.__version__,
        "python_version": sys.version,
        "request_recording": RECORDING,
        **extra,
    }


def align_5m(ms: int) -> int:
    return ms - ms % STEP_MS


def survey_filter(m: dict[str, Any]) -> str | None:
    """Return the first exclusion reason, or None if the market passes."""
    if m.get("swap") is not True:
        return "not_swap"
    if m.get("linear") is not True:
        return "not_linear"
    if m.get("settle") != "USDT":
        return "settle_not_usdt"
    if m.get("quote") != "USDT":
        return "quote_not_usdt"
    if m.get("expiry") is not None:
        return "has_expiry"
    return None


def capture_survey(s: Session) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    excluded: dict[str, int] = {}
    for m in s.ex.markets.values():
        reason = survey_filter(m)
        if reason:
            excluded[reason] = excluded.get(reason, 0) + 1
            continue
        kept.append({k: m.get(k) for k in ("symbol", "id", "base", "quote", "settle", "active", "expiry")})
    kept.sort(key=lambda m: m["symbol"])
    write_json(
        "usdm_perpetual_markets.json",
        {
            "captured_at_wall_ms": time.time_ns() // 1_000_000,
            "ccxt_version": ccxt.__version__,
            "filter": "swap and linear and settle == USDT and quote == USDT and expiry is None",
            "excluded_counts": excluded,
            "markets": kept,
        },
    )
    return kept


def closed_params(market_id: str, start: int, end: int) -> dict[str, Any]:
    return {"symbol": market_id, "interval": INTERVAL, "startTime": start, "endTime": end, "limit": N}


def capture_closed(s: Session, name: str, symbol: str) -> list[list[Any]]:
    market_id = s.ex.market(symbol)["id"]
    obs_before, req_before = s.time_observation()
    start = align_5m(obs_before["server_time_ms"]) - (N + 12) * STEP_MS
    end = start + N * STEP_MS - 1

    mark = s.mark()
    raw = s.ex.fapiPublicGetKlines(closed_params(market_id, start, end))
    raw_req = s.since(mark, "raw_klines")
    mark = s.mark()
    unified = s.ex.fetch_ohlcv(symbol, INTERVAL, since=start, limit=N, params={"until": end})
    uni_req = s.since(mark, "unified_klines")
    obs_after, req_after = s.time_observation()

    meta = base_meta(
        s,
        symbol,
        start_time_ms=start,
        end_time_ms=end,
        limit=N,
        method="fapiPublicGetKlines (raw, canonical source); fetch_ohlcv (unified, comparison only)",
        params={"raw": closed_params(market_id, start, end), "unified": {"since": start, "limit": N, "until": end}},
        requests=[{**req_before, "label": "time_before"}, raw_req, uni_req, {**req_after, "label": "time_after"}],
        time_observations=[obs_before, obs_after],
    )
    write_json(f"{name}_5m_closed_raw.json", raw)
    write_json(f"{name}_5m_closed_unified.json", unified)
    write_json(f"{name}_5m_closed.meta.json", meta)
    return raw


def has_small_price(raw: list[list[Any]]) -> bool:
    return any(len(str(row[4]).partition(".")[2]) >= 6 for row in raw)


def pick_smallprice(s: Session, survey: list[dict[str, Any]]) -> str:
    candidates = [
        m["symbol"]
        for m in survey
        if m["active"] and int(s.ex.market(m["symbol"])["info"].get("pricePrecision", 0)) >= 6
    ]
    for symbol in candidates[:5]:
        market_id = s.ex.market(symbol)["id"]
        probe = s.ex.fapiPublicGetKlines({"symbol": market_id, "interval": INTERVAL, "limit": 5})
        if has_small_price(probe):
            return symbol
    raise RuntimeError(f"no low-priced candidate found among {candidates[:5]}")


def capture_forming(s: Session) -> None:
    market_id = s.ex.market(BTC)["id"]
    obs_before, req_before = s.time_observation()
    mark = s.mark()
    raw = s.ex.fapiPublicGetKlines({"symbol": market_id, "interval": INTERVAL, "limit": 2})
    raw_req = s.since(mark, "raw_klines")
    obs_after, req_after = s.time_observation()
    meta = base_meta(
        s,
        BTC,
        start_time_ms=raw[0][0],
        end_time_ms=raw[-1][6],
        limit=2,
        method="fapiPublicGetKlines",
        params={"symbol": market_id, "interval": INTERVAL, "limit": 2},
        requests=[{**req_before, "label": "time_before"}, raw_req, {**req_after, "label": "time_after"}],
        time_observations=[obs_before, obs_after],
        note="start/end are the observed first openTime and last closeTime; the request had no startTime/endTime",
    )
    write_json("btcusdt_5m_forming_raw.json", raw)
    write_json("btcusdt_5m_forming.meta.json", meta)


def run_closed() -> None:
    s = Session()
    survey = capture_survey(s)
    capture_closed(s, "btcusdt", BTC)
    small = pick_smallprice(s, survey)
    print("low-priced instrument:", small)
    capture_closed(s, "smallprice", small)
    capture_forming(s)


def run_recheck() -> None:
    first = json.loads((FIXTURES / "btcusdt_5m_closed.meta.json").read_text())
    first_wall = first["time_observations"][0]["wall_before_ms"]
    elapsed_s = (time.time_ns() // 1_000_000 - first_wall) / 1000
    if elapsed_s < 3600:
        raise SystemExit(f"recheck needs >= 1 h after the first capture; only {elapsed_s:.0f} s elapsed")
    s = Session()
    start, end = first["start_time_ms"], first["end_time_ms"]
    market_id = first["market_id"]
    obs_before, req_before = s.time_observation()
    mark = s.mark()
    raw = s.ex.fapiPublicGetKlines(closed_params(market_id, start, end))
    raw_req = s.since(mark, "raw_klines")
    obs_after, req_after = s.time_observation()
    meta = base_meta(
        s,
        first["instrument"],
        start_time_ms=start,
        end_time_ms=end,
        limit=N,
        method="fapiPublicGetKlines",
        params=closed_params(market_id, start, end),
        requests=[{**req_before, "label": "time_before"}, raw_req, {**req_after, "label": "time_after"}],
        time_observations=[obs_before, obs_after],
        seconds_since_first_capture=elapsed_s,
    )
    write_json("btcusdt_5m_recheck_raw.json", raw)
    write_json("btcusdt_5m_recheck.meta.json", meta)


def run_probe() -> None:
    """Smoke test only: observes availability of just-closed candles; never stores a candle as data."""
    s = Session()
    market_id = s.ex.market(BTC)["id"]
    obs0, _ = s.time_observation()
    offset_ms = obs0["server_time_ms"] - (obs0["wall_before_ms"] + obs0["wall_after_ms"]) // 2
    boundary = align_5m(obs0["server_time_ms"]) + STEP_MS
    boundaries: list[dict[str, Any]] = []
    all_obs: list[dict[str, Any]] = []
    all_requests: list[dict[str, str]] = []
    for _ in range(PROBE_BOUNDARIES):
        polls: list[dict[str, Any]] = []
        for offset_s in PROBE_OFFSETS_S:
            target_wall_ms = boundary + int(offset_s * 1000) - offset_ms
            time.sleep(max(0.0, (target_wall_ms - time.time_ns() / 1e6) / 1000))
            obs, time_req = s.time_observation()
            mark = s.mark()
            rows = s.ex.fapiPublicGetKlines({"symbol": market_id, "interval": INTERVAL, "limit": 3})
            kline_req = s.since(mark, "raw_klines")
            match = [r for r in rows if r[0] == boundary - STEP_MS]
            polls.append(
                {
                    "offset_s": offset_s,
                    "time_observation": obs,
                    "row": match[0] if match else "absent",
                }
            )
            all_obs.append(obs)
            all_requests += [time_req, kline_req]
        boundaries.append({"boundary_ms": boundary, "polls": polls})
        boundary += STEP_MS
    meta = base_meta(
        s,
        BTC,
        start_time_ms=boundaries[0]["boundary_ms"],
        end_time_ms=boundaries[-1]["boundary_ms"],
        limit=3,
        method="fapiPublicGetKlines",
        params={"symbol": market_id, "interval": INTERVAL, "limit": 3, "offsets_s": PROBE_OFFSETS_S},
        requests=all_requests,
        time_observations=all_obs,
        note="smoke test of 3 boundaries; not a reliability confirmation",
    )
    write_json("btcusdt_5m_boundary_probe.json", {"boundaries": boundaries})
    write_json("btcusdt_5m_boundary_probe.meta.json", meta)


COMMANDS = {"closed": run_closed, "recheck": run_recheck, "probe": run_probe}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        raise SystemExit(f"usage: capture_binance.py {{{'|'.join(COMMANDS)}}}")
    COMMANDS[sys.argv[1]]()
