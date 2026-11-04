"""The page's headline and display filter (pure functions run under Node.js) and the markup they need.

Layout, colors and the pulse of the age dot are checked by nothing automatic; the README describes the manual check.
"""

import json

from tests.tools.symbol_overview.test_funding_page import fund, js, sym
from tests.tools.symbol_overview.test_oi_page import HTML, SCRIPT, call, needs_node


def test_the_page_has_the_headline_and_the_filter_elements() -> None:
    for element in ("banner", "search", "filter-chip", "problems-count"):
        assert f'id="{element}"' in HTML


def test_clear_states_clears_the_headline_and_the_filter_chip() -> None:
    body = SCRIPT.split("function clearStates()")[1].split("\n}\n")[0]

    for element in ("banner", "filter-chip", "problems-count"):
        assert f'"{element}"' in body  # a later page-level error leaves no old green headline


@needs_node
def test_the_headline_is_green_only_for_symbols_that_are_all_ok_after_a_finished_refresh() -> None:
    assert call("headline(5, 0, 'ok', 0)")["level"] == "ok"
    assert call("headline(0, 0, 'ok', 0)")["level"] == "none"  # no symbols: nothing says it is fine
    unfinished = call("headline(5, 0, 'ok', 1)")
    assert unfinished["level"] == "warn" and "did not finish" in unfinished["text"]


@needs_node
def test_the_headline_takes_the_loudness_of_the_worst_state_and_names_it() -> None:
    mild = call("headline(10, 2, 'partly_verified', 0)")
    assert mild["level"] == "warn" and mild["text"] == "2 of 10 symbols need attention"
    assert mild["sub"] == "worst: partly verified"
    assert call("headline(10, 1, 'conflict', 0)")["level"] == "bad"
    assert call("headline(10, 1, 'no_data', 0)")["level"] == "bad"
    assert call("headline(1, 1, 'unknown', 0)")["text"] == "1 of 1 symbol needs attention"


@needs_node
def test_every_state_has_a_loudness_and_only_ok_is_ok() -> None:
    states = call("RANK_ORDER")

    levels = {state: call(f"levelOf('{state}')") for state in states}

    assert [s for s, level in levels.items() if level == "ok"] == ["ok"]
    assert set(levels.values()) <= {"ok", "warn", "bad"}


@needs_node
def test_the_worst_state_is_the_worst_of_all_parts_that_apply() -> None:
    row = sym("A", "behind", "ok", fund("conflict"))

    assert call(f"worstState({js(row)}, 'enabled', 'enabled')") == "conflict"
    # a part that does not apply is not counted
    assert call(f"worstState({js(row)}, 'enabled', 'disabled')") == "behind"


@needs_node
def test_the_query_matches_any_term_in_any_case() -> None:
    symbols = [sym("BTC/USDT"), sym("ETH/USDT"), sym("SOL/USDT")]

    def names(query: str) -> list[str]:
        f = js({"query": query, "part": None, "state": None})
        found: list[str] = call(
            f"{js(symbols)}.filter((s) => matchesFilter(s, {f}, 'enabled', 'enabled')).map((s) => s.symbol)"
        )
        return found

    assert names("") == ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
    assert names("btc") == ["BTC/USDT"]
    assert names("eth, sol") == ["ETH/USDT", "SOL/USDT"]
    assert names("  ") == ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
    assert names("doge") == []


@needs_node
def test_a_tile_filter_compares_the_state_of_exactly_that_part() -> None:
    row = sym("A", "ok", "ok", fund("quiet"))
    plain = json.loads(js(row))

    def hit(part: str, state: str, fstatus: str = "enabled") -> bool:
        f = js({"query": "", "part": part, "state": state})
        matched: bool = call(f"matchesFilter({js(plain)}, {f}, 'enabled', '{fstatus}')")
        return matched

    assert hit("funding", "quiet")
    assert not hit("candles", "quiet")
    assert hit("candles", "ok")
    assert not hit("funding", "quiet", "disabled")  # funding does not apply: it has no state to match
