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
  panes: { candles: "Price (5m candles)", openInterest: "Open interest", funding: "Funding rate" },
  coverageOf: "of stored history",
  coverageFunding: "Funding has no time grid, so it has no percentage: its state is the one above.",
  noData: "no data in this range",
  nothingStored: "nothing stored yet",
  notEnabled: "not enabled in this deployment",
  noSettlement: "no settlement in the last 7 days",
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
    lines: [`open ${groupDecimal(last.o)} · high ${groupDecimal(last.h)} · low ${groupDecimal(last.l)}`, `candle opened ${fmt(last.t)}`,
      changeText(candles.change, "vs the previous candle")].concat(last.conflicted ? [DETAIL_TEXT.conflicted] : []),
  };
  const row = interest.status === "ok" && interest.rows.length ? interest.rows[interest.rows.length - 1] : null;
  const interestMessage = partMessage(interest, row !== null, interest.newest_ms);
  out.openInterest = row === null ? { status: interest.status, head: null, lines: [interestMessage] } : {
    status: "ok", head: groupDecimal(row.value) + (quote ? " " + quote : ""),
    lines: [`open interest ${groupDecimal(row.oi)}`, `period ended ${fmt(row.t)}`, changeText(interest.change, "over 1 h")].concat(row.conflicted ? [DETAIL_TEXT.conflicted] : []),
  };
  const latest = funding.status === "ok" ? funding.latest : null;
  const fundingMessage = funding.status === "ok" ? DETAIL_TEXT.noSettlement : partMessage(funding, false, null);
  out.funding = latest == null ? { status: funding.status, head: null, lines: [fundingMessage] } : {
    status: "ok", head: shiftPercent(latest.rate) + " %",
    lines: [`${latest.rate_type} settlement ${fmt(latest.t)}`, `rate ${latest.rate} (a fraction per settlement)`].concat(
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
  else if (kind === "openInterest") lines.push(`period ended ${fmt(row.t)}`, `value ${groupDecimal(row.value)}`, `open interest ${groupDecimal(row.oi)}`);
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
    const point = { time: oiPlotMs(r.t) / 1000, value: Number(r.value) };
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

// --- DOM below ---
