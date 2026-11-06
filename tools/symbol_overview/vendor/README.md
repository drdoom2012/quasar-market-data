# Vendored: TradingView Lightweight Charts

- **Library:** `lightweight-charts`, version **5.2.1**, license Apache-2.0 (`LICENSE`, `NOTICE`).
- **Source:** `https://registry.npmjs.org/lightweight-charts/-/lightweight-charts-5.2.1.tgz` (tarball SHA-256 `64310292298df4dd527494865e5d91d447f8bb397b68de3912c2a6465025ee5e`), file `package/dist/lightweight-charts.standalone.production.js`, copied unchanged as `lightweight-charts.js`.
- **SHA-256 of `lightweight-charts.js`:** `e21cc5caa0226ef30bd8549c50b9ef926615f2a4ee6b4e486353477a55f598cf`
- `NOTICE` is not in the npm tarball; it is the file of the `v5.2.1` tag of `https://github.com/tradingview/lightweight-charts`.
- **Obligations (Apache-2.0 with the notice requirement):** ship `LICENSE` and `NOTICE`; show the NOTICE text and a link to `https://www.tradingview.com/` on the page that uses the library. The detail view does both (a footer made by `detail.js`, and the chart option `attributionLogo`, which stays on).
- **Why vendored:** the page must work through the SSH tunnel and in a container with no route out, and no unpinned third-party code may run (spec `docs/superpowers/specs/2026-11-06-symbol-detail-design.md`, 5.4). Served by the tool from a fixed route.
- **Bump only deliberately:** replace the file, update the version and both hashes here and `VENDOR_SHA256` in `tests/tools/symbol_overview/test_vendor.py`.
