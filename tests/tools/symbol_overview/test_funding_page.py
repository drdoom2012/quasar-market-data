"""The page with funding (FR-6 design 8): markup checks and the pure decision functions run under Node.js.

Colors, layout and tooltips are checked by nothing automatic; the README describes the manual check.
"""

import json
import re
from typing import Any

from tests.tools.symbol_overview.test_oi_page import HTML, SCRIPT, call, needs_node, oi, reason, symbol
from tools.symbol_overview.model import FUNDING_STATE_ORDER

FUNDING_INFO = {
    "status": "enabled",
    "window": {"start": 1_790_000_000_000, "end": 1_792_670_000_000, "clamped": False},
    "rule": {"behind_after_ms": 7_230_000, "quiet_after_ms": 172_800_000, "backfill_days": 30},
}


def fund(state: str, *reasons: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "state": state,
        "reasons": list(reasons),
        "oldest_funding_time_ms": 1_780_000_000_000,
        "newest_funding_time_ms": 1_792_000_000_000,
        "verified_from_ms": 1_780_000_000_000,
        "verified_through_ms": 1_792_600_000_000,
        "verified_at_ms": 1_792_677_000_000,
        "window_state": "verified",
        "events_in_window": 90,
        "conflicted_events": 0,
        "complete": True,
        "as_of_ms": 1_792_677_001_000,
        **extra,
    }


def sym(
    name: str, candle: str = "ok", interest: str | None = "ok", funding: dict[str, Any] | None = None
) -> dict[str, Any]:
    row = symbol(name, candle, None if interest is None else oi(interest))
    row["funding_rate"] = funding
    return row


def js(value: Any) -> str:
    return json.dumps(value)


# --- markup -------------------------------------------------------------------------------------------------------------


def test_the_page_has_the_funding_elements_and_keeps_the_old_ones() -> None:
    for element in (
        "funding-summary",
        "funding-health",
        "funding-notice",
        "summary",
        "oi-summary",
        "problems",
        "heatmap",
    ):
        assert f'id="{element}"' in HTML


def test_clear_states_clears_the_funding_elements() -> None:
    body = SCRIPT.split("function clearStates()")[1].split("\n}\n")[0]

    for element in ("funding-summary", "funding-health", "funding-notice"):
        assert f'"{element}"' in body  # a later page-level error leaves no old funding tile


def test_every_funding_reason_and_state_has_text_and_a_color_class() -> None:
    block = SCRIPT.split("fundingReasons:")[1].split("  },\n")[0]
    for code in ("no_data", "invalid", "conflict", "quiet", "behind", "unverified", "partly_verified"):
        assert f"{code}:" in block
    states = SCRIPT.split("states:")[1].split("},")[0]
    for state in FUNDING_STATE_ORDER:
        assert f"{state}:" in states
        assert re.search(rf"\.s-{state}\b", HTML)


def test_the_funding_states_of_the_page_are_those_of_the_tool() -> None:
    order = SCRIPT.split("const FUNDING_ORDER = ")[1].split(";")[0]

    assert json.loads(order) == list(FUNDING_STATE_ORDER)


def test_the_page_text_is_still_english_and_calls_only_the_tool() -> None:
    assert re.search(r"[áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ]", HTML) is None
    assert SCRIPT.count("fetch(") == 1 and 'fetch("/overview.json"' in SCRIPT


def test_funding_texts_never_claim_what_the_api_does_not_say() -> None:
    texts = SCRIPT.split("fundingReasons:")[1].split("  oiMissing")[0]

    assert "does not say settlements are missing" in texts
    assert "no settlement stored" in texts and "does not say the symbol stopped" in texts
    assert "cause (stopped task, pause, outage) is not visible" in texts


# --- ranks and lists -------------------------------------------------------------------------------------------------------


@needs_node
def test_the_combined_rank_puts_the_worst_state_of_any_part_first() -> None:
    symbols = [
        sym("A", "ok", "ok", fund("ok")),
        sym("B", "ok", "ok", fund("partly_verified", reason("partly_verified"))),
        sym("C", "ok", "ok", fund("quiet", reason("quiet"))),
        sym("D", "holes", "ok", fund("ok")),
        sym("E", "ok", "ok", fund("conflict", reason("conflict"))),
        sym("F", "stale", "ok", fund("ok")),
    ]

    listed = call(f"problemSymbols({js(symbols)}, 'enabled', 'enabled').map((s) => s.symbol)")

    assert listed == ["F", "E", "C", "D", "B"]  # stale, conflict, quiet, holes, partly_verified; A is not listed


@needs_node
def test_a_funding_problem_alone_lists_a_symbol_and_funding_ok_does_not() -> None:
    bad = sym("A", "ok", "ok", fund("behind", reason("behind")))
    good = sym("B", "ok", "ok", fund("ok"))

    assert call(f"problemSymbols({js([bad, good])}, 'enabled', 'enabled').map((s) => s.symbol)") == ["A"]
    assert call(f"okEntries({js([bad, good])}, 'enabled', 'enabled').map((e) => e.symbol)") == ["B"]


@needs_node
def test_funding_is_ignored_when_it_is_disabled_absent_or_the_parameter_is_left_out() -> None:
    bad = sym("A", "ok", "ok", fund("behind", reason("behind")))

    for call_args in ("'enabled'", "'enabled', 'disabled'", "'enabled', 'absent'"):
        assert call(f"problemSymbols({js([bad])}, {call_args}).length") == 0
        assert call(f"okEntries({js([bad])}, {call_args}).length") == 1


@needs_node
def test_a_missing_funding_block_is_never_green_when_funding_applies() -> None:
    plain = sym("A", "ok", "ok", None)

    for fstatus in ("enabled", "unknown"):
        assert call(f"problemSymbols({js([plain])}, 'enabled', '{fstatus}').length") == 1
        assert call(f"okEntries({js([plain])}, 'enabled', '{fstatus}').length") == 0
        assert call(f"fundingStateOf({js(plain)}, '{fstatus}')") == "unknown"
    for fstatus in ("disabled", "absent"):
        assert call(f"fundingStateOf({js(plain)}, '{fstatus}')") is None


@needs_node
def test_an_unknown_funding_part_lists_every_symbol() -> None:
    unknown = sym("U", "ok", "ok", fund("unknown", reason("invalid", 0, 0, "schema")))

    assert call(f"problemSymbols({js([unknown])}, 'enabled', 'unknown').length") == 1
    assert call(f"okEntries({js([unknown])}, 'enabled', 'unknown').length") == 0


@needs_node
def test_the_funding_status_of_a_page_without_the_block_is_absent() -> None:
    assert call("fundingStatus({})") == "absent"
    assert call("fundingStatus({funding_rate: {status: 'enabled'}})") == "enabled"
    assert call("fundingStatus({funding_rate: {status: 'disabled'}})") == "disabled"


# --- text ------------------------------------------------------------------------------------------------------------------


@needs_node
def test_every_reason_is_prefixed_funding_and_quotes_the_apis_rule() -> None:
    row = sym(
        "A",
        "ok",
        "ok",
        fund(
            "conflict",
            reason("conflict", 2, 0),
            reason("quiet", 0, 0),
            reason("behind", 0, 0),
            reason("unverified", 0, 0),
            reason("partly_verified", 0, 0),
        ),
    )

    lines = call(f"reasonLines({js(row)}, 'enabled', 'enabled', {js(FUNDING_INFO)})")

    assert len(lines) == 5 and all(line.startswith("funding: ") for line in lines)
    assert "2 settlements in conflict" in lines[0] and "may be the wrong one" in lines[0]
    assert "more than 48 h ago" in lines[1] and "does not say the symbol stopped" in lines[1]
    assert "more than 2 h before the API clock" in lines[2] and "not visible to the API" in lines[2]
    assert "does not say settlements are missing" in lines[3] and "not that none occurred" in lines[3]
    assert "the window starts" in lines[4] and "not verified yet" in lines[4]


@needs_node
def test_quiet_without_a_settlement_says_so_instead_of_a_date() -> None:
    row = sym("A", "ok", None, fund("quiet", reason("quiet"), newest_funding_time_ms=None))

    lines = call(f"reasonLines({js(row)}, 'disabled', 'enabled', {js(FUNDING_INFO)})")

    assert len(lines) == 1 and "no settlement stored" in lines[0] and "wrong symbol" in lines[0]
    assert "1970" not in lines[0]


@needs_node
def test_a_missing_funding_block_has_a_reason_line() -> None:
    lines = call(f"reasonLines({js(sym('A', 'ok', None, None))}, 'disabled', 'enabled')")

    assert lines == ["funding: no funding answer for this symbol"]


@needs_node
def test_reason_lines_without_the_funding_parameters_are_those_of_before() -> None:
    row = symbol("A", "holes", oi("holes", reason("missing")), reason("missing", 5, 2))
    row["funding_rate"] = fund("behind", reason("behind"))

    lines = call(f"reasonLines({js(row)}, 'enabled')")

    assert [line.split(":")[0] for line in lines] == ["candles", "open interest"]
    assert lines == call(f"reasonLines({js(row)}, 'enabled', 'absent')")


@needs_node
def test_the_group_label_names_the_parts_it_covers() -> None:
    expected = {
        ("disabled", "absent"): "(candles)",
        ("enabled", "absent"): "(candles and open interest)",
        ("enabled", "disabled"): "(candles and open interest)",
        ("unknown", "absent"): "(candles and open interest)",
        ("disabled", "enabled"): "(candles and funding)",
        ("absent", "unknown"): "(candles and funding)",
        ("enabled", "enabled"): "(candles, open interest and funding)",
    }
    for (status, fstatus), label in expected.items():
        assert call(f"okGroupLabel('{status}', '{fstatus}')") == label
    assert call("okGroupLabel('enabled')") == "(candles and open interest)"


@needs_node
def test_the_funding_notice_says_what_is_and_is_not_known() -> None:
    assert call("fundingNotice('enabled', '', {window: {clamped: false}, rule: {backfill_days: 7}})") is None
    assert call("fundingNotice('enabled', '', null)") is None
    clamped = call("fundingNotice('enabled', '', {window: {clamped: true}, rule: {backfill_days: 7}})")
    assert "last 7 days only" in clamped and "configured to verify" in clamped
    assert call("fundingNotice('disabled', '', null)") == "Funding rate is not enabled in this deployment"
    assert call("fundingNotice('absent', '', null)") == "Funding rate is not reported by the tool"
    unknown = call("fundingNotice('unknown', 'schema:funding_rate', null)")
    assert "could not be judged" in unknown and "schema:funding_rate" in unknown and "listed as a problem" in unknown


@needs_node
def test_the_refresh_notices_name_funding_and_the_count() -> None:
    lines = call("refreshNotices({deadline_hit: false, funding_deadline_hit: true, funding_not_asked: 7})")

    assert len(lines) == 1 and lines[0].startswith("Funding rate") and "7 symbols" in lines[0]
    assert call("refreshNotices({funding_deadline_hit: true, funding_not_asked: 1})")[0].endswith(
        "1 symbol was not asked"
    )
    both = call(
        "refreshNotices({deadline_hit: true, oi_deadline_hit: true, funding_deadline_hit: true,"
        " candle_not_asked: 1, oi_not_asked: 2, funding_not_asked: 3})"
    )
    assert [line.split(":")[0] for line in both] == ["Candles", "Open interest", "Funding rate"]
    assert call("refreshNotices({deadline_hit: false, funding_deadline_hit: false, funding_not_asked: 0})") == []


@needs_node
def test_the_line_under_the_strips_says_what_was_verified_and_never_calls_anything_fine() -> None:
    line = call(f"fundingLine({js(fund('ok'))}, {js(FUNDING_INFO)})")

    assert line.startswith("funding: verified ") and "newest settlement" in line
    assert "90 settlements from " in line and "complete as far as the Collector verified" in line
    unverified = call(
        f"fundingLine({js(fund('unverified', verified_from_ms=None, verified_through_ms=None, complete=False))}, null)"
    )
    assert "verified nothing" in unverified and "not complete" in unverified
    assert call("fundingLine(null, null)") == "funding: cannot judge"
    assert call(f"fundingLine({js(fund('unknown'))}, null)") == "funding: cannot judge"
    assert call(f"fundingLine({js(fund('no_data'))}, null)") == "funding: nothing stored"


@needs_node
def test_the_funding_tiles_keep_their_own_key_list_and_the_others_keep_theirs() -> None:
    assert call("FUNDING_ORDER") == list(FUNDING_STATE_ORDER)
    assert call("ORDER") == ["no_data", "unknown", "stale", "conflict", "holes", "behind", "ok"]
    ranks = call("RANK_ORDER")
    assert sorted(ranks) == sorted(set(call("ORDER")) | set(FUNDING_STATE_ORDER))
    # the relative order of every existing state is the one of before
    old = call("ORDER")
    assert [s for s in ranks if s in old] == old
    assert [s for s in ranks if s in FUNDING_STATE_ORDER] == list(FUNDING_STATE_ORDER)
