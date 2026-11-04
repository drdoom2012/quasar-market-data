# FR-6: funding rate on the symbol overview page — design

- **Date:** 2026-11-04. **Revision 1.**
- **Status:** the user delegated the whole issue to autonomous work that stops at the pull request ("bud pro toto issue autonomni zastav se az u PR", 2026-11-04); the calls U1 to U6 of section 10 are therefore mine and stand until the user overrules them in the pull request. Implemented; section 11 holds the measurements. Issue #28 (tracking #15), optional, not part of the ADR-0004 slice table (P-16).
- **Size:** a project with a written design, **one slice, one pull request**. It changes the overview tool (`tools/symbol_overview/`), its page, its tests, the overview benchmark and the README. **No API, contract, schema, Collector or configuration change** (issue, "Out of scope").
- **How to read this document:** section 3 holds **verified facts** with their source. Sections 4 to 8 are **design decisions, mine unless marked**; section 10 lists the calls the user may want to overrule. The OI-6 design (`2026-11-03-oi-6-open-interest-overview-design.md`, "OI-6") is in force wherever this document is silent: the same phases, the same deadline, the same "never green by inference", the same additive overview object. Abbreviations: **C§x** = `docs/contracts/api-v1.md` section x; **ADR-4** = `docs/adr/0004-funding-rate.md`; `T0` = `as_of_ms` of `/health/ingest`.

## 1. Intent

The page answers "is anything wrong with the data the Collector downloads?" for candles and open interest. Funding rates are served since FR-5 (`GET /v1/funding-rates`, `funding_rate` in `/health/ingest`) and the page says nothing about them. The user wants funding on the page **as far as funding allows it**: funding has no time grid, so there is no per-slot strip and no "hole" to point at; what the API can honestly say is what the Collector **verified**, whether the newest settlement is recent, whether a stored settlement is in conflict, and whether the Collector keeps up.

## 2. Goals, non-goals, anti-goals

**Goals**
- A funding problem is visible in the summary and in the problem list, with a reason in words, for every symbol.
- **Candle and open interest results never depend on funding** (OI-6 8.1, 8.9): not on its answers, not on the time its calls take.
- Whether the refresh still fits at the target universe is **measured** (section 7), not assumed.

**Non-goals**
- A funding cost, average, sum or annualised rate, any use of the value of a rate (ADR-4 NG-1). The page never shows a `funding_rate` value.
- A per-slot strip, a list of missing settlements, a guess of the settlement interval of a symbol (ADR-4 Context 1).
- A combined state of the three parts per symbol; the headline state of a symbol stays the candle state (OI-6, user's decision 2).
- Any change to the API, the contract, the Collector, the schema, a configuration key or the existing candle and open interest behavior.

**Anti-goals (a failure even if it technically works)**
- **Funding shown as fine when the truth is unknown.** An answer that is missing, invalid, late, from another series or self-contradicting is `cannot judge`, never green. A deadline that stops the refresh is never green either.
- `partly_verified` / `unverified` read as "settlements are missing" or "there were none". They say only what the Collector has verified (C§6.7.5).
- A funding failure, or the time funding takes, turning a candle or open interest state into `unknown`.
- A threshold or a clock invented by the page (section 4.3 says exactly which API values it compares).

## 3. Verified facts

Read in the contract, the ADR and the code on 2026-11-04.

| # | Fact | Source |
|---|---|---|
| V1 | `GET /v1/funding-rates?venue&symbol&start[&end][&limit]`: `start`, `end` are inclusive, not aligned; `end` explicit means no capping; `limit` is `2..1000`; the window is at most 365 days. | C§6.7.2 |
| V2 | **`coverage` speaks for the whole window `[start, end]`, never for the page**; `limit` changes `rows`, `next` and nothing in `coverage`. So `limit=2` gives the full verdict for the 30-day window and a body of well under 1 kB. `series.oldest/newest_funding_time_ms` are whole-series (min and max stored row), both `null` without a row; they are not in the window and not in the rows of the page. | C§6.7.4, 6.7.5 |
| V3 | `window_state` is `verified` iff `verified_from_ms <= start` and `end <= verified_through_ms`; `unverified` iff there is no span or the window and the span are disjoint; `partly_verified` otherwise. `complete` iff `verified` and `conflicted_events = 0`. A defaulted `end` (now) is `partly_verified` at best, so **a window that ends at or before `verified_through_ms` has to be asked for with an explicit `end`**. | C§6.7.5, 6.7.2 |
| V4 | `/health/ingest` `funding_rate` is `null` (key present) when `[funding_rate]` is not enabled, else an object with `as_of_ms` (the same value as `T0`), `rule` (`publication_delay_ms`, `behind_after_ms`, `quiet_after_ms`, `backfill_days`), `scope`, `status` (`no_series`, `degraded`, `behind`, `catching_up`, `current`), and the counts `series_count`, `current`, `behind`, `catching_up`, `conflicted_series`, `quiet_series`. **It carries no per-series list**, unlike open interest's capped `behind` list. | C§6.7.8 |
| V5 | A series is **behind** iff `as_of_ms - T >= behind_after_ms` (`T` = `verified_through_ms`, the boundary is inclusive); **quiet** iff it has no stored row or `as_of_ms - N > quiet_after_ms` (strict, `N` = newest stored row); `conflicted_series` counts only conflicts of the last 24 h. Quiet is "the only signal that tells a valid empty answer from a wrong symbol". | C§6.7.8, ADR-4 P-13 |
| V6 | Errors: `404 unknown_series` ("no funding rate span or row of this series has been stored"; a typo and a symbol not yet collected look the same; a series with only conflict records is unknown), `404 funding_rate_disabled` (without opening the database), `503 not_ready`, `500`. | C§6.7.6 |
| V7 | The new span of a series is created by a seed page at the current end, so a series is current at once; its depth grows backward as one range of at most 30 days (about 45 min for 659 series; `backfill_days` is at most 30, ADR-0004 A-4). `catching_up` ⇔ the span is shallower than `backfill_days`. | ADR-4 P-7, P-11 |
| V8 | The tool uses only the standard library, makes at most 4 calls in flight, one pool per phase, a **new connection per call**, a 60 s deadline checked **before every call**, caches the finished snapshot 15 s, allows a fixed set of paths. A `health` without the `open_interest` key makes the open interest part `unknown` for every symbol with no call. The existing test helpers' `/health/ingest` has no `open_interest` key, so existing tests run through exactly that path. | `tools/symbol_overview/refresh.py`, `fetch.py`, `validate.py`; `tests/tools/symbol_overview/helpers.py` |
| V9 | The overview's call budget is the open problem of OI-6 (its section 7.1): `D ≈ (2 + ⌈N/4⌉·k)·L`. The OI-6 benchmark `make bench-overview` reproduces the call order and evaluates AC1 to AC7. | OI-6 7; `tests/api/bench_overview_refresh.py` |

## 4. Judging one symbol's funding

### 4.1 The question asked: one call, an explicit window

Per instrument, **one** `GET /v1/funding-rates` with

- `start = max(S, T0 - backfill_days · 86 400 000)`: `S` is the start of the page's 30-day window (the same `S` as for candles); the clamp exists because the Collector never verifies deeper than `backfill_days` (the configuration minimum is 1 day), and a window that starts before that could never be `verified`, so the page would list every symbol as a problem for a reason that is configuration, not data. For `backfill_days = 30`, the maximum (ADR-0004 A-4), `start = S`. `backfill_days` comes from `health.funding_rate.rule`. If the clamp applies, the page says so in one line (U7);
- `end = T0 - behind_after_ms + 1` (inclusive; `behind_after_ms` from `health.funding_rate.rule`);
- `limit = 2` (the minimum; no row is used, V2).

Why this `end`. The page needs a verdict for the whole window, so `end` must not lie beyond `verified_through_ms` of a healthy series (V3), but a series' span end is not known before the call and health lists no series (V4). By V5 a series is **not** behind at `T0` iff `T0 - T < behind_after_ms` iff `T >= T0 - behind_after_ms + 1`. With that `end`, **`end <= verified_through_ms` holds exactly for the series that are not behind at `T0`**. The API's own `window_state` and `complete` then say "verified for the whole 30 days" for a healthy series, and the page compares only two API values with the window it asked for (4.3); it never applies `behind_after_ms` to a date itself. With the clamp, a series whose span is as deep as configured has `F <= T - backfill_days·day <= T0 - backfill_days·day`, so it is still `verified` from the start side. The cost is that the last `behind_after_ms` (about two hours) of the window is not asked about; a settlement in that gap is not judged by this call, which is exactly what "not behind" already allows.

If `end < start` or the window is out of the valid range, or `behind_after_ms` or `backfill_days` is not a non-negative integer (`backfill_days >= 1`), funding is `unknown` for every symbol with no call (`invalid:schema:funding_window`).

### 4.2 Validation of an answer (mirror of OI-6 8.5)

Any failure is `unknown` with a reason code for that symbol's funding only; a `404 unknown_series` is `no_data`; any other non-200 is reported with its `error.code` (a `404 funding_rate_disabled` while health says enabled is such a case: the two answers contradict each other).

- **Every response:** 200, JSON, UTF-8, at most 2 MB; codes `bad_status`, `api_error`, `bad_content_type`, `too_large`, `bad_json`.
- **Echo** (`echo`): `venue`, `symbol`, `start`, `end` equal the request; `time_semantics == "settlement"`.
- **Types** (`schema`): `series` with both fields `null` or both integers in the valid range with `oldest <= newest`; `rows` a list of at most `limit` items (its content is not used and not trusted); `coverage` with `as_of_ms` in range, `verified_from_ms`, `verified_through_ms`, `verified_at_ms` all `null` or all integers with `from <= through`, `window_state` in the closed set, non-negative integer `events_in_window` and `conflicted_events`, boolean `complete`.
- **Internal consistency** (`counts`), each is the contract's own definition and cannot reject an answer the API produces: `window_state` equals the value derived from the span and the window (V3); `complete == (window_state == "verified" and conflicted_events == 0)`; `events_in_window > 0` implies `series` is not `null`; `series` `null` implies `events_in_window == 0`.
- **Against the other answers:** `T0 <= coverage.as_of_ms <= T0 + REFRESH_DEADLINE_MS + CALL_TIMEOUT_MS` (`clock_inconsistent`, the existing constants). **And fail closed on a host clock that lags the exchange's** (C§6.7.2 admits that such a clock could make a stopped series look `verified`): `verified_through_ms > coverage.as_of_ms` or `newest_funding_time_ms > coverage.as_of_ms` is `clock_inconsistent` (a settlement or a span in the future of the API's own clock), as the open interest part rejects a row for a period that had not started. The inference of 4.1 ("verified means not behind") is only made on an answer that passed this check.
- Health (`schema`, no new code): the key `funding_rate` absent or not an object/`null`, `as_of_ms != T0`, a `rule` without the four non-negative integers, `scope != "stored_series"`, `status` outside the closed set, a missing or negative count. **Absent or malformed makes the whole funding part `unknown`; the candle and open interest parts are unaffected.** `null` is **disabled**: no funding call, one sentence on the page.

No new member of `INVALID_CODES`.

### 4.3 Reasons and states

Inputs are the validated answer, `S`, the asked `end`, `T0` and `quiet_after_ms` (from health).

| Reason | Headline state | When | What the text must (not) say |
|---|---|---|---|
| `no_data` | `no_data` | `404 unknown_series` | nothing stored; the API cannot tell whether something should have been |
| `invalid:<r>` | `unknown` | validation or call failed, or the deadline | the reason |
| `conflict` | `conflict` | `conflicted_events > 0` | "N settlements in conflict in the window: the first stored value may be the wrong one" |
| `quiet` | `quiet` | no stored row (`series` `null`), or `T0 - newest_funding_time_ms > quiet_after_ms` (strict) | two texts: with a newest settlement, its date; without one, "no settlement stored" (normal for a listing that has not had its first settlement, or a wrong symbol). Neither says the symbol stopped (a real pause of two days is reported until the next event, C§6.7.8) |
| `behind` | `behind` | the span exists and `verified_through_ms < end` | the verified span ends at date X, more than `behind_after_ms` (shown in hours, from the API's rule) before the API clock; the cause (stopped task, pause, outage) is not visible to the API |
| `unverified` | `unverified` | `window_state == "unverified"` | the Collector has not verified this window; **not** "settlements are missing" and **not** "none occurred" |
| `partly_verified` | `partly_verified` | `window_state == "partly_verified"` **and** `verified_from_ms > start` | verified from X, the window starts at Y: the older part is not verified yet (the series is catching up, V7); not "missing". It is shown in the clamped case only when the span is shallower than `backfill_days` |

- `partly_verified` is a reason **only** for a span that starts after the window; a span that ends before the window end is `behind` (4.1: the same fact), so the same fact is never listed twice. A span that both starts after the window start and ends before the window end gives **both** reasons: they are two different facts.
- **A symbol is `ok` only when** the answer validated, `complete` is `true` and the symbol is not quiet. Every other path ends in a state above or in `cannot judge`. `ok` does not say the window has no missing settlement the Collector could not see, or that a settlement between `verified_through_ms` and now exists; the README says so (C§6.7.5, "What `complete` says").
- Headline order, worst first: **`no_data`, `unknown`, `conflict`, `quiet`, `behind`, `unverified`, `partly_verified`, `ok`** (my call; it follows C§6.7.8 `status`: `degraded` (conflict, quiet) above `behind` above `catching_up`). A symbol with several reasons takes the worst; each symbol is counted once.
- The only page-side comparisons are: `verified_through_ms < end` and `verified_from_ms > start` (two API values against the asked window) and `T0 - newest > quiet_after_ms` (the API's own rule, V5, against the clock of the health answer, so that it agrees with `health.quiet_series` at `T0`). All three are tested against the **real** API at their boundaries (section 9).
- A series state is true at `T0` for `quiet` and `behind`, and at the answer's own `as_of_ms` for what the API computed (`complete`, `window_state`); OI-6 8.3 already accepts that different parts of one refresh are judged up to the length of the refresh apart.

## 5. One refresh, three phases (additive to OI-6 8.1, 8.2)

1. `/health/ingest`, `/v1/instruments`, **candle phase**, **open interest phase**: unchanged. When the candle phase is over every candle outcome is final; when the open interest phase is over every open interest outcome is final.
2. **Funding phase**, only when health says enabled: **every** instrument gets the call of 4.1 (`no_data` can be learned only from `404 unknown_series`, V4; same reason as OI-6 8.1). Same pool size (4), same one deadline measured from the start of the refresh, **checked before every call**. Symbols not reached are `unknown (deadline)` **for funding only**; calls in flight finish by their own timeout and their valid answers are used.
3. `refresh.deadline_hit` and `oi_deadline_hit` keep their meaning; **added** `funding_deadline_hit` and `funding_not_asked`. When the candle and open interest phases use the whole 60 s, no funding call is made and the page says so, as OI-6 does.
4. Funding `disabled`: zero funding calls, and **every existing key of the overview and every existing test is unchanged**. `/v1/funding-rates` joins the allowed paths and nothing else does.
5. Memory: the worker judges and drops the answer (as for open interest); a funding result holds a few integers.

## 6. The overview object (additive)

At the top level a `funding_rate` block: `status` (`disabled`, `unknown`, `enabled`), `detail`, `window` (`start`, `end` of 4.1), `summary` (counts of symbols by funding state, in the shape of the candle `summary` with the funding states) and `health` (the API's own `status`, `series_count`, `current`, `behind`, `catching_up`, `quiet_series`, `conflicted_series` as reported, labelled as the API's figures over all stored series, not over the symbols of this page). In `refresh` the keys of section 5. Per symbol a `funding_rate` block: `state`, `reasons` (`code`, `slots`, `runs`, `detail` as for the other parts: `slots` is the number of events for `conflict` and `0` otherwise, `runs` is `0`), `oldest_funding_time_ms`, `newest_funding_time_ms`, `verified_from_ms`, `verified_through_ms`, `verified_at_ms`, `window_state`, `events_in_window`, `conflicted_events`, `complete`, `as_of_ms`; `null` when disabled. Exact key names: builder. The symbol's `state` stays the candle headline; no existing key changes.

## 7. Performance

- **Paper model** (OI-6 7.1): a funding phase adds `⌈N/4⌉` sequential calls. `D ≈ (2 + ⌈N/4⌉·3)·L` with candles, open interest and funding, against `(2 + ⌈N/4⌉·2)·L` without funding:

| N | calls now → with funding | `D` with funding | max `L` for `D ≤ 60 s` | max `L` for `D ≤ 45 s` (AC1) |
|---|---|---|---|---|
| 100 | 202 → 302 | 77·L | 779 ms | 584 ms |
| 300 | 602 → 902 | 227·L | 264 ms | 198 ms |
| 600 | 1 202 → 1 802 | 452·L | 132 ms | 99 ms |

  A funding call is probably cheaper than a coverage call (two range counts, a key lookup, two rows, a body under 1 kB) but that is **not measured**: that is what the benchmark is for. The phases order protects candle and open interest results whatever it costs; **funding is the part that becomes `cannot judge` when the time runs out**.
- **Benchmark.** `tests/api/bench_overview_refresh.py` gains a funding phase after the open interest phase, a generated database with funding series (a span, rows, a few conflicts, one behind, one quiet, one shallow) and the API started with `[funding_rate]` enabled; `make bench-overview FUNDING_MODE=real` (default `off`, so existing runs keep their meaning; the API is started with `[funding_rate]` enabled in both measured variants of one run, so the cost of the larger health answer is in both and is not part of the measured difference; AC7 uses the factor 3 when funding is on; the sustained run keeps funding off and says so). It reports (the user asked for a thorough measurement with the total time and the breakdown, 2026-11-04) the **total wall time `D` of a refresh and the time of each phase** (head calls, candles, open interest, funding; min, p50, p95, max and the share of the total), the per-call latency per endpoint (p50, p95, p99, max, mean, count), the calls and the symbols not asked per phase, **the same figures with funding off from the same run** (so what funding adds is a measured difference), AC1 to AC7 (same thresholds) and the extra counts. **Run on the branch before the pull request at `N = 300` and `N = 600`; the numbers go into section 11 and the pull request.** Loopback only: AC7 still needs `L_tunnel` from the user.
- **Decision rule** if AC1 fails at 600: OI-6 7.4 applies unchanged (connection reuse, worker count, asking only some symbols; a batch endpoint only with its own ADR amendment). I make no claim now about which row will apply, and this slice **does not change the constants** (4 workers, 10 s, 60 s, 15 s).

## 8. What the page shows (layout left to the builder)

- **Summary:** the candle row and the open interest row as today; below them one labelled row of funding tiles (funding states) and one line with the API's own figures from `health` ("API view, all stored series: status, behind, catching up, quiet, conflicted in the last 24 h"). Disabled: one line "Funding rate is not enabled in this deployment" instead. Unknown: a visible warning that every symbol is listed as a problem because nothing says it is fine. Not reported by the tool: "Funding rate is not reported by the tool".
- **Problem list:** a symbol is listed when its candle, open interest **or funding** state is a problem; columns: candle state, open interest state, funding state, symbol, reasons, each reason prefixed `candles:`, `open interest:` or `funding:`. Order: the worst of the three by the combined rank (`no_data, unknown, stale, conflict, quiet, holes, behind, unverified, partly_verified, ok`), then the candle state, then symbol. The `ok` group is the symbols where **all applicable** parts are `ok`, labelled with the parts it covers.
- **Heatmap:** each symbol keeps its two strips and gains a **text line, not a strip**: "funding: verified from DAY to TIME UTC · newest settlement TIME UTC · N settlements from START to END (the asked window) · complete/not complete" (the window end is printed because the newest settlement is whole-series and can lie after it). A symbol whose funding is `cannot judge` shows the hatch-free sentence "cannot judge", never a blank.
- **Partial refresh:** a visible line for `funding_deadline_hit` with the count of symbols not asked.
- **Compatibility:** the page functions take the funding status as an optional last parameter, default `"absent"`; with `"absent"` every output is identical to today's. The candle and open interest tiles keep their own key list (they do not iterate the combined rank). Funding has its own state texts; the CSS classes of `behind`, `conflict`, `unknown`, `no_data` are shared and `quiet`, `unverified`, `partly_verified` get new ones (the meaning differs per part, so every tag sits in a labelled column).
- Page text is English, as today. No new color means "fine" for anything but `ok`.

## 9. Tests (CLAUDE.md: pytest, mocked, never production data)

- **Validation** (`validate.py`): every echo, type and consistency failure of 4.2 gives its code and never `ok`; a window in a state the API never returns (`verified` with `from > start`) is rejected; `as_of == T0 + 70 s` accepted, `+ 70 s + 1 ms` and `T0 - 1` rejected; health variants (absent, `null`, malformed pieces, `as_of != T0`).
- **Judging** (`judge.py`): every state and every reason alone and together; headline order; each symbol counted once; `ok` only for `complete` and not quiet; the boundaries `T0 - newest == quiet_after_ms` (not quiet) and `+ 1` (quiet), `verified_through_ms == end` (not behind) and `end - 1` (behind), `verified_from_ms == start` (not partly) and `start + 1`; `series` `null` with a span (quiet, verified, listed); a span that ends before the window gives `unverified` and `behind`, a span that starts after the window end gives `unverified` and **not** `behind`; `backfill_days` below 30 (the window start is clamped, a series as deep as configured is `ok`, a shallower one is `partly_verified`); `verified_through_ms` or the newest settlement after the answer's `as_of_ms` is `clock_inconsistent`; a symbol with `404 unknown_series` is `no_data`.
- **Refresh** (fake API, as `test_oi_refresh.py`): the request is exactly `venue, symbol, start=S, end=T0 - behind_after_ms + 1, limit=2`; every candle call precedes every open interest call precedes every funding call; **with a slow funding fake the candle and open interest parts are identical to a refresh without funding** and the unreached symbols are `unknown (deadline)` for funding only; no call after the deadline in any phase; at most 4 in flight; disabled: **zero funding calls and the overview equals today's**; malformed or absent key: no call and `unknown` for every symbol with the candle part intact; `404 funding_rate_disabled` with an enabled health is `unknown`, not `disabled`; transport failure, non-200, bad JSON, oversize body, an answer for another symbol; the funding block never changes a candle or open interest value.
- **Page decision functions** (Node, skipped without `node`, as `test_oi_page.py`): combined rank, `isProblem` with three parts, the ok group, reason prefixes, every disabled/unknown/absent notice, the line for each deadline.
- **Against the real API** (`test_funding_against_real_api.py`, as `test_oi_against_real_api.py`): a database seeded with a complete series, a conflicted one, a quiet one (no row; and a row older than `quiet_after_ms`), a behind one (through exactly `end` and `end - 1`), a shallow one, an unverified one, an unknown symbol, and funding disabled. This is the test that holds the page's three comparisons to the API's own `is_behind` and quiet rule; the page's states are asserted, not copied from the page's own helper.
- **Regression**: the existing tests of the tool pass **without edits (V8), with one exception**: `test_oi_window.py` pins `ALLOWED_PATHS` to exactly four paths and gains the fifth (one assertion; it is the allow-list the design asks to extend). The page's existing decision-function tests pass unedited because the funding status is an optional last parameter whose default `"absent"` gives output identical to today's (section 8); the boundary test keeps the tool to the standard library; `make check` passes.
- Manual check, documented in the README: the tool against a real API on a test database with the cases above and with funding disabled.

## 10. Calls of mine the user may want to overrule

| # | Call | Alternative and its cost |
|---|---|---|
| U1 | One call per symbol with `end = T0 - behind_after_ms + 1` (4.1): the API's `window_state` is `verified` exactly for series that are not behind. | A defaulted `end`: every symbol is `partly_verified` and the verdict is useless. Two calls per symbol (first the span, then the window): doubles the funding calls. |
| U2 | **`quiet` is judged by the page** from `T0`, `quiet_after_ms` and the newest settlement (V5), and listed in the problem list as the issue asks. | Show only `health.quiet_series` as a count and the newest settlement date per symbol: no per-symbol `quiet` reason. The cost: the issue's problem list loses `quiet`. |
| U3 | **No funding strip.** A text line with the verified span, the newest settlement and the counts (section 8). | A 720-character strip of the verified span (`.` inside, `_` outside), which compares visually with the other two but could be read as "green means complete" and shows nothing about conflicts. |
| U4 | State names and order of 4.3 (`quiet` between `conflict` and `behind`, `unverified` and `partly_verified` last). | Any other order is a one-line change in the page and the judge. |
| U5 | No release tag and no version bump (FR-6 is merged but released with the funding rate rollout; memory: tags only on request). | — |
| U7 | The funding window start is clamped to `backfill_days` (4.1) and the page says so. | Always ask from `S`: with `backfill_days < 30` every symbol is `partly_verified` forever, for a reason that is configuration. |
| U6 | The benchmark default stays `off`; the numbers of `N = 300` and `N = 600` on loopback are run **before** the pull request and shown in it, not as a separate gate. | A gate that stops the merge on a failed AC1 (as OI-6 G2). I read "one PR for related slices" as: no gate, the user reads the numbers in the PR. |

## 11. Measured numbers

`make bench-overview SERIES=<N> OVERVIEW_PROFILE=smoke FUNDING_MODE=real ARGS="--refreshes 10 --sustained-minutes 0"`, run on 2026-11-04 with the final code of this branch. **What this is:** loopback, one machine (macOS 27.0.1 arm64, Python 3.12.15, SQLite 3.53.1), a generated database of the `smoke` profile (9 000 candle rows and 8 640 open interest rows per series, a funding history of 30 days of settlements per series, never production data), the API in a separate process, 4 workers, a new connection per call, 10 measured refreshes of each variant after one discarded warm-up, the two variants alternating and the order inside a pair alternating. **What it is not:** the `target` profile (100 000 candle rows per series), the sustained run with the writer (AC4, AC5), the real refresh of the tool (AC8) or an SSH tunnel (AC7); none of these was measured. Other processes of another session were running on the machine (two at about 16 % CPU each), so every figure carries that noise. Two earlier attempts were discarded: a `target` run that was stopped (it took far too long), and a 300-symbol run that overlapped with a second benchmark instance.

Total time of a refresh and what each phase took (seconds, mean of 10; the share is the phase's part of the mean total):

| N | variant | total (min / p50 / p95 / max) | head calls | candles | open interest | funding |
|---|---|---|---|---|---|---|
| 300 | with funding | 9.51 (8.34 / 9.55 / 10.42 / 10.42) | 0.01 (0.1 %) | 4.81 (50.5 %) | 4.48 (47.1 %) | **0.21 (2.2 %)** |
| 300 | without | 9.15 (8.37 / 9.20 / 10.23 / 10.23) | 0.01 | 4.72 (51.6 %) | 4.42 (48.3 %) | – |
| 600 | with funding | 20.85 (20.05 / 20.72 / 22.21 / 22.21) | 0.02 (0.1 %) | 10.56 (50.7 %) | 9.80 (47.0 %) | **0.47 (2.2 %)** |
| 600 | without | 20.40 (19.39 / 20.46 / 20.84 / 20.84) | 0.02 | 10.48 (51.4 %) | 9.90 (48.5 %) | – |

- **What funding added:** 0.36 s at 300 symbols (3.8 % of the refresh) and 0.45 s at 600 symbols (2.2 %). The per-call latency (mean, p95, max in ms): candle 63.9 / 76.8 / 110 and 70.3 / 84.3 / 118, open interest 59.6 / 72.6 / 98 and 65.2 / 76.6 / 116, **funding 2.8 / 4.1 / 6.3 and 3.1 / 4.4 / 7.3** (300 and 600 symbols). A funding call is a key lookup and two range counts over a few rows, about twenty times cheaper than a coverage call, so the paper model of section 7 (all calls equal) was far too pessimistic: its 452·L would be 45 s at L = 100 ms, the measured refresh is 21 s.
- **Acceptance criteria that were evaluated** (loopback only): AC1 (maximum of the refreshes <= 45 s): 10.42 s and 22.21 s, **pass**. AC2 (no failed call, nothing not asked, in any phase): 0 and 0, **pass**. AC3 is the latency above. AC6 (largest body): 32 117 and 64 217 bytes, decode 0.4 ms at most, **pass**. The API process's peak resident set was 77 and 78 MiB. AC4, AC5, AC7 and AC8 were not evaluated.
- **A prediction, labelled as one, for the tunnel (AC7):** the 450 sequential rounds of the three phases at 600 symbols each pay the latency of the tunnel once, so with funding a refresh of 20.85 s stays under 45 s as long as `L_tunnel <= (45 - 20.85) / 452 = 53 ms` per call; without funding the limit is `(45 - 20.4) / 302 = 81 ms`. Funding therefore lowers the tolerated tunnel latency by about a third, even though it adds only 2 % on loopback. The user's `L_tunnel` is still needed for the verdict.
- The first, clean run with the earlier method (fixed order inside the pair, no warm-up) gave 10.26 s / 10.51 s and 20.99 s / 21.58 s, funding +0.26 s and +0.59 s: the same picture; the method was changed because the fixed order favoured the second variant.

## 12. Risks and accepted limits

- **The refresh gets longer.** Funding is the third phase; at 600 symbols the paper model needs a mean call latency below 132 ms to finish and below 99 ms to meet AC1. Candles and open interest keep their results whatever happens (section 5); funding may be `cannot judge` for many symbols. Measured in section 11, not assumed.
- **Three page-side comparisons** (4.3) could drift from the API's rules (`is_behind`, quiet). They are three lines each, use the API's own values, and are tested against the real API at the boundary.
- **`ok` is not a proof.** A late settlement inside the verified span can still appear (ADR-4 P-8); the page says what the Collector verified at the API's time of that answer.
- **The API's `health.funding_rate` counts differ from the page's** (stored series versus configured instruments; conflicts of 24 h versus the 30-day window). The page labels the API's figures as the API's and does not reconcile them.
- A symbol the Collector **suspended** (ADR-4 P-10) is not visible to the API and shows as `behind`, then `quiet`; the reason texts say the cause is not visible.
