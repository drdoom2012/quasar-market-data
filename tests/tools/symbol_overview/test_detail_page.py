"""The symbol detail view: its pure functions run under Node.js, and the markup checks (design 5.3, 5.6).

pytest does not run JavaScript, so ``detail.js`` is checked like the dashboard: the part above ``// --- DOM below ---``
is pure and runs in Node together with the dashboard's own pure part (``TEXT``, ``fmt``, ``worstState`` ... are shared).
Colors, layout and the charts themselves are checked by nothing automatic; the README describes the manual check.
"""

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from tests.tools.symbol_overview.test_funding_page import fund, js, sym
from tests.tools.symbol_overview.test_oi_page import MARKER, NODE, SCRIPT, needs_node, reason

DETAIL = (Path(__file__).resolve().parents[3] / "tools" / "symbol_overview" / "detail.js").read_text(encoding="utf-8")
TF = 300_000
T0 = 1_792_677_000_000
SYMBOL = "BTC/USDT:USDT"


def program(expression: str) -> str:
    return f"{SCRIPT.split(MARKER)[0]}\n{DETAIL.split(MARKER)[0]}\n;{expression}"


def call(expression: str) -> Any:
    """Evaluate ``expression`` with the dashboard's and the detail's pure sections loaded, and return its JSON."""
    driver = "const vm = require('vm'); process.stdout.write(vm.runInNewContext(process.argv[1]));"
    done = subprocess.run(
        [str(NODE), "-e", driver, program(f"JSON.stringify({expression})")], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def call_async(expression: str) -> Any:
    """Like ``call`` for an expression that is a promise (the loader)."""
    driver = (
        "const vm = require('vm');"
        "Promise.resolve(vm.runInNewContext(process.argv[1], { setTimeout })).then((v) => process.stdout.write(JSON.stringify(v)));"
    )
    done = subprocess.run([str(NODE), "-e", driver, program(expression)], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def series(symbol: str = SYMBOL, venue: str = "binance-usdm", timeframe: str = "5m", **fields: Any) -> dict[str, Any]:
    row = sym(symbol, "ok", "ok", fund("ok"))
    row.update({"venue": venue, "timeframe": timeframe, "as_of_ms": T0, "present_slots": 8000})
    row.update(fields)
    return row


# --- the route ----------------------------------------------------------------------------------------------------


@needs_node
def test_the_dashboards_own_links_round_trip_through_the_route() -> None:
    for symbol in (SYMBOL, "A&B#C D/USDT:USDT", "1000PEPE/USDT:USDT", "币安/USDT:USDT", "100%/X:Y"):
        href = call(f"symbolHref({js(symbol)}, 'binance-usdm', '5m')")
        assert call(f"parseRoute({js(href)})") == {
            "view": "symbol",
            "symbol": symbol,
            "venue": "binance-usdm",
            "timeframe": "5m",
        }


@needs_node
def test_a_name_that_cannot_be_encoded_leads_to_the_dashboard_never_to_a_series() -> None:
    assert call("symbolHref('\\ud800', 'v', 't')") == "#"
    assert call("parseRoute('#')") == {"view": "overview"}


@needs_node
def test_other_hashes_are_the_dashboard_or_invalid() -> None:
    assert call("parseRoute('')") == {"view": "overview"}
    assert call("parseRoute(null)") == {"view": "overview"}
    for bad in (
        "#/symbol/x",
        "#/symbol/x?venue=a",
        "#/symbol/x?timeframe=a",
        "#/symbol/?venue=a&timeframe=b",
        "#/symbol/%E0%A4%A?venue=a&timeframe=b",
        "#/symbol/x?venue=%E0%A4%A&timeframe=b",
        "#/other",
        "#/symbol/x?venue",
    ):
        assert call(f"parseRoute({js(bad)})") == {"view": "invalid"}, bad


@needs_node
def test_a_series_is_found_by_the_whole_triple_and_neighbours_follow_the_snapshot_order() -> None:
    rows = [series("A/USDT:USDT"), series("B/USDT:USDT"), series("B/USDT:USDT", venue="other"), series("C/USDT:USDT")]
    route = {"view": "symbol", "symbol": "B/USDT:USDT", "venue": "other", "timeframe": "5m"}
    assert call(f"findRow({js(rows)}, {js(route)}).venue") == "other"
    assert call(f"findRow({js(rows)}, {js(dict(route, venue='nope'))})") is None
    near = call(f"neighbours({js(rows)}, {js(route)})")
    assert (near["prev"]["symbol"], near["prev"]["venue"], near["next"]["symbol"]) == (
        "B/USDT:USDT",
        "binance-usdm",
        "C/USDT:USDT",
    )
    first = call(f"neighbours({js(rows)}, {js(dict(route, symbol='A/USDT:USDT', venue='binance-usdm'))})")
    assert first["prev"] is None and first["next"]["symbol"] == "B/USDT:USDT"
    last = call(f"neighbours({js(rows)}, {js(dict(route, symbol='C/USDT:USDT', venue='binance-usdm'))})")
    assert last["next"] is None
    assert call(f"neighbours({js(rows)}, {js(dict(route, symbol='nope'))})") == {"prev": None, "next": None}


@needs_node
def test_the_view_state_never_shows_a_detail_without_a_good_overview() -> None:
    route = {"view": "symbol", "symbol": SYMBOL, "venue": "binance-usdm", "timeframe": "5m"}
    data = {"symbols": [series()]}

    def state(r: Any, d: Any, load: Any = None) -> str:
        shown: str = call(f"detailViewState({js(r)}, {js(d)}, {js(load)}).show")
        return shown

    assert state({"view": "overview"}, data) == "overview"
    assert state({"view": "invalid"}, data) == "invalid"
    assert state(route, None) == "loading"  # a bookmark before the first snapshot
    assert state(route, None, {"overviewFailed": False}) == "loading"
    assert state(route, None, {"overviewFailed": True}) == "overview_error"  # an overview error after a render
    assert state(route, {"symbols": []}) == "notfound"
    assert state(route, data) == "detail"


@needs_node
def test_a_slow_answer_for_a_symbol_the_user_left_is_never_applied() -> None:
    result = call_async(
        """(async () => {
          const applied = [];
          const resolvers = {};
          const loader = createDetailLoader(
            (key) => new Promise((resolve) => { resolvers[key] = resolve; }),
            (key, result) => applied.push([key, result]));
          loader.load("A"); await Promise.resolve(); await Promise.resolve();
          loader.load("B"); await Promise.resolve(); await Promise.resolve();
          resolvers.B({ status: 200, body: "b" }); resolvers.A({ status: 200, body: "a" });
          await new Promise((r) => setTimeout(r, 5));
          const afterLoads = applied.slice();
          loader.load("C"); await Promise.resolve(); await Promise.resolve();
          const startedBefore = loader.started("C");
          loader.cancel();
          resolvers.C({ status: 200, body: "c" });
          await new Promise((r) => setTimeout(r, 5));
          return { afterLoads, startedBefore, startedAfter: loader.started("C"), applied: applied.length };
        })()"""
    )
    assert result["afterLoads"] == [["B", {"status": 200, "body": "b"}]]
    assert result["startedBefore"] is True and result["startedAfter"] is False and result["applied"] == 1


@needs_node
def test_a_rejected_load_is_reported_not_thrown() -> None:
    result = call_async(
        """(async () => {
          const applied = [];
          const loader = createDetailLoader(() => Promise.reject(new Error("down")), (k, r) => applied.push([k, r]));
          loader.load("A");
          await new Promise((r) => setTimeout(r, 5));
          return applied;
        })()"""
    )
    assert result[0][0] == "A" and "down" in result[0][1]["error"]


# --- numbers and times --------------------------------------------------------------------------------------------


@needs_node
def test_decimals_are_grouped_and_shifted_as_text() -> None:
    nbsp = " "
    assert call("groupDecimal('76432.5')") == f"76{nbsp}432.5"
    assert call("groupDecimal('-0.0001')") == "-0.0001"
    assert call("groupDecimal('1000')") == f"1{nbsp}000"
    assert call("groupDecimal('0.5')") == "0.5"
    assert (
        call("groupDecimal('1234567890123456789.123456789')")
        == f"1{nbsp}234{nbsp}567{nbsp}890{nbsp}123{nbsp}456{nbsp}789.123456789"
    )
    assert call("groupDecimal('abc')") == "abc"  # never guessed at
    for rate, percent in (
        ("0.0001", "0.01"),
        ("-0.00005123", "-0.005123"),
        ("0", "0"),
        ("0.5", "50"),
        ("12", "1200"),
        ("0.00000001", "0.000001"),
        ("-0.0000", "0"),
    ):
        assert call(f"shiftPercent({js(rate)})") == percent, rate


@needs_node
def test_ages_are_read_off_the_api_clock() -> None:
    assert [call(f"ageText({ms})") for ms in (22_000, 179_000, 180_000, 7_500_000, 97_200_000, 200_000_000)] == [
        "22 s",
        "179 s",
        "3 min",
        "2 h 5 min",
        "27 h 0 min",
        "2 d 7 h",
    ]
    assert call("ageText(-1)") == "–" and call("ageText(null)") == "–" and call("ageText(1.5)") == "–"
    assert call(f"lastCloseAgeMs({js(series(newest_open_time_ms=T0 - 2 * TF))})") == TF
    assert call(f"lastCloseAgeMs({js(series(newest_open_time_ms=None))})") is None


@needs_node
def test_the_quote_currency_comes_from_the_symbol() -> None:
    assert call("quoteCurrency('BTC/USDT:USDT')") == "USDT"
    assert call("quoteCurrency('nonsense')") == ""


# --- coverage and quality -----------------------------------------------------------------------------------------

WINDOW = {"start": T0 - 8640 * TF, "end": T0 + TF}


@needs_node
def test_history_coverage_is_the_share_of_stored_history_and_never_rounds_a_hole_away() -> None:
    full = series(oldest_open_time_ms=WINDOW["start"], newest_open_time_ms=T0, present_slots=8641)
    assert call(f"historyCoverage({js(full)}, {js(WINDOW)}).text") == "100.0 %"
    one_hole = dict(full, oldest_open_time_ms=T0 - 2999 * TF, present_slots=2999)
    assert call(f"historyCoverage({js(one_hole)}, {js(WINDOW)}).text") == "99.9 %"  # 2999 of 3000: never 100
    young = series(oldest_open_time_ms=T0 - 99 * TF, newest_open_time_ms=T0, present_slots=100)
    assert call(f"historyCoverage({js(young)}, {js(WINDOW)}).span") == 100  # the span shrinks to the stored history
    stopped = series(oldest_open_time_ms=T0 - 999 * TF, newest_open_time_ms=T0 - 500 * TF, present_slots=500)
    assert call(f"historyCoverage({js(stopped)}, {js(WINDOW)}).text") == "100.0 %"


@needs_node
def test_history_coverage_is_unknown_when_a_number_is_missing_or_impossible() -> None:
    base = series(oldest_open_time_ms=T0 - 99 * TF, newest_open_time_ms=T0, present_slots=100)
    for bad in (
        dict(base, present_slots=None),
        dict(base, present_slots=101),  # more present slots than slots
        dict(base, present_slots=-1),
        dict(base, oldest_open_time_ms=None),
        dict(base, newest_open_time_ms=T0 - 200 * TF, oldest_open_time_ms=T0),
    ):
        assert call(f"historyCoverage({js(bad)}, {js(WINDOW)})") is None


@needs_node
def test_open_interest_coverage_counts_period_ends() -> None:
    oi_window = {"start": WINDOW["start"] + TF, "end": WINDOW["end"] + TF}
    block = {"present_slots": 100, "oldest_time_ms": T0 - 99 * TF, "newest_time_ms": T0}
    assert call(f"openInterestCoverage({js(block)}, {js(oi_window)}).span") == 100
    assert call(f"openInterestCoverage({js(dict(block, present_slots=99))}, {js(oi_window)}).text") == "99.0 %"
    assert call(f"openInterestCoverage({js(dict(block, present_slots=101))}, {js(oi_window)})") is None
    assert call(f"openInterestCoverage(null, {js(oi_window)})") is None


def rows_of(quality: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {row["key"]: row for row in quality}


@needs_node
def test_quality_rows_split_continuity_from_delay() -> None:
    def quality(row: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return rows_of(call(f"qualityRows({js(row)}, 'enabled', 'enabled', null)"))

    holes = quality(series(state="holes", reasons=[reason("missing", 5, 1)]))
    assert holes["continuity"]["state"] == "holes" and holes["delay"]["state"] == "ok"
    assert "5 slots missing" in holes["continuity"]["lines"][0]
    stale = quality(series(state="stale", reasons=[reason("stale", 4, 1)]))
    assert stale["continuity"]["state"] == "ok" and stale["delay"]["state"] == "stale"
    both = quality(series(state="stale", reasons=[reason("stale", 4, 1), reason("conflict", 2, 1)]))
    assert both["continuity"]["state"] == "conflict" and both["delay"]["state"] == "stale"
    behind = quality(series(state="behind", reasons=[reason("due", 1, 1)]))
    assert behind["delay"]["state"] == "behind"
    ahead = quality(series(state="unknown", reasons=[reason("stored_ahead", 0, 0)]))
    assert ahead["continuity"]["state"] == "unknown" and ahead["delay"]["state"] == "unknown"
    ok = quality(series())
    assert [ok[k]["state"] for k in ("continuity", "openInterest", "funding", "delay")] == ["ok"] * 4


@needs_node
def test_quality_rows_of_a_symbol_that_cannot_be_judged_are_never_ok() -> None:
    for state in ("no_data", "unknown", "something-new"):
        quality = rows_of(call(f"qualityRows({js(series(state=state, reasons=[]))}, 'enabled', 'enabled', null)"))
        expected = state if state != "something-new" else "unrecognised"
        assert quality["continuity"]["state"] == expected and quality["delay"]["state"] == expected


@needs_node
def test_quality_rows_of_parts_that_apply_or_not() -> None:
    row = series()
    row["funding_rate"] = None  # funding applies but the tool sent no block: unknown, never ok
    quality = rows_of(call(f"qualityRows({js(row)}, 'enabled', 'enabled', null)"))
    assert quality["funding"]["state"] == "unknown" and quality["funding"]["lines"] == [
        "no funding answer for this symbol"
    ]
    off = rows_of(call(f"qualityRows({js(series())}, 'disabled', 'absent', null)"))
    assert off["openInterest"] == {
        "key": "openInterest",
        "state": None,
        "lines": ["Open interest is not enabled in this deployment"],
    }
    assert off["funding"]["state"] is None and off["funding"]["lines"] == ["Funding rate is not reported by the tool"]


# --- cards ---------------------------------------------------------------------------------------------------------


def candle_row(t: int, close: str = "76432.5", **fields: Any) -> dict[str, Any]:
    row = {
        "t": t,
        "o": "76000",
        "h": "77000.5",
        "l": "75721.6",
        "c": close,
        "v": "12.5",
        "turnover": None,
        "conflicted": False,
    }
    row.update(fields)
    return row


def detail_doc(**parts: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "symbol": SYMBOL,
        "venue": "binance-usdm",
        "timeframe": "5m",
        "range": "24h",
        "as_of_ms": T0,
        "window": {"start": T0 - 288 * TF, "end": T0 + TF},
        "candles": {
            "status": "ok",
            "code": "",
            "detail": "",
            "rows": [candle_row(T0 - TF)],
            "change": {"percent": "+0.84", "previous_close": "75797"},
            "oldest_ms": T0 - 9 * TF,
            "newest_ms": T0 - TF,
        },
        "open_interest": {
            "status": "ok",
            "code": "",
            "detail": "",
            "rows": [{"t": T0, "oi": "95677.779", "value": "29870000000.5", "conflicted": False}],
            "change": None,
            "window": {"start": 0, "end": 1},
            "oldest_ms": 1,
            "newest_ms": T0,
        },
        "funding_rate": {
            "status": "ok",
            "code": "",
            "detail": "",
            "rows": [],
            "latest": {
                "t": T0 - 5000,
                "rate_type": "Regular",
                "rate": "0.0001",
                "mark_price": "76400.1",
                "conflicted": False,
            },
            "oldest_ms": 1,
            "newest_ms": T0 - 5000,
        },
    }
    doc.update(parts)
    return doc


@needs_node
def test_the_cards_print_exact_strings() -> None:
    nbsp = " "
    cards = call(f"kpiModel({js(detail_doc())})")
    assert cards["candle"]["head"] == f"76{nbsp}432.5"
    assert cards["candle"]["lines"][0] == f"open 76{nbsp}000 · high 77{nbsp}000.5 · low 75{nbsp}721.6"
    assert cards["candle"]["lines"][2] == "change +0.84 % vs the previous candle"
    assert cards["openInterest"]["head"] == f"29{nbsp}870{nbsp}000{nbsp}000.5 USDT"
    assert cards["openInterest"]["lines"][2] == "change n/a"
    assert cards["funding"]["head"] == "0.01 %"
    assert cards["funding"]["lines"][1] == "rate 0.0001 (a fraction per settlement)"


@needs_node
def test_a_conflicted_latest_value_says_so() -> None:
    doc = detail_doc(candles=dict(detail_doc()["candles"], rows=[candle_row(T0 - TF, conflicted=True)]))
    lines = call(f"kpiModel({js(doc)})")["candle"]["lines"]
    assert lines[-1].startswith("conflicted: the stored value is the first one")


@needs_node
def test_a_stale_or_empty_range_says_so_and_names_the_newest_stored() -> None:
    empty = dict(detail_doc()["candles"], rows=[], change=None)
    line = call(f"kpiModel({js(detail_doc(candles=empty))})")["candle"]["lines"][0]
    assert line == f"no data in this range (newest stored: {call(f'fmt({T0 - TF})')})"
    no_newest = dict(empty, newest_ms=None)
    assert call(f"kpiModel({js(detail_doc(candles=no_newest))})")["candle"]["lines"] == ["no data in this range"]


@needs_node
def test_parts_that_are_not_ok_show_why_and_no_numbers() -> None:
    doc = detail_doc(
        candles={"status": "error", "code": "api_error", "detail": "not_ready"},
        open_interest={"status": "disabled", "code": "", "detail": ""},
        funding_rate={"status": "empty", "code": "", "detail": ""},
    )
    cards = call(f"kpiModel({js(doc)})")
    assert cards["candle"] == {"status": "error", "head": None, "lines": ["cannot be loaded (api_error: not_ready)"]}
    assert (
        cards["openInterest"]["lines"] == ["not enabled in this deployment"] and cards["openInterest"]["head"] is None
    )
    assert cards["funding"]["lines"] == ["nothing stored yet"] and cards["funding"]["head"] is None
    no_settlement = detail_doc(funding_rate=dict(detail_doc()["funding_rate"], latest=None))
    assert call(f"kpiModel({js(no_settlement)})")["funding"]["lines"] == ["no settlement in the last 7 days"]


@needs_node
def test_a_pane_is_drawn_only_from_an_ok_part_with_rows() -> None:
    doc = detail_doc()
    assert call(f"paneModel({js(doc['candles'])})") == {"draw": True, "text": ""}
    assert call(f"paneModel({js(doc['funding_rate'])})")["draw"] is False  # ok but no rows in the range
    assert call(f"paneModel({js({'status': 'error', 'code': 'schema', 'detail': 'pages'})})") == {
        "draw": False,
        "text": "cannot be loaded (schema: pages)",
    }
    assert call(f"paneModel({js({'status': 'disabled'})})")["text"] == "not enabled in this deployment"


@needs_node
def test_tooltips_use_the_exact_stored_strings() -> None:
    lines = call(f"tooltipLines('candles', {js(candle_row(T0, conflicted=True))})")
    assert lines[1].endswith("close 76 432.5") and lines[-1].startswith("conflicted")
    oi_lines = call(f"tooltipLines('openInterest', {js({'t': T0, 'oi': '1.5', 'value': '2.5', 'conflicted': False})})")
    assert oi_lines[0].startswith("period ended") and oi_lines[1] == "value 2.5"
    funding_lines = call(
        f"tooltipLines('funding', {js({'t': T0, 'rate_type': 'Special', 'rate': '-0.5', 'mark_price': None, 'conflicted': False})})"
    )
    assert funding_lines[1] == "Special rate -0.5" and funding_lines[2] == "mark price: none stored"


# --- chart data ----------------------------------------------------------------------------------------------------

COLORS = {"up": "U", "down": "D", "volUp": "vu", "volDown": "vd"}


@needs_node
def test_a_missing_slot_is_a_whitespace_point_never_skipped() -> None:
    start = T0 - 3 * TF
    window = {"start": start, "end": start + 3 * TF}
    rows = [candle_row(start), candle_row(start + 2 * TF)]
    bars = call(f"candleBars({js(rows)}, {js(window)}, {js(COLORS)})")
    assert [("open" in b) for b in bars] == [True, False, True]  # one entry per slot, the hole has only a time
    assert bars[1] == {"time": (start + TF) / 1000}
    volumes = call(f"volumeBars({js(rows)}, {js(window)}, {js(COLORS)})")
    assert volumes[1] == {"time": (start + TF) / 1000}
    assert volumes[0]["color"] == "vu"  # 76432.5 >= 76000: the close is not below the open


@needs_node
def test_a_conflicted_candle_is_marked() -> None:
    start = T0 - TF
    window = {"start": start, "end": start + TF}
    bar = call(f"candleBars({js([candle_row(start, conflicted=True)])}, {js(window)}, {js(COLORS)})")[0]
    assert bar["color"] == bar["borderColor"] == bar["wickColor"] == "#c2186b"
    assert (
        call(f"volumeBars({js([candle_row(start, conflicted=True)])}, {js(window)}, {js(COLORS)})")[0]["color"]
        == "#c2186b"
    )


@needs_node
def test_open_interest_is_plotted_at_the_candle_it_belongs_to() -> None:
    start = T0 - 2 * TF
    window = {"start": start, "end": start + 2 * TF}
    rows = [{"t": start + TF, "oi": "1", "value": "10.5", "conflicted": False}]  # the END of the first slot's period
    points = call(f"oiPoints({js(rows)}, {js(window)})")
    assert points == [{"time": start / 1000, "value": 10.5}, {"time": (start + TF) / 1000}]
    assert call(f"oiPlotMs({T0})") == T0 - TF


@needs_node
def test_a_regular_and_a_special_row_in_the_same_second_are_counted_not_merged() -> None:
    settle = 1_792_656_000_000
    rows = [
        {"t": settle, "rate_type": "Regular", "rate": "0.0001", "mark_price": None, "conflicted": False},
        {"t": settle + 650, "rate_type": "Special", "rate": "0.0003", "mark_price": None, "conflicted": False},
        {
            "t": settle + 8 * 3_600_000,
            "rate_type": "Regular",
            "rate": "-0.00005",
            "mark_price": None,
            "conflicted": False,
        },
    ]
    result = call(f"fundingPoints({js(rows)}, {js(COLORS)})")
    assert result["special"] == 1 and len(result["points"]) == 2
    assert result["points"][0] == {"time": settle / 1000, "value": 0.01, "color": "U"}
    assert result["points"][1]["color"] == "D"


# --- no name of detail.js may repeat one of the inline script ---------------------------------------------------------


def top_level_names(script: str) -> set[str]:
    return set(re.findall(r"^(?:async\s+)?(?:function|const|let|var|class)\s+([A-Za-z_$][\w$]*)", script, re.MULTILINE))


def test_detail_names_do_not_collide_with_the_inline_script() -> None:
    assert top_level_names(DETAIL) and top_level_names(SCRIPT)
    assert top_level_names(DETAIL) & top_level_names(SCRIPT) == set()


@needs_node
def test_both_scripts_parse_together() -> None:
    done = subprocess.run(
        [str(NODE), "--check", "-"], input=SCRIPT + "\n" + DETAIL, capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr


# --- markup and the rules the page keeps -----------------------------------------------------------------------------

PAGE_HTML = (Path(__file__).resolve().parents[3] / "tools" / "symbol_overview" / "page.html").read_text(
    encoding="utf-8"
)
DETAIL_VIEW = PAGE_HTML.split('<main id="detail-view"')[1].split("</main>")[0]
HOOK = 'if (typeof detailOverview === "function") detailOverview(lastData);'


def test_the_detail_view_has_every_element_the_script_fills() -> None:
    for element in (
        "overview-view",
        "detail-view",
        "detail-state",
        "detail-body",
        "detail-head",
        "detail-status",
        "kpi-candle",
        "kpi-oi",
        "kpi-funding",
        "detail-ranges",
        "detail-charts",
        "detail-charts-title",
        "detail-chart-note",
        "detail-coverage",
        "detail-quality",
        "detail-fetched",
        "detail-notice",
    ):
        assert f'id="{element}"' in PAGE_HTML, element
    assert '<main id="detail-view" hidden>' in PAGE_HTML and '<div id="detail-body" hidden>' in PAGE_HTML


def test_the_scripts_load_in_order_after_the_inline_script_and_the_first_script_tag_is_still_the_inline_one() -> None:
    assert PAGE_HTML.split("<script>")[1].startswith('\n"use strict";\n// All text of the page')
    tags = re.findall(r"<script[^>]*>", PAGE_HTML)
    assert tags[0] == "<script>" and tags[1] == '<script src="/vendor/lightweight-charts.js">'
    assert tags[2].startswith('<script src="/detail.js" onerror="')
    assert (
        PAGE_HTML.index("</script>")
        < PAGE_HTML.index('src="/vendor/lightweight-charts.js"')
        < PAGE_HTML.index('src="/detail.js"')
    )
    assert "could not be loaded" in tags[2]


def test_the_inline_script_tells_the_detail_about_every_overview_result_and_nothing_else_changed() -> None:
    assert SCRIPT.count(HOOK) == 2
    assert SCRIPT.count("fetch(") == 1 and 'fetch("/overview.json"' in SCRIPT
    clear = SCRIPT.split("function clearStates()")[1].split("\n}\n")[0]
    assert HOOK in clear and "lastData = null" in clear  # an overview failure clears the detail too
    render = SCRIPT.split("function render(data, httpStatus)")[1].split("\n}\n")[0]
    assert HOOK in render.rstrip().splitlines()[-1]  # after the last thing render does


def test_the_page_still_obeys_the_dashboards_rules() -> None:
    assert re.search(r'(src|href)="https?:', PAGE_HTML) is None  # the attribution link is made by detail.js
    for forbidden in ("url(", "@import", "heatmap", "<canvas", "legend()", "fundingLine", "drawStrip"):
        assert forbidden not in PAGE_HTML, forbidden
    ids = re.findall(r'\sid="([^"]+)"', PAGE_HTML)
    assert len(ids) == len(set(ids))
    assert "<header>" in PAGE_HTML and PAGE_HTML.count("<header>") == 1  # the detail header is a div


def test_the_detail_markup_has_no_data_text_and_its_hidden_attributes_are_not_overridden() -> None:
    assert "data-text" not in DETAIL_VIEW and "data-title" not in DETAIL_VIEW
    for selector in ("#detail-view", "#detail-body", "#overview-view"):
        for rule in re.findall(re.escape(selector) + r"\s*\{[^}]*\}", PAGE_HTML):
            assert "display" not in rule or "[hidden]" in rule or "display: none" in rule


def test_detail_js_keeps_the_pages_rules() -> None:
    assert DETAIL.startswith('"use strict";')
    assert DETAIL.count("fetch(") == 1 and 'fetch("/detail.json?"' in DETAIL
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "data-text"):
        assert forbidden not in DETAIL, forbidden
    assert re.search(r"[áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ]", DETAIL) is None  # the text is English
    lines = [line for line in DETAIL.splitlines() if "Date.now()" in line]
    assert lines and all("detailFetchedAt" in line for line in lines)
    assert DETAIL.rstrip().endswith("detailInit();")


def test_detail_js_has_its_attribution_notice_and_never_turns_the_logo_off() -> None:
    assert "TradingView Lightweight Charts" in DETAIL and "https://www.tradingview.com/" in DETAIL
    assert "attributionLogo: false" not in DETAIL and "attributionLogo:false" not in DETAIL


# --- the charts ------------------------------------------------------------------------------------------------------


@needs_node
def test_the_price_axis_has_the_decimals_of_the_stored_strings() -> None:
    assert call("decimalPlaces(['76432.5', '75721.25', '77000'])") == 2
    assert call("decimalPlaces(['100', '101'])") == 0
    assert call("decimalPlaces(['0.000012345678912'])") == 8  # capped
    assert call("decimalPlaces([])") == 0


@needs_node
def test_colors_with_alpha_come_from_hex_and_anything_else_is_left_alone() -> None:
    assert call("withAlpha('#22c55e', 0.45)") == "rgba(34, 197, 94, 0.45)"
    assert call("withAlpha(' #3B82F6 ', 0.02)") == "rgba(59, 130, 246, 0.02)"
    assert call("withAlpha('var(--ok)', 0.5)") == "var(--ok)"


def test_the_chart_colors_are_read_from_the_pages_variables_not_written_in_the_script() -> None:
    assert "getComputedStyle(document.documentElement)" in DETAIL
    for variable in ("--ok", "--bad", "--accent", "--line", "--muted", "--card"):
        assert f'detailVariable("{variable}")' in DETAIL
    literals = set(re.findall(r"#[0-9a-fA-F]{6}\b", DETAIL))
    assert literals == {"#c2186b"}  # the conflict colour is the only fixed one


def test_the_chart_is_made_only_when_a_new_document_arrives_and_is_removed_when_the_view_changes() -> None:
    assert "if (doc === detailDrawn && doc !== null) return;" in DETAIL
    assert DETAIL.count("detailDestroyChart();") >= 2 and "detailChart.remove()" in DETAIL
    assert "setVisibleRange" in DETAIL and "try {" in DETAIL.split("setVisibleRange")[0].split("\n")[-1]  # guarded


def test_a_missing_library_is_said_not_thrown() -> None:
    assert 'typeof LightweightCharts === "undefined"' in DETAIL and "chartsFailed" in DETAIL
