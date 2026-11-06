"""The dashboard page: its pure model (run under Node.js) and the markup it needs.

The pure functions sit above the marker ``// --- DOM below ---`` of ``page.html``; ``dashboardModel`` turns an overview
object into everything the page draws, so what the page shows is tested here without a browser. Layout, colors and the
look of the loading placeholders are checked by nothing automatic; the README describes the manual check.
"""

import re
from typing import Any

from tests.tools.symbol_overview.test_funding_page import fund, js, sym
from tests.tools.symbol_overview.test_oi_page import HTML, SCRIPT, call, needs_node, reason

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
def test_a_state_the_page_does_not_know_is_unrecognised_everywhere() -> None:
    odd = sym("A", "brand_new", "ok", fund("ok"))
    no_state = {**sym("B", "ok", "ok", fund("ok")), "state": None}
    prototype = sym("C", "constructor", "ok", fund("ok"))
    data = js(
        {
            "symbols": [odd, no_state, prototype],
            "open_interest": {"status": "enabled"},
            "funding_rate": {"status": "enabled"},
        }
    )
    none = js({"query": "", "part": None, "state": None})
    row = js({"query": "", "part": "candles", "state": "unrecognised"})

    model = call(f"dashboardModel({data}, {none})")
    clicked = call(f"dashboardModel({data}, {row})")

    assert model["counts"] == {"total": 3, "ok": 0, "warn": 0, "bad": 3}
    assert model["causes"] == [{"part": "candles", "state": "unrecognised", "count": 3, "level": "bad"}]
    assert len(model["attention"]) == 3 and model["healthy"] == []  # tiles, lists and causes agree
    assert model["worst"] == "unrecognised"
    assert call("headline(3, 3, 'unrecognised', 0)")["sub"] == "worst: unrecognised"
    assert len(clicked["attention"]) == 3  # clicking the cause row lists exactly those symbols
    assert call("levelOf('constructor')") == "bad"  # a prototype key is not a state


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
    mostly_warn = call("donutBackground({total: 1000, ok: 1, warn: 998, bad: 1})")
    assert re.search(r" -\d", mostly_warn) is None  # no negative stop


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
    assert model["worst"] == "unknown" and model["incomplete"] == 0  # what the banner needs, decided here
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
    assert call(f"dashboardModel({js(data)}, {none})")["incomplete"] == 1


# --- the markup ---------------------------------------------------------------------------------------------------------

IDS = [
    "n-total",
    "n-ok",
    "n-warn",
    "n-bad",
    "p-ok",
    "p-warn",
    "p-bad",
    "donut",
    "donut-total",
    "donut-legend",
    "parts",
    "causes",
    "problems",
    "ok-group",
    "problems-count",
    "ok-count",
    "ok-label",
    "notes",
    "banner",
    "oi-notice",
    "funding-notice",
    "funding-health",
    "refresh-notice",
]


def slot(element: str) -> str:
    """The markup from ``id="<element>"`` up to the next id: the placeholders inside that slot."""
    return HTML.split(f'id="{element}"')[1].split('id="')[0]


def main_style() -> str:
    """The page's stylesheet (the longest ``<style>``: the one in ``<noscript>`` is a single rule)."""
    sheets: list[str] = re.findall(r"<style>(.*?)</style>", HTML, re.DOTALL)
    return max(sheets, key=len)


def body_of(function: str) -> str:
    return SCRIPT.split(f"function {function}(")[1].split("\n}\n")[0]


def test_the_page_starts_in_the_loading_phase_with_every_slot_present() -> None:
    assert 'data-phase="loading"' in HTML
    for element in IDS:
        assert f'id="{element}"' in HTML
    for element in ("banner", "parts", "causes", "problems", "ok-group"):
        assert re.search(r'class="skel[ "]', slot(element))  # a placeholder of the final size, so loading moves nothing


def test_the_banner_and_the_status_line_reserve_their_space() -> None:
    style = main_style()

    for selector in ("#banner", "#status-line"):
        assert re.search(rf"{re.escape(selector)}[^{{}}]*\{{[^}}]*min-height", style)
    assert "<noscript>" in HTML


def test_clear_states_empties_every_data_slot_forgets_the_data_and_sets_the_error_phase() -> None:
    body = body_of("clearStates")

    for element in [
        "n-total",
        "n-ok",
        "n-warn",
        "n-bad",
        "p-ok",
        "p-warn",
        "p-bad",
        "donut-total",
        "donut-legend",
        "parts",
        "causes",
        "problems",
        "ok-group",
        "banner",
        "filter-chip",
        "problems-count",
        "ok-count",
        "oi-notice",
        "funding-notice",
        "funding-health",
        "refresh-notice",
    ]:
        assert f'"{element}"' in body
    assert "lastData = null" in body  # typing, Esc or a click cannot bring old data back after an error
    assert 'setPhase("error")' in body_of("showError")


def test_render_marks_ready_only_after_a_good_answer() -> None:
    body = body_of("render")

    assert body.count('setPhase("ready")') == 1
    assert body.index("data.api_error") < body.index('setPhase("ready")')
    refresh = body_of("refresh") if "function refresh(" in SCRIPT else SCRIPT.split("async function refresh(")[1]
    assert "lastFetchedAt = null" in refresh and "AbortController" in refresh and "90000" in refresh


def test_names_are_inserted_as_text_never_as_markup() -> None:
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert forbidden not in SCRIPT
    assert 'setAttribute("href"' in SCRIPT


def test_the_page_loads_nothing_external() -> None:
    assert re.search(r'(src|href)="https?:', HTML) is None
    assert "@import" not in HTML
    assert "url(" not in HTML


def test_the_removed_strip_machinery_is_gone() -> None:
    for gone in ("drawStrip", "<canvas", "heatmap", "fundingLine", "legend()"):
        assert gone not in HTML


@needs_node
def test_every_static_text_path_of_the_markup_exists_in_text() -> None:
    paths = sorted(set(re.findall(r'data-(?:text|title)="([^"]+)"', HTML)))

    found = call(f"{js(paths)}.map((path) => typeof path.split('.').reduce((node, key) => node[key], TEXT))")

    assert paths and found == ["string"] * len(paths)  # a typo would freeze the page in its placeholders


def test_the_abort_ends_in_the_error_phase_with_its_own_text() -> None:
    body = SCRIPT.split("async function refresh(")[1].split("\n}\n")[0]

    assert "AbortError" in body and "TEXT.timeout" in body and "showError(" in body.split("catch")[1]


def test_the_cards_have_a_fixed_height_so_the_lists_do_not_move_when_the_data_arrives() -> None:
    style = main_style()

    assert re.search(r"\.cards \.card \{ height: 300px", style)
    assert re.search(r"#causes \{[^}]*max-height[^}]*overflow-y: auto", style)  # many causes scroll instead of growing


def test_without_javascript_the_placeholders_do_not_shimmer_for_ever() -> None:
    assert re.search(r"<noscript><style>[^<]*animation: none", HTML)


def test_the_age_line_is_not_a_live_region_that_speaks_every_second() -> None:
    assert 'id="status-line" role=' not in HTML
