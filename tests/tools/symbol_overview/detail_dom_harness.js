"use strict";
// A minimal DOM, fake timers, a controllable fetch and a fake chart library, so that the DOM part of detail.js runs
// under Node in tests/tools/symbol_overview/test_detail_dom.py. Not a browser: it checks the flows (what is drawn,
// what is fetched, what is dropped), not colours or layout.
module.exports = function makeEnv(options) {
  const env = { timers: [], fetches: [], charts: [], hash: options.hash || "", listeners: {}, docListeners: {}, aborted: 0 };
  const nodes = new Map();
  const textNode = (text) => ({ textContent: String(text) });
  function makeNode() {
    const node = {
      children: [], attrs: {}, hidden: false, className: "", style: {}, _text: "", listeners: {}, dataset: {},
      get textContent() { return node._text + node.children.map((c) => c.textContent).join(""); },
      set textContent(value) { node._text = String(value); node.children = []; },
      append(...items) { for (const item of items) node.children.push(typeof item === "string" ? textNode(item) : item); },
      replaceChildren(...items) { node._text = ""; node.children = []; node.append(...items); },
      setAttribute(name, value) { node.attrs[name] = String(value); if (name === "hidden") node.hidden = true; },
      addEventListener(type, fn) { (node.listeners[type] = node.listeners[type] || []).push(fn); },
      clientWidth: 1000, clientHeight: 560, offsetWidth: 100, offsetHeight: 40,
    };
    return node;
  }
  const document = {
    getElementById(id) {
      if (!nodes.has(id)) { const node = makeNode(); node.hidden = id === "detail-view" || id === "detail-body"; nodes.set(id, node); }
      return nodes.get(id);
    },
    createElement: makeNode,
    visibilityState: "visible",
    title: "",
    body: { dataset: { phase: options.phase || "ready" } },
    documentElement: {},
    addEventListener(type, fn) { (env.docListeners[type] = env.docListeners[type] || []).push(fn); },
  };
  const window = {
    addEventListener(type, fn) { (env.listeners[type] = env.listeners[type] || []).push(fn); },
    scrollTo() {},
  };
  const location = { get hash() { return env.hash; } };
  class AbortController {
    constructor() { this.signal = { aborted: false }; }
    abort() { this.signal.aborted = true; env.aborted += 1; }
  }
  const fakeFetch = (url, opts) => new Promise((resolve, reject) => { env.fetches.push({ url, opts, resolve, reject }); });
  const setTimeoutFake = (fn, ms) => { const id = env.timers.length + 1; env.timers.push({ id, fn, ms, live: true }); return id; };
  const clearTimeoutFake = (id) => { const timer = env.timers.find((t) => t.id === id); if (timer) timer.live = false; };
  const chartLibrary = {
    CandlestickSeries: "candlestick", HistogramSeries: "histogram", AreaSeries: "area",
    createChart() {
      const chart = {
        removed: false, series: [], visible: null,
        addSeries(definition, seriesOptions, pane) {
          const series = { definition, pane, data: null, sets: 0, setData(data) { series.data = data; series.sets += 1; }, priceScale() { return { applyOptions() {} }; } };
          chart.series.push(series);
          return series;
        },
        panes() { return [0, 1, 2].map(() => ({ setStretchFactor() {} })); },
        timeScale() { return { setVisibleRange(range) { chart.visible = range; } }; },
        subscribeCrosshairMove(fn) { chart.crosshair = fn; },
        remove() { chart.removed = true; },
      };
      env.charts.push(chart);
      return chart;
    },
  };
  env.fireHash = (hash) => { env.hash = hash; for (const fn of env.listeners.hashchange || []) fn(); };
  env.fireVisibility = (state) => { document.visibilityState = state; for (const fn of env.docListeners.visibilitychange || []) fn(); };
  env.respond = (index, status, body) => env.fetches[index].resolve({ status, json: async () => body });
  env.fail = (index, message) => env.fetches[index].reject(new Error(message));
  env.liveTimers = (ms) => env.timers.filter((t) => t.live && t.ms === ms);
  const globals = {
    env, document, window, location, AbortController, URLSearchParams, console,
    fetch: fakeFetch, setTimeout: setTimeoutFake, clearTimeout: clearTimeoutFake, setInterval: () => 0,
    getComputedStyle: () => ({ getPropertyValue: (name) => (name === "--accent" ? "#3b82f6" : "#22c55e") }),
  };
  if (options.charts !== false) globals.LightweightCharts = chartLibrary;
  return { env, globals };
};
