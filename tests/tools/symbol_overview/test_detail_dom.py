"""The DOM part of detail.js under Node with a stub DOM: what is drawn, fetched and dropped (design 5.3, plan Review Focus 5).

Not a browser: colours, layout and the real chart library are checked by nothing automatic. The harness
(``detail_dom_harness.js``) gives detail.js a minimal DOM, fake timers, a controllable ``fetch`` and a fake chart library.
"""

import json
import subprocess
from pathlib import Path
from typing import Any

from tests.tools.symbol_overview.test_detail_page import DETAIL, SYMBOL, T0, TF, detail_doc, series
from tests.tools.symbol_overview.test_oi_page import MARKER, NODE, SCRIPT, needs_node

HARNESS = Path(__file__).with_name("detail_dom_harness.js")
INLINE_HELPERS = """
const $ = (id) => document.getElementById(id);
function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
  for (const c of children) node.append(c);
  return node;
}
"""
A, B = "A/USDT:USDT", "B/USDT:USDT"
HASH_A = "#/symbol/A%2FUSDT%3AUSDT?venue=binance-usdm&timeframe=5m"
HASH_B = "#/symbol/B%2FUSDT%3AUSDT?venue=binance-usdm&timeframe=5m"
WINDOW = {"start": T0 - 8640 * TF, "end": T0 + TF}
DATA = {"symbols": [series(A), series(B)], "window": WINDOW}
TICK = "const tick = async () => { for (let i = 0; i < 30; i += 1) await Promise.resolve(); };"


def doc_for(symbol: str = A, range_name: str = "24h", **parts: Any) -> dict[str, Any]:
    return detail_doc(symbol=symbol, range=range_name, **parts)


def dom(body: str, *, data: Any = DATA, hash_: str = HASH_A, phase: str = "ready", charts: bool = True) -> Any:
    """Run detail.js against the stub DOM with ``lastData = data``, then the async ``body``; return its JSON."""
    program = (
        f"{SCRIPT.split(MARKER)[0]}\n{INLINE_HELPERS}\nlet lastData = {json.dumps(data)};\n{DETAIL}\n"
        f";(async () => {{ {TICK}\nawait tick();\n{body}\n}})()"
    )
    driver = (
        "const vm = require('vm'); const makeEnv = require(process.argv[1]);"
        "const built = makeEnv(JSON.parse(process.argv[3]));"
        "const context = vm.createContext(built.globals);"
        "Promise.resolve(vm.runInContext(process.argv[2], context)).then("
        "(v) => process.stdout.write(JSON.stringify(v === undefined ? null : v)),"
        "(e) => { console.error(e); process.exit(1); });"
    )
    options = json.dumps({"hash": hash_, "phase": phase, "charts": charts})
    done = subprocess.run(
        [str(NODE), "-e", driver, str(HARNESS), program, options], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@needs_node
def test_a_bookmarked_detail_is_drawn_when_the_script_arrives_after_the_snapshot() -> None:
    result = dom(
        """return { body: $("detail-body").hidden, overview: $("overview-view").hidden, fetches: env.fetches.map((f) => f.url),
          head: $("detail-head").textContent };"""
    )
    assert result["body"] is False and result["overview"] is True
    assert len(result["fetches"]) == 1
    assert "symbol=A%2FUSDT%3AUSDT" in result["fetches"][0] and "range=24h" in result["fetches"][0]
    assert "A/USDT:USDT" in result["head"] and "Next" in result["head"]


@needs_node
def test_a_bookmark_waits_for_the_first_snapshot_and_then_loads() -> None:
    result = dom(
        """const before = { state: $("detail-state").textContent, body: $("detail-body").hidden, fetches: env.fetches.length };
        lastData = DATA_FROM_TEST; detailOverview(lastData); await tick();
        return { before, after: { body: $("detail-body").hidden, fetches: env.fetches.length } };""".replace(
            "DATA_FROM_TEST", json.dumps(DATA)
        ),
        data=None,
    )
    assert result["before"] == {"state": "Loading the symbol…", "body": True, "fetches": 0}
    assert result["after"] == {"body": False, "fetches": 1}


@needs_node
def test_an_overview_that_failed_before_the_script_ran_is_said_not_shown_as_loading() -> None:
    result = dom(
        'return { state: $("detail-state").textContent, fetches: env.fetches.length };', data=None, phase="error"
    )
    assert "overview could not be loaded" in result["state"] and result["fetches"] == 0


@needs_node
def test_a_slow_answer_for_a_symbol_the_user_left_is_not_drawn() -> None:
    result = dom(
        f"""env.fireHash({json.dumps(HASH_B)}); await tick();
        const urls = env.fetches.map((f) => f.url);
        env.respond(0, 200, {json.dumps(doc_for(A))}); await tick();
        const afterStale = {{ doc: detailDoc === null, card: $("kpi-candle").textContent }};
        env.respond(1, 200, {json.dumps(doc_for(B))}); await tick();
        return {{ urls, afterStale, symbol: detailDoc.symbol, head: $("detail-head").textContent }};"""
    )
    assert len(result["urls"]) == 2 and "symbol=B%2FUSDT%3AUSDT" in result["urls"][1]
    assert result["afterStale"]["doc"] is True and "76" not in result["afterStale"]["card"]
    assert result["symbol"] == B and "B/USDT:USDT" in result["head"]


@needs_node
def test_a_slow_answer_for_a_range_the_user_left_is_not_drawn() -> None:
    result = dom(
        f"""$("detail-ranges").children[3].listeners.click[0](); await tick();  // 7d
        const urls = env.fetches.map((f) => f.url);
        env.respond(0, 200, {json.dumps(doc_for(A, "24h"))}); await tick();
        const stale = detailDoc === null;
        env.respond(1, 200, {json.dumps(doc_for(A, "7d"))}); await tick();
        return {{ urls, stale, range: detailDoc.range, pressed: $("detail-ranges").children.map((c) => c.attrs["aria-pressed"]) }};"""
    )
    assert "range=7d" in result["urls"][1] and result["stale"] is True and result["range"] == "7d"
    assert result["pressed"] == ["false", "false", "false", "true", "false"]


@needs_node
def test_an_answer_for_another_series_is_refused() -> None:
    result = dom(
        f"""env.respond(0, 200, {json.dumps(doc_for(B))}); await tick();
        return {{ doc: detailDoc, state: $("detail-state").textContent, card: $("kpi-candle").textContent }};"""
    )
    assert result["doc"] is None and "another series" in result["state"] and "76" not in result["card"]


@needs_node
def test_a_failed_load_says_why_and_shows_no_numbers() -> None:
    result = dom(
        """env.respond(0, 502, { api_error: { code: "api_error", detail: "not_ready" } }); await tick();
        const first = { state: $("detail-state").textContent, card: $("kpi-candle").textContent, timers: env.liveTimers(60000).length };
        return first;"""
    )
    assert result["state"] == "The detail could not be loaded: api_error (not_ready)"
    assert "76" not in result["card"] and result["timers"] == 1  # it retries in a minute


@needs_node
def test_an_overview_failure_hides_the_detail_and_recovery_shows_it_without_a_new_load() -> None:
    result = dom(
        f"""env.respond(0, 200, {json.dumps(doc_for(A))}); await tick();
        const loaded = {{ body: $("detail-body").hidden, card: $("kpi-candle").textContent }};
        lastData = null; detailOverview(null);
        const failed = {{ body: $("detail-body").hidden, state: $("detail-state").textContent }};
        lastData = {json.dumps(DATA)}; detailOverview(lastData);
        return {{ loaded, failed, recovered: {{ body: $("detail-body").hidden, fetches: env.fetches.length, card: $("kpi-candle").textContent }} }};"""
    )
    assert result["loaded"]["body"] is False and "76" in result["loaded"]["card"]
    assert result["failed"]["body"] is True and "overview could not be loaded" in result["failed"]["state"]
    assert result["recovered"]["body"] is False and result["recovered"]["fetches"] == 1


@needs_node
def test_leaving_the_detail_stops_the_poll_and_removes_the_chart() -> None:
    result = dom(
        f"""env.respond(0, 200, {json.dumps(doc_for(A))}); await tick();
        const before = {{ timers: env.liveTimers(60000).length, charts: env.charts.length, removed: env.charts[0].removed }};
        env.fireHash("");
        return {{ before, after: {{ timers: env.liveTimers(60000).length, removed: env.charts[0].removed,
          overview: $("overview-view").hidden, detail: $("detail-view").hidden }} }};"""
    )
    assert result["before"] == {"timers": 1, "charts": 1, "removed": False}
    assert result["after"] == {"timers": 0, "removed": True, "overview": False, "detail": True}


@needs_node
def test_a_poll_of_the_same_series_keeps_the_chart_and_its_zoom() -> None:
    result = dom(
        f"""env.respond(0, 200, {json.dumps(doc_for(A))}); await tick();
        env.liveTimers(60000)[0].fn(); await tick();
        env.respond(1, 200, {json.dumps(doc_for(A))}); await tick();
        const same = {{ charts: env.charts.length, removed: env.charts[0].removed, sets: env.charts[0].series[0].sets }};
        $("detail-ranges").children[3].listeners.click[0](); await tick();
        env.respond(2, 200, {json.dumps(doc_for(A, "7d"))}); await tick();
        return {{ same, other: {{ charts: env.charts.length, first_removed: env.charts[0].removed }} }};"""
    )
    assert result["same"] == {"charts": 1, "removed": False, "sets": 2}  # the data changed, the chart did not
    assert result["other"] == {"charts": 2, "first_removed": True}  # another range is another chart


@needs_node
def test_a_part_that_is_not_ok_has_no_pane_and_says_why() -> None:
    doc = doc_for(A, open_interest={"status": "error", "code": "schema", "detail": "pages"})
    result = dom(
        f"""env.respond(0, 200, {json.dumps(doc)}); await tick();
        return {{ series: env.charts[0].series.map((s) => s.definition), note: $("detail-chart-note").textContent,
          oi: $("kpi-oi").textContent }};"""
    )
    assert result["series"] == ["candlestick", "histogram"]  # candles and volume only: no open interest pane
    assert "Open interest: cannot be loaded (schema: pages)" in result["note"]
    assert "cannot be loaded (schema: pages)" in result["oi"]


@needs_node
def test_a_missing_chart_library_is_said_and_the_cards_still_work() -> None:
    result = dom(
        f"""env.respond(0, 200, {json.dumps(doc_for(A))}); await tick();
        return {{ note: $("detail-chart-note").textContent, card: $("kpi-candle").textContent }};""",
        charts=False,
    )
    assert "chart library could not be loaded" in result["note"] and "76" in result["card"]


@needs_node
def test_a_hidden_tab_reloads_at_once_when_it_comes_back_after_a_minute() -> None:
    result = dom(
        f"""env.respond(0, 200, {json.dumps(doc_for(A))}); await tick();
        env.fireVisibility("hidden");
        env.fireVisibility("visible"); await tick();
        const fresh = env.fetches.length;
        detailFetchedAt = Date.now() - 120000;
        env.fireVisibility("hidden"); env.fireVisibility("visible"); await tick();
        return {{ fresh, stale: env.fetches.length }};"""
    )
    assert result == {"fresh": 1, "stale": 2}  # a fresh document only restarts the timer; an old one is reloaded


@needs_node
def test_the_detail_view_shows_no_unknown_series_and_the_invalid_link() -> None:
    assert dom(
        'return $("detail-state").textContent;', hash_="#/symbol/NOPE?venue=binance-usdm&timeframe=5m"
    ).startswith("This series is not in the overview.")
    assert dom('return $("detail-state").textContent;', hash_="#/symbol/x").startswith(
        "This link is not a valid symbol link."
    )
    assert dom('return { overview: $("overview-view").hidden, fetches: env.fetches.length };', hash_="") == {
        "overview": False,
        "fetches": 0,
    }


@needs_node
def test_the_tooltip_says_a_slot_has_no_row_but_not_for_a_moment_between_slots() -> None:
    result = dom(
        f"""env.respond(0, 200, {json.dumps(doc_for(A))}); await tick();
        const move = env.charts[0].crosshair;
        const tip = () => $("detail-charts").children[0];
        const slot = ({T0} - 5 * {TF}) / 1000;
        move({{ point: {{ x: 10, y: 10 }}, time: slot }});
        const onGrid = tip().textContent;
        move({{ point: {{ x: 10, y: 10 }}, time: slot + 1 }});
        return {{ onGrid, between: tip().hidden }};"""
    )
    assert (
        "no candle stored for this slot" in result["onGrid"]
        and "no open interest row for this slot" in result["onGrid"]
    )
    assert result["between"] is True


def test_the_symbol_is_a_constant_of_these_tests() -> None:
    assert SYMBOL == "BTC/USDT:USDT"
