"""The dashboard page: its pure model (run under Node.js) and the markup it needs.

The pure functions sit above the marker ``// --- DOM below ---`` of ``page.html``; ``dashboardModel`` turns an overview
object into everything the page draws, so what the page shows is tested here without a browser. Layout, colors and the
look of the loading placeholders are checked by nothing automatic; the README describes the manual check.
"""

from typing import Any

from tests.tools.symbol_overview.test_funding_page import fund, js, sym
from tests.tools.symbol_overview.test_oi_page import call, needs_node, reason

BOTH = "'enabled', 'enabled'"


def mixed() -> list[dict[str, Any]]:
    return [
        sym("A", "ok", "ok", fund("ok")),
        sym("B", "holes", "ok", fund("ok")),
        sym("C", "behind", "ok", fund("ok")),
        sym("D", "ok", "stale", fund("ok")),
        sym("E", "ok", "ok", fund("quiet")),
    ]


# --- counts and shares --------------------------------------------------------------------------------------------------


@needs_node
def test_health_counts_use_the_existing_loudness_rule() -> None:
    assert call(f"healthCounts({js(mixed())}, {BOTH})") == {"total": 5, "ok": 1, "warn": 2, "bad": 2}
    assert call(f"healthCounts([], {BOTH})") == {"total": 0, "ok": 0, "warn": 0, "bad": 0}


@needs_node
def test_a_symbol_without_a_block_is_critical_not_healthy() -> None:
    no_oi = js([sym("A", "ok", None, fund("ok"))])
    no_funding = js([sym("A", "ok", "ok", None)])

    assert call(f"healthCounts({no_oi}, {BOTH})")["bad"] == 1
    assert call(f"healthCounts({no_oi}, 'disabled', 'enabled')")["ok"] == 1
    assert call(f"healthCounts({no_funding}, {BOTH})")["bad"] == 1
    assert call(f"healthCounts({no_funding}, 'enabled', 'absent')")["ok"] == 1


@needs_node
def test_the_ok_tile_is_the_symbols_that_are_not_listed_as_problems() -> None:
    rows = [*mixed(), sym("F", "no_data", "ok", fund("ok"))]

    counts = call(f"healthCounts({js(rows)}, {BOTH})")
    listed = call(f"problemSymbols({js(rows)}, {BOTH}).length")

    assert counts["ok"] == len(rows) - listed  # the tiles and the headline can never disagree


@needs_node
def test_percent_text_never_rounds_a_problem_away() -> None:
    assert call("percentText(507, 520)") == "97.5 %"
    assert call("percentText(970, 1000)") == "97.0 %"
    assert call("percentText(2999, 3000)") == "99.9 %"
    assert call("percentText(1, 3000)") == "0.1 %"
    assert call("percentText(0, 520)") == "0.0 %"
    assert call("percentText(520, 520)") == "100.0 %"
    assert call("percentText(0, 0)") == "–"


# --- coverage and causes ------------------------------------------------------------------------------------------------


@needs_node
def test_part_coverage_counts_only_ok_and_says_why_a_part_does_not_apply() -> None:
    rows = [
        sym("A", "ok", "holes", fund("ok")),
        sym("B", "ok", None, fund("ok")),
        sym("C", "ok", "ok", fund("ok")),
    ]

    parts = call(f"partCoverage({js(rows)}, 'enabled', 'disabled')")

    assert parts[0] == {"part": "candles", "active": True, "ok": 3, "total": 3, "note": None}
    assert parts[1] == {"part": "openInterest", "active": True, "ok": 1, "total": 3, "note": None}
    assert parts[2]["part"] == "funding" and parts[2]["active"] is False
    assert (parts[2]["ok"], parts[2]["total"]) == (0, 0)
    assert parts[2]["note"] == "Funding rate is not enabled in this deployment"
    absent = call(f"partCoverage({js(rows)}, 'absent', 'absent')")
    assert absent[1]["note"] == "Open interest is not reported by the tool"


@needs_node
def test_cause_rows_one_row_per_part_and_state_worst_first() -> None:
    rows = [
        sym("A", "holes", "ok", fund("ok")),
        sym("B", "holes", "ok", fund("ok")),
        sym("C", "stale", "stale", fund("quiet")),
        sym("D", "ok", "ok", fund("partly_verified")),
    ]

    found = call(f"causeRows({js(rows)}, {BOTH})")

    assert [(r["part"], r["state"], r["count"], r["level"]) for r in found] == [
        ("candles", "stale", 1, "bad"),
        ("openInterest", "stale", 1, "bad"),
        ("funding", "quiet", 1, "bad"),
        ("candles", "holes", 2, "warn"),
        ("funding", "partly_verified", 1, "warn"),
    ]


@needs_node
def test_a_part_that_does_not_apply_has_no_cause_rows() -> None:
    rows = [sym("A", "ok", "stale", None)]

    found = call(f"causeRows({js(rows)}, 'disabled', 'absent')")

    assert found == []


@needs_node
def test_an_unrecognised_state_is_counted_and_gets_a_row() -> None:
    odd = sym("A", "brand_new", "ok", fund("ok"))
    no_state = sym("B", "ok", "ok", fund("ok"))
    del no_state["state"]

    counts = call(f"healthCounts({js([odd])}, {BOTH})")
    found = call(f"causeRows({js([odd, no_state])}, {BOTH})")

    assert counts["bad"] == 1
    assert found[0] == {"part": "candles", "state": "brand_new", "count": 1, "level": "bad"}
    assert found[1] == {"part": "candles", "state": "unrecognised", "count": 1, "level": "bad"}


@needs_node
def test_every_problem_symbol_is_in_some_cause_row() -> None:
    rows = [*mixed(), sym("F", "no_data", "ok", fund("ok")), sym("G", "ok", None, fund("ok"))]

    covered = call(
        f"""(() => {{
          const rows = {js(rows)};
          const causes = causeRows(rows, {BOTH});
          return rows.filter((s) => isProblem(s, {BOTH})).every((s) =>
            ["candles", "openInterest", "funding"].some((p) => {{
              const st = partState(s, p, {BOTH});
              return st !== null && st !== "ok" && causes.some((c) => c.part === p && c.state === st);
            }}));
        }})()"""
    )

    assert covered is True


# --- links and the donut ------------------------------------------------------------------------------------------------


@needs_node
def test_symbol_href_encodes_the_name_and_survives_a_bad_one() -> None:
    assert call("symbolHref('BTC/USDT', 'binance-usdm', '5m')") == "#/symbol/BTC%2FUSDT?venue=binance-usdm&timeframe=5m"
    nasty = call("""symbolHref('A&B #"x" y')""")
    assert nasty.startswith("#/symbol/A%26B%20%23%22x%22%20y")
    assert call("symbolHref('\\uD800')") == "#"  # a lone surrogate: encodeURIComponent throws, the link is a bare hash


@needs_node
def test_donut_background_is_neutral_without_symbols_and_never_green_for_a_problem() -> None:
    empty = call("donutBackground({total: 0, ok: 0, warn: 0, bad: 0})")
    assert "--track" in empty and "--ok" not in empty
    assert call("donutBackground({total: 520, ok: 507, warn: 8, bad: 5})") == (
        "conic-gradient(var(--ok) 0 97.5%, var(--warn) 97.5% 99.04%, var(--bad) 99.04% 100%)"
    )
    one = "conic-gradient(var(--ok) 0 99.5%, var(--warn) 99.5% 99.5%, var(--bad) 99.5% 100%)"
    assert call("donutBackground({total: 3000, ok: 2999, warn: 0, bad: 1})") == one
    assert call("donutBackground({total: 20000, ok: 19999, warn: 0, bad: 1})") == one


# --- the model ----------------------------------------------------------------------------------------------------------


def two_series_of_one_name() -> dict[str, Any]:
    ok = {**sym("X/USDT", "ok", None), "venue": "binance-usdm", "timeframe": "5m"}
    odd = {**sym("X/USDT", "unknown", None, None), "venue": "bybit", "timeframe": "1h"}
    odd["reasons"] = [reason("invalid", 0, 0, "schema")]
    other = {**sym("Y/USDT", "ok", None), "venue": "binance-usdm", "timeframe": "5m"}
    return {"symbols": [ok, odd, other]}


@needs_node
def test_the_model_for_a_name_in_two_series_labels_both_and_keeps_the_counts_consistent() -> None:
    data = two_series_of_one_name()
    none = js({"query": "", "part": None, "state": None})

    model = call(f"dashboardModel({js(data)}, {none})")

    assert [e["label"] for e in model["healthy"]] == ["X/USDT (binance-usdm, 5m)", "Y/USDT"]
    assert [e["label"] for e in model["attention"]] == ["X/USDT (bybit, 1h)"]
    assert model["healthy"][0]["href"] != model["attention"][0]["href"]
    assert model["attention"][0]["level"] == "bad"
    assert model["attention"][0]["aria"] == "X/USDT (bybit, 1h): cannot judge"
    assert "the API answer cannot be used" in model["attention"][0]["title"]
    assert model["counts"] == {"total": 3, "ok": 2, "warn": 0, "bad": 1}
    only_y = call(f"dashboardModel({js(data)}, {js({'query': 'y', 'part': None, 'state': None})})")
    assert [e["label"] for e in only_y["healthy"]] == ["Y/USDT"]
    only_x = call(f"dashboardModel({js(data)}, {js({'query': 'x/', 'part': None, 'state': None})})")
    assert [e["label"] for e in only_x["healthy"]] == [
        "X/USDT (binance-usdm, 5m)"
    ]  # the label does not depend on the filter


@needs_node
def test_the_model_filter_only_chooses_what_is_listed() -> None:
    rows = [sym("Z", "ok", None), sym("A", "ok", None), sym("M", "ok", None), sym("B", "holes", None)]
    data = js({"symbols": rows})
    none = js({"query": "", "part": None, "state": None})
    hides = js({"query": "zzz", "part": None, "state": None})

    plain = call(f"dashboardModel({data}, {none})")
    hidden = call(f"dashboardModel({data}, {hides})")

    assert [e["label"] for e in plain["healthy"]] == ["A", "M", "Z"]  # sorted by name
    assert plain["attentionLabel"] == "1" and plain["healthyLabel"] == "3"
    assert hidden["attention"] == [] and hidden["healthy"] == []
    assert hidden["attentionLabel"] == "0 of 1" and hidden["healthyLabel"] == "0 of 3"
    for key in ("counts", "shares", "donut", "coverage", "causes"):
        assert hidden[key] == plain[key]  # the filter chooses what is listed, never what is counted


@needs_node
def test_the_model_of_no_symbols_shows_zeros_and_no_share() -> None:
    none = js({"query": "", "part": None, "state": None})

    model = call(f"dashboardModel({js({'symbols': []})}, {none})")

    assert model["empty"] is True
    assert model["counts"] == {"total": 0, "ok": 0, "warn": 0, "bad": 0}
    assert model["shares"] == {"ok": "–", "warn": "–", "bad": "–"}
    assert "--track" in model["donut"]


@needs_node
def test_the_model_notes_name_what_is_not_enabled_and_what_the_refresh_missed() -> None:
    none = js({"query": "", "part": None, "state": None})
    data = {
        "symbols": [sym("A", "ok", None)],
        "open_interest": {"status": "disabled", "detail": "", "summary": None},
        "refresh": {"deadline_hit": True, "candle_not_asked": 2},
    }

    notes = call(f"dashboardModel({js(data)}, {none})")["notes"]

    texts = {n["slot"]: n["text"] for n in notes}
    assert texts["oi-notice"] == "Open interest is not enabled in this deployment"
    assert texts["funding-notice"] == "Funding rate is not reported by the tool"
    assert "2 symbols" in texts["refresh-notice"]
