"""THROWAWAY spike: analysis of the Binance fixtures (Task 4). Not product code.

Usage: uv run python spikes/analyze_binance.py
Prints results; ADR-0001 Evidence records the printed output with sample sizes and dates.
"""

import json
import re
import statistics
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from statistics import median as st_median
from typing import Any

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "binance"
STEP = 300_000
NAMES = ["btcusdt", "smallprice"]
FIELDS = {"open": 1, "high": 2, "low": 3, "close": 4, "volume": 5}
DIVERGENCE_GATE_MS = 1000.0  # analysis-only threshold, not a product value
PLAIN = re.compile(r"^(0|[1-9][0-9]*)(\.[0-9]+)?$")
MAX_INPUT, MAX_MANTISSA_DIGITS = 128, 96


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def exists(name: str) -> bool:
    return (FIXTURES / name).exists()


def header(title: str) -> None:
    print(f"\n=== {title} ===")


def item1_identity() -> None:
    header("1. Identity, market, completeness (raw vs unified)")
    for name in NAMES:
        raw, uni = load(f"{name}_5m_closed_raw.json"), load(f"{name}_5m_closed_unified.json")
        meta = load(f"{name}_5m_closed.meta.json")
        raw_opens, uni_opens = [r[0] for r in raw], [u[0] for u in uni]
        contiguous = all(b - a == STEP for a, b in pairwise(raw_opens))
        worst = Decimal(0)
        for r, u in zip(raw, uni, strict=True):
            for idx in range(1, 6):
                a, b = Decimal(r[idx]), Decimal(str(u[idx]))
                if a != 0:
                    worst = max(worst, abs(a - b) / abs(a))
        print(
            f"{name} ({meta['instrument']}, id={meta['market_id']}): rows raw={len(raw)} unified={len(uni)} "
            f"same_open_times={raw_opens == uni_opens} contiguous={contiguous} max_relative_diff={worst:.3E}"
        )


def item2_numeric_loss() -> None:
    header("2. Numeric loss: Decimal(raw) vs Decimal(str(unified)) (comparison only)")
    for name in NAMES:
        raw, uni = load(f"{name}_5m_closed_raw.json"), load(f"{name}_5m_closed_unified.json")
        numeric, fmt_only, same, total = 0, 0, 0, 0
        examples: list[str] = []
        for r, u in zip(raw, uni, strict=True):
            for field, idx in FIELDS.items():
                total += 1
                a, b = Decimal(r[idx]), Decimal(str(u[idx]))
                if a != b:
                    numeric += 1
                    if len(examples) < 3:
                        examples.append(f"{field}@{r[0]}: raw={r[idx]} unified={u[idx]!r}")
                elif r[idx] != str(u[idx]):
                    fmt_only += 1
                else:
                    same += 1
        print(f"{name}: values={total} identical_text={same} formatting_only={fmt_only} numeric_differences={numeric}")
        for e in examples:
            print("   ", e)


def item3_text_forms() -> None:
    header("3. Observed text forms in raw numeric strings (indices 1-5, 7)")
    strings: list[str] = []
    for name in NAMES:
        for f in (f"{name}_5m_closed_raw.json", "btcusdt_5m_forming_raw.json", "btcusdt_5m_recheck_raw.json"):
            if exists(f):
                strings += [row[i] for row in load(f) for i in (1, 2, 3, 4, 5, 7)]
    print(f"sample size: {len(strings)} strings")
    print("with exponent:", sum(1 for s in strings if "e" in s.lower()))
    print("leading '+':", sum(1 for s in strings if s.startswith("+")))
    print("leading zeros (e.g. '00'):", sum(1 for s in strings if re.match(r"^0[0-9]", s)))
    print("max length:", max(len(s) for s in strings))
    print("max digit count:", max(sum(c.isdigit() for c in s) for s in strings))
    print("max fractional digits:", max(len(s.partition(".")[2]) for s in strings))
    outside = [s for s in strings if not PLAIN.match(s) or len(s) > MAX_INPUT]
    too_many = [s for s in strings if sum(c.isdigit() for c in s) > MAX_MANTISSA_DIGITS]
    print(f"outside plain-decimal grammar or > {MAX_INPUT} chars:", len(outside), outside[:5])
    print(f"mantissa digits > {MAX_MANTISSA_DIGITS}:", len(too_many))
    print("trailing-zero padded (fraction ends in '0'):", sum(1 for s in strings if "." in s and s.endswith("0")))


def observation_stats(label: str, obs: list[dict[str, Any]]) -> None:
    rtts = [(o["monotonic_after_ns"] - o["monotonic_before_ns"]) / 1e6 for o in obs]
    offsets = [o["server_time_ms"] - (o["wall_before_ms"] + o["wall_after_ms"]) / 2 for o in obs]
    print(
        f"{label}: n={len(obs)} rtt_ms min/median/max={min(rtts):.1f}/{statistics.median(rtts):.1f}/{max(rtts):.1f} "
        f"offset_estimate_ms (wall, diagnostic) min/median/max="
        f"{min(offsets):.1f}/{statistics.median(offsets):.1f}/{max(offsets):.1f}"
    )


def item4_time() -> None:
    header("4. Time semantics")
    for name in NAMES:
        raw = load(f"{name}_5m_closed_raw.json")
        diffs = {r[6] - (r[0] + STEP) for r in raw}
        print(f"{name}: closeTime - (open_time + 300000) distinct values over {len(raw)} rows: {sorted(diffs)}")
    forming = load("btcusdt_5m_forming_raw.json")
    fmeta = load("btcusdt_5m_forming.meta.json")
    s0 = fmeta["time_observations"][0]["server_time_ms"]
    print(
        f"forming: last row closeTime={forming[-1][6]} server_time={s0} "
        f"closeTime>=server_time={forming[-1][6] >= s0}; previous row closed={forming[0][6] < s0}"
    )
    all_obs: list[tuple[str, dict[str, Any]]] = []
    for f in sorted(FIXTURES.glob("*.meta.json")):
        meta = json.loads(f.read_text())
        all_obs += [(f.name, o) for o in meta["time_observations"]]
    observation_stats("all captures", [o for _, o in all_obs])
    ordered = sorted((o for _, o in all_obs), key=lambda o: o["monotonic_after_ns"])
    regress = sum(1 for a, b in pairwise(ordered) if b["server_time_ms"] < a["server_time_ms"])
    print(f"server time regressions between successive observations (by monotonic order): {regress}")
    pairs: list[dict[str, float]] = []
    for i, a in enumerate(ordered):
        for b in ordered[i + 1 :]:
            local = (b["monotonic_after_ns"] - a["monotonic_after_ns"]) / 1e6
            if local < 60_000:
                continue
            server = b["server_time_ms"] - a["server_time_ms"]
            rtt_sum = sum((o["monotonic_after_ns"] - o["monotonic_before_ns"]) / 1e6 for o in (a, b))
            wall = (b["wall_after_ms"] + b["wall_before_ms"]) / 2 - (a["wall_after_ms"] + a["wall_before_ms"]) / 2
            mono = (
                (b["monotonic_after_ns"] + b["monotonic_before_ns"])
                - (a["monotonic_after_ns"] + a["monotonic_before_ns"])
            ) / 2e6
            pairs.append(
                {
                    "ppm": (server - local) / local * 1e6,
                    "bound": rtt_sum / local * 1e6,
                    "secs": local / 1000,
                    "div": wall - mono,
                }
            )
    if not pairs:
        print("no observation pairs >= 60 s apart yet (rate difference not computable)")
        return
    rejected = [q for q in pairs if abs(q["div"]) > DIVERGENCE_GATE_MS]
    accepted = [q for q in pairs if abs(q["div"]) <= DIVERGENCE_GATE_MS]
    print(
        f"pairs >= 60 s apart: {len(pairs)}; wall-vs-monotonic divergence > {DIVERGENCE_GATE_MS:.0f} ms: {len(rejected)}"
    )
    if rejected:
        print(
            f"   divergence of rejected pairs (ms) min/max: {min(q['div'] for q in rejected):.0f}/{max(q['div'] for q in rejected):.0f}"
        )
        print(
            "   consistent with a system suspend: the monotonic clock stopped while wall clock and server time advanced (inferred, not observed directly)"
        )
    ppm = sorted(q["ppm"] for q in accepted)
    print(
        f"accepted pairs: n={len(accepted)} divergence ms min/max={min(q['div'] for q in accepted):.1f}/{max(q['div'] for q in accepted):.1f}"
    )
    print(
        f"   rate difference ppm: min={ppm[0]:+.0f} p05={ppm[len(ppm) // 20]:+.0f} median={st_median(ppm):+.0f} p95={ppm[len(ppm) * 19 // 20]:+.0f} max={ppm[-1]:+.0f}"
    )
    long_pairs = [q for q in accepted if q["secs"] >= 1800]
    if long_pairs:
        lp = sorted(q["ppm"] for q in long_pairs)
        print(
            f"   pairs >= 30 min apart: n={len(lp)} rate difference ppm min/max={lp[0]:+.0f}/{lp[-1]:+.0f}; "
            f"measurement error bound ppm min/max={min(q['bound'] for q in long_pairs):.0f}/{max(q['bound'] for q in long_pairs):.0f}"
        )
    print(
        f"   all accepted: largest measurement error bound = {max(q['bound'] for q in accepted):.0f} ppm (shorter spans are bounded by RTT)"
    )


PROBE_FILES = ["btcusdt_5m_boundary_probe", "btcusdt_5m_boundary_probe_extended"]
VALUE_IDX = (1, 2, 3, 4, 5, 7, 8)


def item5_probe() -> None:
    header("5. Boundary probes (smoke tests)")
    settle_after: list[float] = []  # first poll offset from which the row equals the last poll, per boundary
    total = 0
    for stem in PROBE_FILES:
        if not exists(f"{stem}.json"):
            print(f"{stem}: not captured yet")
            continue
        probe, meta = load(f"{stem}.json"), load(f"{stem}.meta.json")
        offsets = [p["offset_s"] for p in probe["boundaries"][0]["polls"]]
        print(
            f"{stem}: boundaries={len(probe['boundaries'])} polls/boundary={len(offsets)} last offset={offsets[-1]} s"
        )
        for b in probe["boundaries"]:
            polls = b["polls"]
            present = [p for p in polls if p["row"] != "absent"]
            first = present[0]["offset_s"] if present else None
            key = [None if p["row"] == "absent" else tuple(p["row"][i] for i in VALUE_IDX) for p in polls]
            final = key[-1]
            changes = sum(1 for x, y in pairwise(key) if x != y)
            settled_from = next(p["offset_s"] for i, p in enumerate(polls) if all(k == final for k in key[i:]))
            settle_after.append(settled_from)
            total += 1
            print(
                f"   boundary {b['boundary_ms']}: first present at {first} s, distinct values={len(set(key))}, "
                f"changes between consecutive polls={changes}, value equals final from poll at {settled_from} s"
            )
        observation_stats(stem, meta["time_observations"])
    if settle_after:
        ordered = sorted(settle_after)
        print(f"\nsettled-from offset over {total} boundaries (sorted): {ordered}")
        for delay in (3, 5, 8, 13, 20):
            print(
                f"   boundaries not yet settled at the {delay} s poll: {sum(1 for x in settle_after if x > delay)}/{total}"
            )


def item6_turnover() -> None:
    header("6. Turnover/volume sanity (supplementary, not proof of semantics)")
    for name in NAMES:
        raw = load(f"{name}_5m_closed_raw.json")
        checked = bad = 0
        for r in raw:
            vol, turn = Decimal(r[5]), Decimal(r[7])
            if vol == 0:
                continue
            lo, hi = Decimal(r[3]), Decimal(r[2])
            tick = Decimal(1).scaleb(-len(r[3].partition(".")[2]))
            checked += 1
            ratio = turn / vol
            if not (lo - tick <= ratio <= hi + tick):
                bad += 1
                print(f"   {name} {r[0]}: turnover/volume={ratio} outside [{lo - tick}, {hi + tick}]")
        print(f"{name}: non-zero-volume rows checked={checked}, outside [low-tick, high+tick]={bad}")


def item7_recheck() -> None:
    header("7. Recheck: first vs recheck capture (btcusdt, canonical numeric values)")
    if not exists("btcusdt_5m_recheck_raw.json"):
        print("recheck fixture not captured yet")
        return
    first, again = load("btcusdt_5m_closed_raw.json"), load("btcusdt_5m_recheck_raw.json")
    meta = load("btcusdt_5m_recheck.meta.json")
    diffs = []
    for a, b in zip(first, again, strict=True):
        for i in (1, 2, 3, 4, 5, 7):
            if Decimal(a[i]) != Decimal(b[i]):
                diffs.append((a[0], i, a[i], b[i]))
    print(
        f"rows compared: {len(first)}/{len(again)}, seconds since first capture: {meta['seconds_since_first_capture']:.0f}"
    )
    print(f"numeric differences: {len(diffs)}", diffs[:5])


def item8_survey() -> None:
    header("8. Market survey (USDT-M perpetuals)")
    survey = load("usdm_perpetual_markets.json")
    markets = survey["markets"]
    symbols, ids = [m["symbol"] for m in markets], [m["id"] for m in markets]
    print(f"captured_at_wall_ms={survey['captured_at_wall_ms']} ccxt={survey['ccxt_version']}")
    print(
        f"markets={len(markets)} unique_symbols={len(set(symbols))} unique_ids={len(set(ids))} "
        f"bijection={len(set(zip(symbols, ids, strict=True))) == len(markets)}"
    )
    print("excluded_counts:", survey["excluded_counts"])
    print("inactive:", sum(1 for m in markets if not m["active"]))
    chars = sorted({c for s in symbols for c in s})
    print("symbol character set:", "".join(chars), "| max symbol length:", max(map(len, symbols)))
    print("id character set:", "".join(sorted({c for i in ids for c in i})), "| max id length:", max(map(len, ids)))
    shape = re.compile(r"^[^/]+/USDT:USDT$")
    off = [s for s in symbols if not shape.match(s)]
    print("symbols not matching BASE/USDT:USDT:", len(off), off[:10])
    non_ascii = [s for s in symbols + ids if not s.isascii() or not s.isprintable()]
    print("non-ASCII or non-printable:", non_ascii[:10])
    odd = [(m["symbol"], m["id"]) for m in markets if m["id"] != m["symbol"].split("/")[0] + "USDT"]
    print(f"id != BASE+'USDT' for {len(odd)} markets:", odd[:10])


if __name__ == "__main__":
    for step in (
        item1_identity,
        item2_numeric_loss,
        item3_text_forms,
        item4_time,
        item5_probe,
        item6_turnover,
        item7_recheck,
        item8_survey,
    ):
        step()
