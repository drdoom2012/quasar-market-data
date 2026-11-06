"use strict";
// The symbol detail view (design docs/superpowers/specs/2026-11-06-symbol-detail-design.md). This file shares the
// global scope with the inline script of page.html: it reuses TEXT, fmt, levelOf, worstState, partState, stateText,
// reasonText, oiReasonText, fundingReasonText, percentText, symbolHref, oiStatus, fundingStatus, oiStateOf,
// fundingStateOf, applies, known, $ and el, and its own top-level names never repeat one of that script's (a test
// compares the two sets). Everything above the marker below is pure (no document, no window); the DOM part is below.
// The detail judges nothing: states, reasons and counts are the overview's, rows are what the API stored. Values stay
// exact decimal text; Number() is used only where a point is placed on a chart.

const DETAIL_TF = 300000;
const OI_REFERENCE_MS = 12 * DETAIL_TF;
const CONFLICT_COLOR = "#c2186b";  // a conflicted candle or row: the stored values are the first ones, the rejected value is not shown
const DETAIL_RANGES = ["1h", "6h", "24h", "7d", "30d"];
const DETAIL_TEXT = {
  back: "Symbols",
  previous: "Previous",
  next: "Next",
  loading: "Loading the symbol…",
  invalid: "This link is not a valid symbol link.",
  notFound: "This series is not in the overview.",
  overviewError: "The overview could not be loaded, so nothing can be shown for this symbol.",
  scriptFailed: "The detail script could not be loaded.",
  chartsFailed: "The chart library could not be loaded; the numbers above are still shown.",
  sections: { status: "Symbol status", candles: "Latest candle", openInterest: "Open interest", funding: "Funding rate", coverage: "History coverage", quality: "Quality checks", charts: "Charts" },
  status: { state: "State", lastUpdate: "Last candle closed", source: "Data source", timeframe: "Timeframe", history: "History from" },
  quality: { continuity: "Candle continuity", openInterest: "Open interest", funding: "Funding rate", delay: "Data delay" },
  ranges: { "1h": "1h", "6h": "6h", "24h": "24h", "7d": "7d", "30d": "30d" },
  panes: { candles: "Price (5m candles)", openInterest: "Open interest (quantity)", funding: "Funding rate" },
  coverageOf: "of stored history",
  coverageFunding: "Funding has no time grid, so it has no percentage: its state is the one above.",
  noData: "no data in this range",
  nothingStored: "nothing stored yet",
  notEnabled: "not enabled in this deployment",
  noSettlement: "no settlement stored in the last 7 days",  // an empty or unverified window does not say none occurred
  specialNotPlotted: (n) => `${n} ${n === 1 ? "special settlement" : "special settlements"} in this range ${n === 1 ? "is" : "are"} not plotted`,
  fetched: (s) => `answer is up to 60 s old; fetched ${s} s ago`,
  notice: "TradingView Lightweight Charts™ · Copyright (c) 2025 TradingView, Inc.",
  noticeLink: "https://www.tradingview.com/",
  conflicted: "conflicted: the stored value is the first one the Collector saw",
};

// --- the route ---------------------------------------------------------------------------------------------------

// "#/symbol/<symbol>?venue=<v>&timeframe=<t>" is a series; "" and "#" are the dashboard; anything else is invalid.
function parseRoute(hash) {
  if (hash == null || hash === "" || hash === "#") return { view: "overview" };
  const match = /^#\/symbol\/([^?]*)\?(.*)$/.exec(hash);
  if (!match) return { view: "invalid" };
  try {
    const params = new Map();
    for (const pair of match[2].split("&")) {
      const at = pair.indexOf("=");
      if (at < 0) return { view: "invalid" };
      params.set(decodeURIComponent(pair.slice(0, at)), decodeURIComponent(pair.slice(at + 1)));
    }
    const symbol = decodeURIComponent(match[1]), venue = params.get("venue"), timeframe = params.get("timeframe");
    if (!symbol || !venue || !timeframe) return { view: "invalid" };
    return { view: "symbol", symbol, venue, timeframe };
  } catch (error) {
    return { view: "invalid" };
  }
}
function findRow(symbols, route) {
  return symbols.find((s) => s.symbol === route.symbol && (s.venue || "") === route.venue && (s.timeframe || "") === route.timeframe) || null;
}
function neighbours(symbols, route) {
  const at = symbols.findIndex((s) => s.symbol === route.symbol && (s.venue || "") === route.venue && (s.timeframe || "") === route.timeframe);
  if (at < 0) return { prev: null, next: null };
  return { prev: at > 0 ? symbols[at - 1] : null, next: at < symbols.length - 1 ? symbols[at + 1] : null };
}
function rowHref(row) { return symbolHref(row.symbol, row.venue, row.timeframe); }

// What the detail view shows. `data` is the last overview object, or null (not loaded yet, or the overview failed:
// `loadState.overviewFailed`). Nothing of the detail is drawn without a good overview.
function detailViewState(route, data, loadState) {
  if (route.view === "overview") return { show: "overview" };
  if (route.view === "invalid") return { show: "invalid" };
  if (data == null) return { show: loadState && loadState.overviewFailed ? "overview_error" : "loading" };
  const row = findRow(data.symbols, route);
  return row ? { show: "detail", row } : { show: "notfound" };
}

// A load of the detail document. A result is applied only if no later load or cancel happened (a slow answer for a
// symbol or range the user has left is never drawn). `cancel` also forgets the started key, so a later good
// snapshot starts the load again.
function createDetailLoader(fetchJson, apply) {
  let generation = 0, startedKey = null;
  return {
    load(key) {
      generation += 1;
      const mine = generation;
      startedKey = key;
      Promise.resolve().then(() => fetchJson(key)).then(
        (result) => { if (mine === generation) apply(key, result); },
        (error) => { if (mine === generation) apply(key, { error: String(error) }); });
    },
    cancel() { generation += 1; startedKey = null; },
    started(key) { return startedKey === key; },
  };
}

// --- numbers and times (text in, text out) ------------------------------------------------------------------------

// Thousands separators on the integer part of a decimal STRING, never through Number.
function groupDecimal(text) {
  const match = /^(-?)([0-9]+)(\.[0-9]+)?$/.exec(String(text));
  if (!match) return String(text);
  return match[1] + match[2].replace(/\B(?=([0-9]{3})+(?![0-9]))/g, " ") + (match[3] || "");
}
// A fraction as a percent by moving the decimal point two places in the text: "0.0001" -> "0.01".
function shiftPercent(text) {
  const match = /^(-?)([0-9]+)(?:\.([0-9]+))?$/.exec(String(text));
  if (!match) return String(text);
  const fraction = (match[3] || "").padEnd(2, "0");
  const whole = (match[2] + fraction.slice(0, 2)).replace(/^0+(?=[0-9])/, "");
  const rest = fraction.slice(2).replace(/0+$/, "");
  const value = rest ? whole + "." + rest : whole;
  return (value === "0" ? "" : match[1]) + value;
}
function ageText(ms) {
  if (!Number.isInteger(ms) || ms < 0) return "–";
  const s = Math.floor(ms / 1000);
  if (s < 180) return `${s} s`;
  const min = Math.floor(s / 60);
  if (min < 120) return `${min} min`;
  const h = Math.floor(min / 60);
  if (h < 48) return `${h} h ${min % 60} min`;
  return `${Math.floor(h / 24)} d ${h % 24} h`;
}
// How long ago the newest candle CLOSED, by the API's clock (the browser's clock is never used for a state).
function lastCloseAgeMs(s) {
  if (!Number.isInteger(s.as_of_ms) || !Number.isInteger(s.newest_open_time_ms)) return null;
  return s.as_of_ms - (s.newest_open_time_ms + DETAIL_TF);
}
function quoteCurrency(symbol) {
  const match = /\/([^:]+)/.exec(String(symbol));
  return match ? match[1] : "";
}

// --- coverage and quality (from the overview's own fields) ----------------------------------------------------------

// The share of STORED history that is present inside the 30-day window; how current the data is is the delay row.
function historyCoverage(s, window_) {
  const present = s.present_slots, oldest = s.oldest_open_time_ms, newest = s.newest_open_time_ms;
  if (![present, oldest, newest, window_.start, window_.end].every(Number.isInteger)) return null;
  const span = (Math.min(newest + DETAIL_TF, window_.end) - Math.max(oldest, window_.start)) / DETAIL_TF;
  if (!(span > 0) || !Number.isInteger(span) || present < 0 || present > span) return null;
  return { present, span, text: percentText(present, span) };
}
function openInterestCoverage(block, oiWindow) {
  if (!block || !oiWindow) return null;
  const present = block.present_slots, oldest = block.oldest_time_ms, newest = block.newest_time_ms;
  if (![present, oldest, newest, oiWindow.start, oiWindow.end].every(Number.isInteger)) return null;
  const span = (Math.min(newest, oiWindow.end - DETAIL_TF) - Math.max(oldest, oiWindow.start)) / DETAIL_TF + 1;
  if (!(span > 0) || !Number.isInteger(span) || present < 0 || present > span) return null;
  return { present, span, text: percentText(present, span) };
}
// Four rows: candle continuity, open interest, funding rate, data delay. `state` is one of the page's states, or null
// when the part does not apply; `lines` are the reasons in words (the overview's own texts).
function qualityRows(s, status, fstatus, finfo) {
  const reasons = s.reasons || [];
  const has = (code) => reasons.some((r) => r.code === code);
  const words = (codes) => reasons.filter((r) => codes.includes(r.code)).map((r) => reasonText(r, s));
  let continuity, delay;
  const candle = known(s.state);
  if (candle === "no_data" || candle === "unknown" || candle === "unrecognised") {
    const all = reasons.map((r) => reasonText(r, s));
    continuity = { state: candle, lines: all };
    delay = { state: candle, lines: [] };
  } else {
    continuity = { state: has("conflict") ? "conflict" : has("missing") || has("unavailable") ? "holes" : "ok", lines: words(["conflict", "missing", "unavailable"]) };
    delay = { state: has("stored_ahead") ? "unknown" : has("stale") ? "stale" : has("due") ? "behind" : "ok", lines: words(["stale", "due", "stored_ahead"]) };
  }
  const oi = oiStateOf(s, status);
  const interest = oi === null
    ? { state: null, lines: [status === "disabled" ? TEXT.oiDisabled : TEXT.oiAbsent] }
    : { state: oi, lines: s.open_interest ? s.open_interest.reasons.map((r) => oiReasonText(r, s.open_interest)) : [TEXT.oiMissing] };
  const fund = fundingStateOf(s, fstatus);
  const funding = fund === null
    ? { state: null, lines: [fstatus === "disabled" ? TEXT.fundingDisabled : TEXT.fundingAbsent] }
    : { state: fund, lines: s.funding_rate ? s.funding_rate.reasons.map((r) => fundingReasonText(r, s.funding_rate, finfo)) : [TEXT.fundingMissing] };
  return [{ key: "continuity", ...continuity }, { key: "openInterest", ...interest }, { key: "funding", ...funding }, { key: "delay", ...delay }];
}

// --- the cards ---------------------------------------------------------------------------------------------------

function partMessage(part, hasRows, newestMs) {
  if (part.status === "empty") return DETAIL_TEXT.nothingStored;
  if (part.status === "disabled") return DETAIL_TEXT.notEnabled;
  if (part.status === "error") return `cannot be loaded (${part.code}${part.detail ? ": " + part.detail : ""})`;
  if (part.status !== "ok") return "cannot be shown";
  if (hasRows) return null;
  return newestMs == null ? DETAIL_TEXT.noData : `${DETAIL_TEXT.noData} (newest stored: ${fmt(newestMs)})`;
}
function changeText(change, suffix) {
  return change ? `change ${change.percent} % ${suffix}` : "change n/a";
}
// One card per part: `head` is the big value (exact text) or null, `lines` the rest. Never a number the API did not store.
function kpiModel(detail) {
  const quote = quoteCurrency(detail.symbol);
  const candles = detail.candles, interest = detail.open_interest, funding = detail.funding_rate;
  const out = {};
  const last = candles.status === "ok" && candles.rows.length ? candles.rows[candles.rows.length - 1] : null;
  const candleMessage = partMessage(candles, last !== null, candles.newest_ms);
  out.candle = last === null ? { status: candles.status, head: null, lines: [candleMessage] } : {
    status: "ok", head: groupDecimal(last.c),
    lines: [`open ${groupDecimal(last.o)} · high ${groupDecimal(last.h)} · low ${groupDecimal(last.l)}`,
      `closed ${fmt(last.t + DETAIL_TF)} · ${ageText(detail.as_of_ms - (last.t + DETAIL_TF))} before the API clock`,
      changeText(candles.change, "vs the previous candle")].concat(last.conflicted ? [DETAIL_TEXT.conflicted] : []),
  };
  const row = interest.status === "ok" && interest.rows.length ? interest.rows[interest.rows.length - 1] : null;
  const interestMessage = partMessage(interest, row !== null, interest.newest_ms);
  out.openInterest = row === null ? { status: interest.status, head: null, lines: [interestMessage] } : {
    status: "ok", head: groupDecimal(row.oi),
    lines: [`quantity in the base asset (unit inferred, not documented by Binance)`,
      quote ? `value ${groupDecimal(row.value)} ${quote} (quantity times price, so it follows the price)` : `value ${groupDecimal(row.value)}`,
      `period ended ${fmt(row.t)}`, changeText(interest.change, "of the quantity over 1 h")].concat(row.conflicted ? [DETAIL_TEXT.conflicted] : []),
  };
  const latest = funding.status === "ok" ? funding.latest : null;
  const fundingMessage = funding.status === "ok" ? DETAIL_TEXT.noSettlement : partMessage(funding, false, null);
  out.funding = latest == null ? { status: funding.status, head: null, lines: [fundingMessage] } : {
    status: "ok", head: shiftPercent(latest.rate) + " %",
    lines: [`${latest.rate_type} settlement ${fmt(latest.t)}`, `rate ${latest.rate} (a fraction per settlement; unit inferred)`].concat(
      latest.mark_price == null ? [] : [`mark price ${groupDecimal(latest.mark_price)}`], latest.conflicted ? [DETAIL_TEXT.conflicted] : []),
  };
  return out;
}
// A pane is drawn only from an ok part that has rows; otherwise it shows why, never an empty chart.
function paneModel(part) {
  const rows = part.status === "ok" && Array.isArray(part.rows) ? part.rows : [];
  if (rows.length) return { draw: true, text: "" };
  return { draw: false, text: partMessage(part, false, part.newest_ms) || DETAIL_TEXT.noData };
}
function tooltipLines(kind, row) {
  const lines = [];
  if (kind === "candles") lines.push(fmt(row.t), `open ${groupDecimal(row.o)} · high ${groupDecimal(row.h)} · low ${groupDecimal(row.l)} · close ${groupDecimal(row.c)}`, `volume ${groupDecimal(row.v)}`);
  else if (kind === "openInterest") lines.push(`period ended ${fmt(row.t)}`, `open interest ${groupDecimal(row.oi)}`, `value ${groupDecimal(row.value)}`);
  else lines.push(fmt(row.t), `${row.rate_type} rate ${row.rate}`, row.mark_price == null ? "mark price: none stored" : `mark price ${groupDecimal(row.mark_price)}`);
  if (row.conflicted) lines.push(DETAIL_TEXT.conflicted);
  return lines;
}

// --- chart data (the only place Number() is used) -------------------------------------------------------------------

// One entry per slot of [startMs, endMs): the point of the row stored for that slot, else a whitespace point {time}.
// A slot without a row is therefore a gap in the chart, never a line drawn across it.
function denseSeries(rows, startMs, endMs, stepMs, toPoint, slotOf) {
  const byTime = new Map(rows.map((r) => [slotOf ? slotOf(r.t) : r.t, r]));
  const points = [];
  for (let t = startMs; t < endMs; t += stepMs) {
    const row = byTime.get(t);
    points.push(row === undefined ? { time: t / 1000 } : toPoint(row));
  }
  return points;
}
// colors: {up, down, volUp, volDown}, read from the page's CSS variables by the DOM part.
function candleBars(rows, window_, colors) {
  return denseSeries(rows, window_.start, window_.end, DETAIL_TF, (r) => {
    const point = { time: r.t / 1000, open: Number(r.o), high: Number(r.h), low: Number(r.l), close: Number(r.c) };
    if (r.conflicted) { point.color = CONFLICT_COLOR; point.borderColor = CONFLICT_COLOR; point.wickColor = CONFLICT_COLOR; }
    return point;
  });
}
function volumeBars(rows, window_, colors) {
  return denseSeries(rows, window_.start, window_.end, DETAIL_TF, (r) => ({
    time: r.t / 1000, value: Number(r.v), color: r.conflicted ? CONFLICT_COLOR : Number(r.c) >= Number(r.o) ? colors.volUp : colors.volDown }));
}
// An open interest row is stamped with the END of its period; it belongs to the candle that OPENED one period earlier (C§6.5.2).
function oiPlotMs(t) { return t - DETAIL_TF; }
function oiPoints(rows, window_) {
  return denseSeries(rows, window_.start, window_.end, DETAIL_TF, (r) => {
    const point = { time: oiPlotMs(r.t) / 1000, value: Number(r.oi) };  // the quantity: the value (quantity x price) would just redraw the price
    if (r.conflicted) point.color = CONFLICT_COLOR;
    return point;
  }, oiPlotMs);
}
// Funding is an instant per settlement, not a grid. Two rows in the same second (a Regular and a Special one) cannot
// share a time on the chart: the first is plotted, the others are counted in `special` and said so under the pane.
function fundingPoints(rows, colors) {
  const points = [];
  let special = 0, lastSecond = null;
  for (const r of rows) {
    const second = Math.floor(r.t / 1000);
    if (second === lastSecond) { special += 1; continue; }
    lastSecond = second;
    points.push({ time: second, value: Number(r.rate) * 100, color: r.conflicted ? CONFLICT_COLOR : r.rate.startsWith("-") ? colors.down : colors.up });
  }
  return { points, special };
}

// The decimals a price axis needs, from the exact strings of the rows (at most 8): a chart never rounds a stored price
// to two places because that is the library's default.
function decimalPlaces(texts) {
  let most = 0;
  for (const text of texts) {
    const at = String(text).indexOf(".");
    if (at >= 0) most = Math.max(most, String(text).length - at - 1);
  }
  return Math.min(8, most);
}
// "#rrggbb" and an alpha as an rgba() colour (the library wants colours, the page keeps variables).
function withAlpha(hex, alpha) {
  const match = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(String(hex).trim());
  if (!match) return String(hex);
  return `rgba(${parseInt(match[1], 16)}, ${parseInt(match[2], 16)}, ${parseInt(match[3], 16)}, ${alpha})`;
}

// --- DOM below ---

// The state of the view. `detailOverview` is called by the inline script after every overview render (with the data)
// and after every overview failure (with null), so the detail never shows more than the overview says.
let detailRoute = { view: "overview" }, detailRange = "24h", detailOverviewFailed = false;
let detailDoc = null, detailDocKey = null, detailError = null, detailFetchedAt = null, detailTimer = null, detailController = null;
const detailLoader = createDetailLoader(detailFetchJson, detailApply);

function detailKey() { return JSON.stringify([detailRoute.symbol, detailRoute.venue, detailRoute.timeframe, detailRange]); }

async function detailFetchJson(key) {
  const [symbol, venue, timeframe, range] = JSON.parse(key);
  if (detailController) detailController.abort();
  const controller = new AbortController();
  detailController = controller;
  const giveUp = setTimeout(() => controller.abort(), 90000);  // a hung tool or tunnel ends in an error line, never in endless placeholders
  try {
    const query = new URLSearchParams({ symbol, venue, timeframe, range });
    const response = await fetch("/detail.json?" + query.toString(), { cache: "no-store", signal: controller.signal });
    return { status: response.status, body: await response.json() };
  } finally {
    clearTimeout(giveUp);
  }
}

function detailErrorText(body) {
  const error = body && body.api_error;
  if (!error) return "The tool answered with something that is not a detail.";
  if (error.code === "unsupported_series") return "Charts are drawn only for binance-usdm 5m series.";
  if (error.code === "unknown_series") return "The API does not list this series any more.";
  if (error.code === "busy") return "The tool is busy with another detail; it retries in a minute.";
  return `The detail could not be loaded: ${error.code}${error.detail ? " (" + error.detail + ")" : ""}`;
}

function detailApply(key, result) {
  detailDoc = null; detailDocKey = null; detailError = null;
  const [symbol, venue, timeframe, range] = JSON.parse(key);
  if (result.error) detailError = /AbortError/.test(result.error) ? TEXT.timeout : TEXT.down + result.error;
  else if (result.status !== 200) detailError = detailErrorText(result.body);
  else if (!result.body || result.body.symbol !== symbol || result.body.venue !== venue || result.body.timeframe !== timeframe || result.body.range !== range) {
    detailError = "The tool answered for another series.";  // never draw a detail under the wrong name
  } else { detailDoc = result.body; detailDocKey = key; detailFetchedAt = Date.now(); }
  detailRender();
  detailSchedule();
}

// Re-fetch every 60 s, only while a symbol is shown and the tab is visible.
function detailSchedule() {
  clearTimeout(detailTimer);
  detailTimer = null;
  if (detailRoute.view !== "symbol" || document.visibilityState !== "visible") return;
  detailTimer = setTimeout(() => { if (detailRoute.view === "symbol") detailLoader.load(detailKey()); }, 60000);
}

function detailOverview(data) {
  detailOverviewFailed = data === null;
  detailRender();
}

function detailRouteChanged() {
  const next = parseRoute(location.hash);
  const changed = JSON.stringify(next) !== JSON.stringify(detailRoute);
  detailRoute = next;
  if (changed) {
    detailLoader.cancel();
    detailDoc = null; detailDocKey = null; detailError = null;
    clearTimeout(detailTimer);
    detailDestroyChart();
  }
  detailRender();
  if (changed && next.view === "symbol") window.scrollTo(0, 0);
}

function detailSetRange(range) {
  if (range === detailRange) return;
  detailRange = range;
  detailLoader.cancel();
  detailDoc = null; detailDocKey = null; detailError = null;
  detailRender();
}

function detailFill(box, ...children) { box.replaceChildren(...children); }
function detailTag(state) { return el("span", { class: `dtag s-${state}` }, stateText(state)); }
function detailFacts(pairs) {
  const list = el("dl", { class: "facts" });
  for (const [name, value] of pairs) list.append(el("dt", {}, name), el("dd", {}, value));
  return list;
}

function detailRender() {
  const state = detailViewState(detailRoute, lastData, { overviewFailed: detailOverviewFailed });
  const open = state.show !== "overview";
  $("overview-view").hidden = open;
  $("detail-view").hidden = !open;
  document.title = open && detailRoute.symbol ? `${detailRoute.symbol} · ${TEXT.title}` : TEXT.title;
  if (!open) return;
  const box = $("detail-state"), body = $("detail-body");
  if (state.show !== "detail") {
    const message = { loading: DETAIL_TEXT.loading, invalid: DETAIL_TEXT.invalid, notfound: DETAIL_TEXT.notFound, overview_error: DETAIL_TEXT.overviewError }[state.show];
    box.className = state.show === "loading" ? "" : "bad";
    detailFill(box, message, state.show === "loading" ? "" : " ", state.show === "loading" ? "" : el("a", { href: "#" }, `← ${DETAIL_TEXT.back}`));
    body.hidden = true;
    return;
  }
  detailDrawBody(state.row);
  const key = detailKey();
  if (!detailLoader.started(key)) detailLoader.load(key);
}

function detailDrawBody(row) {
  const data = lastData, status = oiStatus(data), fstatus = fundingStatus(data), finfo = data.funding_rate || null;
  const key = detailKey();
  const doc = detailDocKey === key ? detailDoc : null;
  $("detail-body").hidden = false;
  const box = $("detail-state");
  box.className = detailError ? "bad" : "";
  box.replaceChildren(detailError || "");

  const worst = worstState(row, status, fstatus);
  const near = neighbours(data.symbols, detailRoute);
  const link = (target, text) => (target ? el("a", { class: "dlink", href: rowHref(target), "aria-label": `${text}: ${target.symbol}` }, text) : el("span", { class: "dlink off" }, text));
  detailFill($("detail-head"),
    el("a", { class: "dlink", href: "#" }, `← ${DETAIL_TEXT.back}`),
    el("h1", {}, row.symbol), detailTag(worst), el("span", { class: "chip" }, row.venue || "–"),
    el("span", { class: "spacer" }), link(near.prev, `← ${DETAIL_TEXT.previous}`), link(near.next, `${DETAIL_TEXT.next} →`));

  const closeAge = lastCloseAgeMs(row);
  const coverage = historyCoverage(row, data.window);
  const reasons = reasonLines(row, status, fstatus, finfo);
  detailFill($("detail-status"), el("h3", {}, DETAIL_TEXT.sections.status),
    detailFacts([
      [DETAIL_TEXT.status.state, detailTag(worst)],
      [DETAIL_TEXT.status.lastUpdate, row.newest_open_time_ms == null ? "–" : `${fmt(row.newest_open_time_ms + DETAIL_TF)} (${ageText(closeAge)} before the API clock)`],
      [DETAIL_TEXT.status.source, row.venue || "–"],
      [DETAIL_TEXT.status.timeframe, row.timeframe || "–"],
      [DETAIL_TEXT.status.history, row.oldest_open_time_ms == null ? "–" : `${fmt(row.oldest_open_time_ms)}${coverage ? ` (${coverage.text} ${DETAIL_TEXT.coverageOf})` : ""}`],
    ]),
    ...(reasons.length ? [el("div", { class: "note reasons" }, ...reasons.map((line) => el("p", {}, line)))] : []));

  const cards = doc ? kpiModel(doc) : null;
  for (const [id, title, card] of [["kpi-candle", DETAIL_TEXT.sections.candles, cards && cards.candle], ["kpi-oi", DETAIL_TEXT.sections.openInterest, cards && cards.openInterest], ["kpi-funding", DETAIL_TEXT.sections.funding, cards && cards.funding]]) {
    detailFill($(id), el("h3", {}, title),
      el("div", { class: `big${card && card.head ? "" : " none"}` }, card ? (card.head || "–") : (detailError ? "–" : "…")),
      ...(card ? card.lines.map((line) => el("div", { class: "line" }, line)) : []));
  }

  const quality = qualityRows(row, status, fstatus, finfo);
  detailFill($("detail-quality"), el("h3", {}, DETAIL_TEXT.sections.quality),
    ...quality.map((q) => el("div", { class: "qrow" },
      el("span", { class: "lbl" }, DETAIL_TEXT.quality[q.key]),
      q.state === null ? el("span", { class: "dash" }, "–") : detailTag(q.state),
      el("span", { class: "note", title: q.lines.join("\n") }, q.lines.join(" · ")))));

  const oiCoverage = oiStateOf(row, status) === null ? null : openInterestCoverage(row.open_interest, data.open_interest && data.open_interest.window);
  const bar = (label, share, note) => {
    const track = el("span", { class: "track" });
    if (share) { const fill = el("b"); fill.style.width = `${share.present / share.span * 100}%`; track.append(fill); }
    return [el("div", { class: "bar" }, el("span", { class: "lbl" }, label), track, el("span", { class: "v" }, share ? share.text : "–")), ...(note ? [el("div", { class: "note" }, note)] : [])];
  };
  const fundingState = fundingStateOf(row, fstatus);
  detailFill($("detail-coverage"), el("h3", {}, `${DETAIL_TEXT.sections.coverage} (${DETAIL_TEXT.coverageOf})`),
    ...bar(TEXT.rows.candles, coverage, coverage ? "" : "cannot be computed from the overview's numbers"),
    ...(oiStateOf(row, status) === null ? [] : bar(TEXT.rows.openInterest, oiCoverage, oiCoverage ? "" : "cannot be computed from the overview's numbers")),
    ...(fundingState === null ? [] : [el("div", { class: "bar" }, el("span", { class: "lbl" }, TEXT.rows.funding), detailTag(fundingState)), el("div", { class: "note" }, DETAIL_TEXT.coverageFunding)]));

  detailDrawCharts(doc);
  detailAge();
}

function detailAge() {
  const line = $("detail-fetched");
  if (detailRoute.view !== "symbol" || detailDoc === null || detailFetchedAt === null) { line.textContent = ""; return; }
  const seconds = Math.round((Date.now() - detailFetchedAt) / 1000);  // display only, never a state
  line.textContent = `API clock ${fmt(detailDoc.as_of_ms)} · ${DETAIL_TEXT.fetched(seconds)}`;
}

let detailChart = null, detailDrawn = null, detailChartState = null;

function detailVariable(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
function detailDestroyChart() {
  if (detailChart) { detailChart.remove(); detailChart = null; }
  detailDrawn = null; detailChartState = null;
}
function detailColors() {
  const colors = {
    up: detailVariable("--ok"), down: detailVariable("--bad"), accent: detailVariable("--accent"), line: detailVariable("--line"),
    text: detailVariable("--muted"), card: detailVariable("--card"),
  };
  colors.volUp = withAlpha(colors.up, 0.45);
  colors.volDown = withAlpha(colors.down, 0.45);
  return colors;
}

// What the three panes plot, and the rows behind each plotted time (for the tooltip).
function detailChartData(doc, models, colors) {
  const window_ = doc.window;
  const out = { lookups: { candles: new Map(), openInterest: new Map(), funding: new Map() }, places: [0, 0], special: 0 };
  if (models[0].draw) {
    const rows = doc.candles.rows;
    out.candles = candleBars(rows, window_, colors);
    out.volume = volumeBars(rows, window_, colors);
    out.places[0] = decimalPlaces(rows.flatMap((r) => [r.o, r.h, r.l, r.c]));
    for (const r of rows) out.lookups.candles.set(r.t / 1000, r);
  }
  if (models[1].draw) {
    const rows = doc.open_interest.rows;
    out.area = oiPoints(rows, window_);
    out.places[1] = Math.min(3, decimalPlaces(rows.map((r) => r.oi)));
    for (const r of rows) out.lookups.openInterest.set(oiPlotMs(r.t) / 1000, r);
  }
  if (models[2].draw) {
    const rows = doc.funding_rate.rows;
    const funding = fundingPoints(rows, colors);
    out.funding = funding.points;
    out.special = funding.special;
    for (const r of rows) out.lookups.funding.set(Math.floor(r.t / 1000), r);
  }
  return out;
}

// A new document of the same series and range changes the data only, so the user's zoom and pan survive the poll.
function detailFillChart(state, data) {
  if (data.candles) { state.series.candles.setData(data.candles); state.series.volume.setData(data.volume); }
  if (data.area) state.series.area.setData(data.area);
  if (data.funding) state.series.funding.setData(data.funding);
  state.lookups = data.lookups;
}

function detailBuildChart(box, doc, models, colors, data, layout) {
  const LW = LightweightCharts;
  const chart = LW.createChart(box, {
    autoSize: true,
    layout: { background: { type: "solid", color: colors.card }, textColor: colors.text, panes: { separatorColor: colors.line } },
    grid: { vertLines: { color: colors.line }, horzLines: { color: colors.line } },
    rightPriceScale: { borderColor: colors.line },
    timeScale: { borderColor: colors.line, timeVisible: true, secondsVisible: false },  // the library shows UTC, like the whole page
  });
  detailChart = chart;
  const state = { layout, series: {}, lookups: data.lookups };
  const shown = [];  // stretch factor of each pane that is drawn, in pane order
  let pane = 0;
  if (models[0].draw) {
    const places = data.places[0];
    state.series.candles = chart.addSeries(LW.CandlestickSeries, {
      upColor: colors.up, downColor: colors.down, borderUpColor: colors.up, borderDownColor: colors.down, wickUpColor: colors.up, wickDownColor: colors.down,
      priceFormat: { type: "price", precision: places, minMove: Math.pow(10, -places) },
    }, pane);
    state.series.volume = chart.addSeries(LW.HistogramSeries, { priceScaleId: "", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false }, pane);
    state.series.volume.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    shown.push(0.55);
    pane += 1;
  }
  if (models[1].draw) {
    state.series.area = chart.addSeries(LW.AreaSeries, {
      lineColor: colors.accent, topColor: withAlpha(colors.accent, 0.35), bottomColor: withAlpha(colors.accent, 0.02),
      priceFormat: { type: "price", precision: data.places[1], minMove: Math.pow(10, -data.places[1]) },
    }, pane);
    shown.push(0.25);
    pane += 1;
  }
  if (models[2].draw) {
    state.series.funding = chart.addSeries(LW.HistogramSeries, {
      priceFormat: { type: "custom", formatter: (v) => `${v.toFixed(4)} %`, minMove: 0.0001 }, lastValueVisible: false, priceLineVisible: false,
    }, pane);
    shown.push(0.2);
  }
  detailFillChart(state, data);
  const total = shown.reduce((sum, factor) => sum + factor, 0);
  const paneApis = chart.panes();
  shown.forEach((factor, i) => { if (paneApis[i]) paneApis[i].setStretchFactor(factor / total); });
  try { chart.timeScale().setVisibleRange({ from: doc.window.start / 1000, to: doc.window.end / 1000 }); } catch (error) { /* no data to show: the notes under the chart say why */ }

  // The exact stored strings of the hovered slot; a slot without a stored row says so (a hole is never filled in).
  const tip = el("div", { class: "ctip", hidden: "" });
  box.append(tip);
  chart.subscribeCrosshairMove((param) => {
    if (!param.point || param.time === undefined) { tip.hidden = true; return; }
    const lines = [];
    const onGrid = (param.time * 1000) % DETAIL_TF === 0;  // an instant between slots (a settlement) is not a missing slot
    const candle = state.lookups.candles.get(param.time), interest = state.lookups.openInterest.get(param.time), settlement = state.lookups.funding.get(param.time);
    if (models[0].draw && (candle || onGrid)) lines.push(...(candle ? tooltipLines("candles", candle) : [fmt(param.time * 1000), "no candle stored for this slot"]));
    if (models[1].draw && (interest || onGrid)) lines.push(...(interest ? tooltipLines("openInterest", interest) : ["no open interest row for this slot"]));
    if (settlement) lines.push(...tooltipLines("funding", settlement));
    if (lines.length === 0) { tip.hidden = true; return; }
    tip.replaceChildren(...lines.map((line) => el("div", {}, line)));
    tip.hidden = false;
    const left = Math.min(param.point.x + 14, Math.max(0, box.clientWidth - tip.offsetWidth - 70));
    tip.style.left = `${left}px`;
    tip.style.top = `${Math.min(param.point.y + 14, Math.max(0, box.clientHeight - tip.offsetHeight))}px`;
  });
  return state;
}

// Three panes on one time axis: candles (with volume), open interest, funding. A part that is not ok draws nothing
// and says why under the chart.
function detailDrawCharts(doc) {
  const box = $("detail-charts"), note = $("detail-chart-note");
  if (doc === detailDrawn && doc !== null) return;
  note.replaceChildren();
  if (doc === null) { detailDestroyChart(); box.replaceChildren(); return; }
  const parts = [["candles", doc.candles], ["openInterest", doc.open_interest], ["funding", doc.funding_rate]];
  const models = parts.map(([, part]) => paneModel(part));
  parts.forEach(([name], i) => { if (!models[i].draw) note.append(el("p", {}, `${DETAIL_TEXT.panes[name]}: ${models[i].text}`)); });
  if (typeof LightweightCharts === "undefined") { detailDestroyChart(); box.replaceChildren(); note.append(el("p", { class: "notice" }, DETAIL_TEXT.chartsFailed)); return; }
  if (!models.some((m) => m.draw)) { detailDestroyChart(); box.replaceChildren(); detailDrawn = doc; return; }
  const data = detailChartData(doc, models, detailColors());
  const layout = JSON.stringify([detailDocKey, models.map((m) => m.draw), data.places]);
  if (detailChart && detailChartState && detailChartState.layout === layout) {
    detailFillChart(detailChartState, data);
  } else {
    detailDestroyChart();
    box.replaceChildren();
    detailChartState = detailBuildChart(box, doc, models, detailColors(), data, layout);
  }
  if (data.special > 0) note.append(el("p", {}, DETAIL_TEXT.specialNotPlotted(data.special)));
  detailDrawn = doc;
}

function detailInit() {
  detailFill($("detail-charts-title"), DETAIL_TEXT.sections.charts);
  const ranges = $("detail-ranges");
  for (const range of DETAIL_RANGES) {
    const button = el("button", { type: "button", "aria-pressed": String(range === detailRange) }, DETAIL_TEXT.ranges[range]);
    button.addEventListener("click", () => {
      detailSetRange(range);
      for (const other of ranges.children) other.setAttribute("aria-pressed", String(other === button));
    });
    ranges.append(button);
  }
  const link = el("a", { class: "dlink", rel: "noopener" }, "tradingview.com");
  link.setAttribute("href", DETAIL_TEXT.noticeLink);
  detailFill($("detail-notice"), `${DETAIL_TEXT.notice} · `, link);
  window.addEventListener("hashchange", detailRouteChanged);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible" || detailRoute.view !== "symbol") return;
    // after a long time in a hidden tab the cards would show old numbers for a minute: load at once
    if (lastData !== null && (detailFetchedAt === null || Date.now() - detailFetchedAt >= 60000)) detailLoader.load(detailKey());
    else detailSchedule();
  });
  setInterval(detailAge, 1000);
  // the overview may have failed before this script was evaluated: its hook was not there to tell us
  detailOverviewFailed = lastData === null && document.body.dataset.phase === "error";
  detailRouteChanged();
}

detailInit();
