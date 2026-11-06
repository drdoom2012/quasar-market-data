"""The page of the symbol overview with open interest (OI-6b, design 8.8).

pytest does not run JavaScript, so the page is checked two ways: string and markup checks on ``page.html``, and the
page's **pure decision functions run under Node.js** (they sit above the marker ``// --- DOM below ---``). Colors,
layout and tooltips are checked by nothing automatic; the README describes the manual check.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

PAGE = Path(__file__).resolve().parents[3] / "tools" / "symbol_overview" / "page.html"
MARKER = "// --- DOM below ---"
HTML = PAGE.read_text(encoding="utf-8")
SCRIPT = HTML.split("<script>")[1].split("</script>")[0]
HORIZON = 2_505_600_000


def test_the_page_keeps_every_existing_element_id() -> None:
    for element in ("problems", "ok-group", "refresh", "api-error", "status-line", "banner"):
        assert f'id="{element}"' in HTML


def test_the_page_has_the_new_elements() -> None:
    for element in ("oi-notice", "refresh-notice"):
        assert f'id="{element}"' in HTML


def test_clear_states_clears_the_new_elements() -> None:
    body = SCRIPT.split("function clearStates()")[1].split("\n}\n")[0]

    for element in ("oi-notice", "refresh-notice", "problems", "n-ok"):
        assert f'"{element}"' in body  # a later page-level error leaves no old green open interest tile


def test_every_reason_code_of_open_interest_has_text() -> None:
    block = SCRIPT.split("oiReasons:")[1].split("},\n")[0]

    for code in ("no_data", "invalid", "stale", "conflict", "missing", "due", "unrecoverable"):
        assert f"{code}:" in block


def test_the_page_text_is_english() -> None:
    assert re.search(r"[áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ]", HTML) is None


def test_the_page_reads_the_browser_clock_only_for_the_age_line() -> None:
    lines = [line for line in SCRIPT.splitlines() if "Date.now()" in line]

    assert lines and all("lastFetchedAt" in line for line in lines)


def test_the_page_does_not_call_the_api_directly() -> None:
    assert SCRIPT.count("fetch(") == 1 and 'fetch("/overview.json"' in SCRIPT


# --- the decision functions under Node ---------------------------------------------------------------------------------

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed: the page logic is not executed")


def call(expression: str) -> Any:
    """Evaluate ``expression`` in the page's pure section (everything above the DOM marker) and return its JSON."""
    pure = SCRIPT.split(MARKER)[0]
    program = f"{pure}\n;JSON.stringify({expression})"
    driver = "const vm = require('vm'); process.stdout.write(vm.runInNewContext(process.argv[1]));"
    done = subprocess.run([str(NODE), "-e", driver, program], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def oi(state: str, *reasons: dict[str, Any]) -> dict[str, Any]:
    return {
        "state": state,
        "reasons": list(reasons),
        "oldest_time_ms": 1_790_000_000_000,
        "newest_time_ms": 1_792_000_000_000,
        "as_of_ms": 1_792_677_001_000,
        "retrieval_horizon_ms": HORIZON,
        "strip": "." * 720,
    }


def symbol(name: str, candle: str, interest: dict[str, Any] | None, *reasons: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": name,
        "state": candle,
        "reasons": list(reasons),
        "oldest_open_time_ms": 1_790_000_000_000,
        "newest_open_time_ms": 1_792_000_000_000,
        "strip": "." * 720,
        "open_interest": interest,
    }


def reason(code: str, slots: int = 3, runs: int = 1, detail: str = "") -> dict[str, Any]:
    return {"code": code, "slots": slots, "runs": runs, "detail": detail}


@needs_node
def test_problem_symbols_filter_and_order() -> None:
    symbols = [
        symbol("A", "ok", oi("ok")),
        symbol("B", "holes", oi("ok")),
        symbol("C", "ok", oi("stale")),
        symbol("D", "behind", oi("conflict")),
        symbol("F", "conflict", oi("behind")),
        symbol("E", "conflict", oi("behind")),
    ]

    listed = call(f"problemSymbols({json.dumps(symbols)}, 'enabled').map((s) => s.symbol)")

    # worse of the two first (C: stale; then conflict: D, E, F), then the candle state (E, F before D), then the name
    assert listed == ["C", "E", "F", "D", "B"]


@needs_node
def test_the_ok_group_holds_only_symbols_where_both_are_ok() -> None:
    symbols = [
        symbol("A", "ok", oi("ok")),
        symbol("B", "ok", oi("holes", reason("missing"))),
        symbol("C", "holes", oi("ok")),
        symbol("D", "ok", oi("ok", reason("unrecoverable", 4, 1))),
    ]

    names = call(f"okEntries({json.dumps(symbols)}, 'enabled').map((e) => e.symbol)")

    assert names == ["A", "D"]


@needs_node
def test_the_unrecoverable_note_is_shown_for_an_ok_symbol() -> None:
    symbols = [symbol("A", "ok", oi("ok")), symbol("D", "ok", oi("ok", reason("unrecoverable", 4, 1)))]

    entries = call(f"okEntries({json.dumps(symbols)}, 'enabled')")

    assert entries[0]["note"] == ""
    assert "4 slots" in entries[1]["note"] and "can no longer be fetched" in entries[1]["note"]
    assert "29 days" in entries[1]["note"]  # the retrieval horizon of the answer, in days


@needs_node
def test_the_reasons_are_prefixed_by_their_source() -> None:
    row = symbol("A", "holes", oi("behind", reason("due", 2)), reason("missing", 5, 2))

    lines = call(f"reasonLines({json.dumps(row)}, 'enabled')")

    assert lines[0].startswith("candles: ") and "5 slots missing" in lines[0]
    assert lines[1].startswith("open interest: ") and "behind by 2 slots" in lines[1]
    assert len(lines) == 2


@needs_node
def test_disabled_and_missing_blocks_never_look_green() -> None:
    plain = symbol("A", "ok", None)

    # not applicable: a deployment without open interest, or a tool that does not report it
    for status in ("disabled", "absent"):
        assert call(f"okEntries({json.dumps([plain])}, '{status}').length") == 1
        assert call(f"problemSymbols({json.dumps([plain])}, '{status}').length") == 0
    # enabled or unknown with no block for a symbol: nothing says it is fine
    for status in ("enabled", "unknown"):
        assert call(f"okEntries({json.dumps([plain])}, '{status}').length") == 0
        assert call(f"problemSymbols({json.dumps([plain])}, '{status}').length") == 1
    unknown = symbol("U", "ok", oi("unknown", reason("invalid", 0, 0, "schema")))
    assert call(f"okEntries({json.dumps([unknown])}, 'unknown').length") == 0
    assert call(f"problemSymbols({json.dumps([unknown])}, 'unknown').length") == 1


@needs_node
def test_the_labels_and_notices_say_what_is_and_is_not_known() -> None:
    assert call("okGroupLabel('enabled')") == "(candles and open interest)"
    assert call("okGroupLabel('unknown')") == "(candles and open interest)"
    assert call("okGroupLabel('disabled')") == "(candles)"
    assert call("okGroupLabel('absent')") == "(candles)"
    assert call("oiNotice('enabled', '')") is None
    assert call("oiNotice('disabled', '')") == "Open interest is not enabled in this deployment"
    assert call("oiNotice('absent', '')") == "Open interest is not reported by the tool"
    unknown = call("oiNotice('unknown', 'schema:open_interest')")
    assert "could not be judged" in unknown and "schema:open_interest" in unknown


@needs_node
def test_the_refresh_notices_name_each_part_and_the_counts() -> None:
    both = {"deadline_hit": True, "oi_deadline_hit": True, "candle_not_asked": 2, "oi_not_asked": 5}

    lines = call(f"refreshNotices({json.dumps(both)})")

    assert len(lines) == 2
    assert lines[0].startswith("Candles") and "2 symbols" in lines[0]
    assert lines[1].startswith("Open interest") and "5 symbols" in lines[1]
    assert (
        call("refreshNotices({deadline_hit: false, oi_deadline_hit: false, candle_not_asked: 0, oi_not_asked: 0})")
        == []
    )
    old_tool = call("refreshNotices({deadline_hit: true})")  # a tool that does not report open interest
    assert len(old_tool) == 1 and old_tool[0].startswith("Candles")


@needs_node
def test_the_whole_script_parses_under_node(tmp_path: Path) -> None:
    """Syntax only: the DOM part below the marker is never executed by pytest."""
    script = tmp_path / "page.js"
    script.write_text(SCRIPT, encoding="utf-8")

    done = subprocess.run([str(NODE), "--check", str(script)], capture_output=True, text=True, check=False)

    assert done.returncode == 0, done.stderr


@needs_node
def test_a_symbol_without_open_interest_shows_no_state_rather_than_ok() -> None:
    plain = json.dumps(symbol("A", "ok", None))
    judged = json.dumps(symbol("B", "ok", oi("holes")))

    for status in ("disabled", "absent"):
        assert call(f"oiStateOf({plain}, '{status}')") is None  # the column shows a dash, never a green "ok"
    assert call(f"oiStateOf({plain}, 'enabled')") == "unknown"  # an answer is missing: not fine
    assert call(f"oiStateOf({judged}, 'enabled')") == "holes"
