# Symbol detail page — design

- **Date:** 2026-11-06.
- **Status:** IMPLEMENTED on branch `feat/symbol-detail` (plan `docs/superpowers/plans/2026-11-06-symbol-detail.md`); APPROVED by the user on 2026-11-06 ("schvaluji"), including the chart library of section 5.4. Everything marked **my call** is a default the user can overturn.
- **Base:** the dashboard of pull request #30 (branch `feat/overview-redesign`, not yet merged), which already links every symbol to its detail and lists this page as follow-up F-24. The work is a branch `feat/symbol-detail` started from that branch and a pull request against it; it closes F-24. (Found after the first draft of this document, which was written from `main`.)
- **Size:** a small project in one pull request: a new read-only route and validation in `tools/symbol_overview/`, a detail view in its page, one vendored JavaScript library. No change to the Collector, the API, the database, any schema or any contract.
- **How to read this document:** section 4 holds facts read in the code and the contract. Section 5 holds design decisions. A statement is never in both.

## 1. Intent

The symbol overview page (`tools/symbol_overview/`, design `2026-11-01-symbol-overview-design.md`) answers "is anything wrong?". When the user clicks a symbol they want to see **that one symbol**: its state, its latest numbers, its last hours or days of candles, open interest and funding rate, and how complete its history is. The user supplied a dark-theme mockup (BTC/USDT detail with a header, a status card, three KPI cards, three stacked charts, coverage bars) and asked to show **only data the API really has**, and not to change the API.

## 2. Scope and what the user decided

- A detail view per symbol, reached by clicking the symbol in the "Needs attention" and "Healthy" lists of the dashboard, and by a URL that can be bookmarked.
- Taken from the mockup: header (symbol, state tag, venue, Previous/Next), status card, three KPI cards (latest candle, open interest, funding rate), three stacked charts with a range switch, history coverage, quality checks.
- Left out because the API has no such data: the Logs tab, "latest events", data size in GB, the top navigation (Collectors, Sources, API), a "Perpetual" or "primary" label.
- The existing API is the only data source; no API change.
- If a UI framework or a component library would help, propose it (done in 5.4).

## 3. Goals, non-goals, anti-goals

**Goals**
- Everything on the detail view is a stored value from the API or a state the overview tool already judged; nothing else.
- Works the same locally, in the container and through the SSH tunnel; keeps the tool's isolation (read-only, only the API).

**Non-goals**
- Indicators, order books, trades, alerts, history of past states, authentication, any control that writes.
- Timeframes other than `5m`, venues other than `binance-usdm` (the limits of API v1).
- Changing what the overview shows or how it judges.

**Anti-goals (a failure even if it technically works)**
- **A line that crosses a hole.** A missing slot must show as a gap in the candle and open interest charts, never an interpolated segment. A funding rate must not be drawn as a step line that implies values between settlements without saying so.
- **A green or empty detail when the truth is unknown.** An API error, a timeout, an invalid or contradicting answer, or a partly loaded range is shown as such, never as an empty chart or a stale one that looks current.
- **A "live price".** The latest candle is shown with its own time and age; it is the last *closed* candle, not a quote.
- Hiding a conflicted candle or row: it is marked (it carries its first stored values, C§5.2).
- Letting the browser call anything but the tool, or letting the tool call anything but read-only API paths.

## 4. Verified facts

| # | Fact | Source |
|---|---|---|
| D1 | The browser talks only to the tool; the API has no CORS. The tool's HTTP client allows exactly five paths (`/health/ingest`, `/v1/instruments`, `/v1/coverage`, the open interest coverage path, `/v1/funding-rates`) and its server serves only `/` and `/overview.json`. | `fetch.py::ALLOWED_PATHS`, `server.py` |
| D2 | On the dashboard (PR #30) every name in "Needs attention" and "Healthy" is a link `#/symbol/<encodeURIComponent(symbol)>?venue=<enc>&timeframe=<enc>` that leads nowhere yet (follow-up F-24). The page is dark only, one inline script of about 500 lines with a pure-function part above the marker `// --- DOM below ---` and a DOM part, one request (`/overview.json`), no external asset, no router. The strips, the heatmap and the per-symbol funding line were removed from the page but `/overview.json` still carries `strip`, `oldest_*`, `newest_*`, `as_of_ms` and the funding fields per symbol. | `feat/overview-redesign:tools/symbol_overview/page.html`, `docs/follow-ups.md` F-24 |
| D3 | `GET /v1/candles` returns at most 1000 candles per call (`next` pages by time), the window is at most 10 000 slots; candle values are exact decimal strings, `turnover` may be `null`, `conflicted` is always present. 30 days of `5m` are 8 640 candles, nine calls. | C§5.1, C§5.2 |
| D4 | `GET /v1/open-interest` has the same limits; `time_ms` is the period **end**, rows carry `open_interest` and `open_interest_value` as decimal strings; a defaulted window does not show the age of a stopped series, so an explicit `end` is needed. | C§6.5.2, C§6.5.4 |
| D5 | `GET /v1/funding-rates` rows are per settlement (`funding_time_ms`, `rate_type`, `funding_rate`, nullable `mark_price`); the window is the closed interval `[start, end]`, no grid; the `coverage` block describes the whole window with a verified span, not a percentage. | C§6.7.2, C§6.7.4, C§6.7.5 |
| D6 | The API has no "latest N" parameter; the last N candles are read with `start = newest - (N-1) x 5m`. | C§5.1 |
| D7 | The overview tool already computes, per symbol, a state, reasons in words, `oldest_open_time_ms`, `newest_open_time_ms`, `as_of_ms` and a strip for candles and open interest, and a funding state and line. The overview refresh is shared and cached for 15 s. | `model.py`, `refresh.py`, `page.html` |
| D8 | The `overview` container is limited to 128 MiB and copies the whole `tools/symbol_overview/` directory into the image. | `compose.yaml`, `Dockerfile` target `overview` |
| D9 | `tests/tools/symbol_overview/test_boundary.py` allows only standard-library Python imports in the tool; it does not look at JavaScript. | `test_boundary.py` |
| D10 | The API's database has one writer and is read here only through the API; the SSH tunnel and `make overview` are the ways the page is opened. | README "Symbol overview" |

## 5. Design decisions

### 5.1 Route and links

- The detail is a **hash route** in the same page, the one the dashboard's links already use: `#/symbol/<encodeURIComponent(symbol)>?venue=<enc>&timeframe=<enc>`. Venue and timeframe select the series, because a symbol name can occur in more than one series. The empty hash is the dashboard, unchanged. No server routing is needed and the existing `/` route keeps serving one page.
- The links already exist (D2); this project draws what they point to. The dashboard's filter survives going back (builder's detail).
- Previous / Next walk the rows of the overview's last snapshot in the order of `/overview.json` (the API's order by venue, symbol, timeframe). They are disabled at the ends and when the snapshot is not available.
- An unknown symbol in the URL shows "this symbol is not in the configured universe" with a link back, never an empty detail.

### 5.2 Where the data comes from

- **Header, state tag, status card, coverage bars, quality checks** come from the overview snapshot (`/overview.json`) that the page already loads. The page does not judge states again; it reuses the tool's states, reasons and strips (D7). Fields the snapshot lacks (for example the present-slot counts behind a coverage percentage) are added to `/overview.json` as **additive** fields; existing fields keep their meaning. (**my call**)
- **Charts and KPI cards** come from one new tool route, `GET /detail.json?symbol=<symbol>&venue=<venue>&timeframe=<timeframe>&range=<1h|6h|24h|7d|30d>` (the series is the triple, as in the dashboard's links). The tool fetches, for that one symbol, candles, open interest rows and funding rows through the same HTTP client (its allowlist gets `/v1/candles` and `/v1/open-interest`, the paths of sections 5 and 6.5 of the contract; `/v1/funding-rates` is already there), validates them as strictly as it validates coverage today (`validate.py`), and returns one normalized document. Any other query is `400`, any invalid or unreachable answer is an explicit error body, never partial data presented as complete. (**my call**)
- **Counts need no extra calls.** The tool already receives `present_slots`, `missing_slots`, the other slot counts and the runs from `/v1/coverage` and the open interest coverage endpoint for the 30-day window (`model.py::Coverage`, `OiCoverage`); `/overview.json` simply does not pass the counts on today. Passing them is the additive field above. Holes come from the same `runs`; the tool never derives a gap from the candle grid. (answers a builder question)
- **Fail closed per part, not per page.** `/detail.json` loads candles, open interest and funding independently, and each part has its own status (`ok` or an error code). A part is `ok` only when **every** page of its range loaded and validated; one failed page makes that whole part an error (no half-drawn series). A failed part shows an error in its own pane and its KPI card says "cannot be loaded"; the other parts still show. (**my call**)
- **Lookback for change figures.** The KPI changes need a value before the latest one: the tool also reads the candle one slot before the window start and the open interest rows back to one hour before the newest row, outside the selected range, so that the change is computable in the `1h` range too. If that earlier value is missing the change shows "n/a", never an estimate. The change is always the latest value against that fixed reference, whatever the range. (**my call**)
- **The window** is anchored on the API's clock (`as_of_ms` from `/health/ingest`), as `window.py` does for the overview; never on the browser's clock. The range is the last N slots, explicit `start` and `end`, aligned (D6, D4).
- **Decimals.** Values stay exact decimal strings in the document and in every number printed on the page (KPI cards, tooltips). A conversion to a floating point number happens only in the browser, only to place a point on a chart, and the chart's price labels are formatted from that display value. Nothing converted is ever shown as the stored value. (CLAUDE.md: exact decimals)

### 5.3 What each part shows

| Part (mockup) | Shown from | Notes |
|---|---|---|
| Header: symbol, state tag, venue, Previous / Next | overview snapshot | The venue is the stored `binance-usdm`. No "Perpetual", no "primary". |
| Status card | overview snapshot | State and its reasons in words; last candle open time and its age; data source = venue; timeframe `5m`; history from `oldest_open_time_ms`. Data size is not shown (no data). |
| KPI: latest candle | `/detail.json` | Close, O/H/L, time, age. The change is against the previous stored candle's close, shown only when that candle is the adjacent slot; otherwise "n/a". |
| KPI: open interest | `/detail.json` | Latest `open_interest` (the quantity; the chart and the change figure use it) and `open_interest_value` (quantity times price, shown as a line only: it follows the price), its time; change of the quantity against the row one hour earlier only if that row exists. Amended after the user saw the value curve repeat the price curve. |
| KPI: funding rate | `/detail.json` | Latest settlement rate, its time, `rate_type`. The mockup's "in 7 h 57 min" is not shown: the next settlement is not an API value. |
| Three stacked charts, range switch 1h / 6h / 24h / 7d / 30d | `/detail.json` | Candlesticks with volume (builder's call whether volume is a sub-series), open interest as an area, funding as bars at settlement time. Only funding rows inside the shown range are plotted; the funding KPI uses the latest settlement of the last seven days whatever the range. One shared time axis and crosshair. Gaps are gaps (anti-goals). Conflicted candles and rows are marked. Times are UTC, as everywhere on the page. |
| History coverage bars | overview snapshot | Candles and open interest: present slots over the slots of the **stored history** inside the 30-day window (from the oldest stored candle to the newest), from the API's counts; labelled "of stored history", so a series that stopped days ago reads its own completeness, and how current it is shows in the quality card's delay row (**my call**). Funding has no percentage (D5): it shows the verified state in words, as the overview's funding line does. |
| Quality checks | overview snapshot | One row per part: candle continuity (holes, conflicts), open interest, funding rate, data delay (behind / stale with the API's rule). Each row has the reason in words. The mockup's "valid OHLCV" row is left out: the API offers no such check. |
| Last events, Logs tab | — | Left out (no data). |

### 5.4 Chart library: proposal for the user's approval

- **Proposal: `lightweight-charts` 5.2.1** (TradingView, Apache-2.0, released 2026-08-12), the standalone production build, **vendored** as one file in `tools/symbol_overview/vendor/` with its license text and the SHA-256 of the file recorded in the repository, served by the tool from a fixed route (no CDN: the page must work through the tunnel and with no route out, D1, D8; and no unpinned third-party code at run time). It is a drawing library, not a framework: no build step, no Node, no change to the Python side beyond a route.
- **Why a library and not hand-drawn canvas:** the mockup is three panes with a shared time axis, a crosshair that follows across panes, price labels, zoom and pan, and a candlestick series. The existing strips are one-dimensional canvas rows; reproducing the rest by hand is the larger part of the work and the least testable.
- **Alternatives considered and dropped:** hand-written canvas (more code, more bugs, no crosshair sync); uPlot or Chart.js (no candlestick series without a plugin); ECharts (several times larger, heavier than needed); a UI framework such as React or Vue (needs a build step and Node in a repository and an image that have neither; the page's DOM helper is enough for a handful of cards).
- **Conditions:** the vendored file is covered by a test that compares its hash with the recorded one; the version is bumped only deliberately; the license and notice obligations of the package are checked by the builder before merging and its notice is kept next to the file; `test_boundary.py` stays as it is (it covers Python only, D9).
- If the user declines the library, the fallback is hand-drawn canvas with a single shared crosshair and no zoom; the rest of the design does not change.

### 5.5 Cost and load

- One detail view costs the API: the candle pages, the open interest pages and one funding call for the **selected range** only (1h to 7d: one call each; 30d: nine candle calls and nine open interest calls). Other ranges load when they are selected. (**my call**)
- Detail answers are cached by the tool for a short time per symbol and range (the same idea as the overview's 15 s), with a bounded cache so the 128 MiB limit (D8) is not threatened. The three parts of one detail run in parallel (one call each, so the detail adds at most 3 calls in flight to the overview refresh's 4, **my call**, amended after the plan review); one detail refresh runs at a time and a waiting request gives up with a `busy` answer. The cache holds the encoded answers under a byte budget (16 MiB); a failed or partly failed detail is kept for 5 s only, so a retry reaches the API soon. The peak memory of a 30-day detail is measured before the pull request is opened.
- The page re-fetches the whole detail document (no incremental append, so no cursor or gap logic) every 60 s like the overview, only while the detail is shown and the browser tab is visible. The cache lifetime grows with the range: 15 s for `1h` to `24h`, 60 s for `7d` and `30d`. (**my call**)
- A bookmarked `#/symbol/...` waits for the first overview snapshot before it draws anything (a loading line, then the detail or an error); the header, state tag and checks all depend on it, so the page never shows charts under a missing state. (**my call**)

### 5.6 Look and language

- **Language: English**, as the page's text is today, although the 2026-11-01 design said the text would be Czech like the mockups. (**my call**; the existing page is English, and mixing languages would be worse.)
- **Theme:** dark only, like the dashboard it extends (`color-scheme: dark`, CSS variables `--bg --card --line --fg --muted --ok --warn --bad --accent`). The charts take their colors from the same variables, so the detail looks like the user's mockup. The state colors and tags of the dashboard are reused.
- Usable on a phone: the three panes stack, the KPI cards wrap, as the overview does today.

## 6. What is left to the builder

- Splitting `page.html` (a separate static script for the detail, with its own fixed route, is acceptable) and the exact wire shape of `/detail.json`.
- Paging, concurrency, deadline and cache numbers of 5.5.
- How volume, conflicts and the change figures are drawn.
- Test layout: pure functions for validation and the change computation with fake API answers (`tests/tools/symbol_overview/fake_api.py` pattern); a test against a real API where the existing ones do (`test_against_real_api.py`); the hash test of 5.4; the existing boundary test unchanged.
- README and `docs/` updates (the tool's route list, the vendored file, the new fields).

## 7. Risks and what is not verified

- Colors, layout and the charts are not covered by an automated test (as for the overview today); they need a look in a light and a dark browser.
- A 30-day detail is nineteen API calls; no benchmark covers it yet.
- The license and notice obligations of the chosen library are not yet checked in the package itself. This check is the **first task of the plan**, before any code uses the library; if it finds an obligation the user does not accept, the fallback of 5.4 applies.
- This change is not a deployment: nothing is tagged or released (the release rule and the funding rate deployment stay as they are).
