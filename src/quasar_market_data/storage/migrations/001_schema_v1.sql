-- Schema version 1 (ADR-0002 P-2, P-4, P-5, P-6, P-8). Do not edit once released: add a new migration.

CREATE TABLE schema_migrations (
  version         INTEGER PRIMARY KEY,
  name            TEXT    NOT NULL,
  checksum_sha256 TEXT    NOT NULL,
  applied_at_ms   INTEGER NOT NULL
) STRICT;

CREATE TABLE candles (
  venue          TEXT    NOT NULL CHECK (length(venue) BETWEEN 1 AND 32),
  symbol         TEXT    NOT NULL CHECK (length(symbol) BETWEEN 1 AND 64),
  timeframe      TEXT    NOT NULL CHECK (length(timeframe) BETWEEN 2 AND 3),
  open_time_ms   INTEGER NOT NULL CHECK (open_time_ms BETWEEN 946684800000 AND 4102444800000),
  open           TEXT    NOT NULL CHECK (typeof(open) = 'text' AND length(open) BETWEEN 1 AND 97 AND open NOT GLOB '*[^0-9.]*' AND open NOT GLOB '.*' AND open NOT GLOB '*.' AND open NOT GLOB '*.*.*' AND open NOT GLOB '*.*0' AND open NOT GLOB '0[0-9]*'),
  high           TEXT    NOT NULL CHECK (typeof(high) = 'text' AND length(high) BETWEEN 1 AND 97 AND high NOT GLOB '*[^0-9.]*' AND high NOT GLOB '.*' AND high NOT GLOB '*.' AND high NOT GLOB '*.*.*' AND high NOT GLOB '*.*0' AND high NOT GLOB '0[0-9]*'),
  low            TEXT    NOT NULL CHECK (typeof(low) = 'text' AND length(low) BETWEEN 1 AND 97 AND low NOT GLOB '*[^0-9.]*' AND low NOT GLOB '.*' AND low NOT GLOB '*.' AND low NOT GLOB '*.*.*' AND low NOT GLOB '*.*0' AND low NOT GLOB '0[0-9]*'),
  close          TEXT    NOT NULL CHECK (typeof(close) = 'text' AND length(close) BETWEEN 1 AND 97 AND close NOT GLOB '*[^0-9.]*' AND close NOT GLOB '.*' AND close NOT GLOB '*.' AND close NOT GLOB '*.*.*' AND close NOT GLOB '*.*0' AND close NOT GLOB '0[0-9]*'),
  volume         TEXT    NOT NULL CHECK (typeof(volume) = 'text' AND length(volume) BETWEEN 1 AND 97 AND volume NOT GLOB '*[^0-9.]*' AND volume NOT GLOB '.*' AND volume NOT GLOB '*.' AND volume NOT GLOB '*.*.*' AND volume NOT GLOB '*.*0' AND volume NOT GLOB '0[0-9]*'),
  turnover       TEXT             CHECK (turnover IS NULL OR (typeof(turnover) = 'text' AND length(turnover) BETWEEN 1 AND 97 AND turnover NOT GLOB '*[^0-9.]*' AND turnover NOT GLOB '.*' AND turnover NOT GLOB '*.' AND turnover NOT GLOB '*.*.*' AND turnover NOT GLOB '*.*0' AND turnover NOT GLOB '0[0-9]*')),
  ingested_at_ms INTEGER NOT NULL,
  PRIMARY KEY (venue, symbol, timeframe, open_time_ms)
) STRICT, WITHOUT ROWID;

CREATE TRIGGER candles_no_update BEFORE UPDATE ON candles
  BEGIN SELECT RAISE(ABORT, 'candles are immutable'); END;
CREATE TRIGGER candles_no_delete BEFORE DELETE ON candles
  BEGIN SELECT RAISE(ABORT, 'candles are immutable'); END;

CREATE TABLE candle_conflicts (
  conflict_id      INTEGER PRIMARY KEY,
  venue            TEXT    NOT NULL,
  symbol           TEXT    NOT NULL,
  timeframe        TEXT    NOT NULL,
  open_time_ms     INTEGER NOT NULL,
  open           TEXT    NOT NULL CHECK (typeof(open) = 'text' AND length(open) BETWEEN 1 AND 97 AND open NOT GLOB '*[^0-9.]*' AND open NOT GLOB '.*' AND open NOT GLOB '*.' AND open NOT GLOB '*.*.*' AND open NOT GLOB '*.*0' AND open NOT GLOB '0[0-9]*'),
  high           TEXT    NOT NULL CHECK (typeof(high) = 'text' AND length(high) BETWEEN 1 AND 97 AND high NOT GLOB '*[^0-9.]*' AND high NOT GLOB '.*' AND high NOT GLOB '*.' AND high NOT GLOB '*.*.*' AND high NOT GLOB '*.*0' AND high NOT GLOB '0[0-9]*'),
  low            TEXT    NOT NULL CHECK (typeof(low) = 'text' AND length(low) BETWEEN 1 AND 97 AND low NOT GLOB '*[^0-9.]*' AND low NOT GLOB '.*' AND low NOT GLOB '*.' AND low NOT GLOB '*.*.*' AND low NOT GLOB '*.*0' AND low NOT GLOB '0[0-9]*'),
  close          TEXT    NOT NULL CHECK (typeof(close) = 'text' AND length(close) BETWEEN 1 AND 97 AND close NOT GLOB '*[^0-9.]*' AND close NOT GLOB '.*' AND close NOT GLOB '*.' AND close NOT GLOB '*.*.*' AND close NOT GLOB '*.*0' AND close NOT GLOB '0[0-9]*'),
  volume         TEXT    NOT NULL CHECK (typeof(volume) = 'text' AND length(volume) BETWEEN 1 AND 97 AND volume NOT GLOB '*[^0-9.]*' AND volume NOT GLOB '.*' AND volume NOT GLOB '*.' AND volume NOT GLOB '*.*.*' AND volume NOT GLOB '*.*0' AND volume NOT GLOB '0[0-9]*'),
  turnover       TEXT             CHECK (turnover IS NULL OR (typeof(turnover) = 'text' AND length(turnover) BETWEEN 1 AND 97 AND turnover NOT GLOB '*[^0-9.]*' AND turnover NOT GLOB '.*' AND turnover NOT GLOB '*.' AND turnover NOT GLOB '*.*.*' AND turnover NOT GLOB '*.*0' AND turnover NOT GLOB '0[0-9]*')),
  first_seen_at_ms INTEGER NOT NULL
) STRICT;

CREATE UNIQUE INDEX candle_conflicts_observation ON candle_conflicts
  (venue, symbol, timeframe, open_time_ms, open, high, low, close, volume, coalesce(turnover, ''));
CREATE INDEX candle_conflicts_slot ON candle_conflicts (venue, symbol, timeframe, open_time_ms);

CREATE TRIGGER candle_conflicts_no_update BEFORE UPDATE ON candle_conflicts
  BEGIN SELECT RAISE(ABORT, 'candle_conflicts are immutable'); END;
CREATE TRIGGER candle_conflicts_no_delete BEFORE DELETE ON candle_conflicts
  BEGIN SELECT RAISE(ABORT, 'candle_conflicts are immutable'); END;

CREATE TABLE unavailable_candles (
  venue          TEXT    NOT NULL,
  symbol         TEXT    NOT NULL,
  timeframe      TEXT    NOT NULL,
  open_time_ms   INTEGER NOT NULL CHECK (open_time_ms BETWEEN 946684800000 AND 4102444800000),
  reason         TEXT    NOT NULL CHECK (reason IN ('EXCHANGE_NO_DATA')),
  recorded_at_ms INTEGER NOT NULL,
  PRIMARY KEY (venue, symbol, timeframe, open_time_ms)
) STRICT, WITHOUT ROWID;

CREATE TRIGGER unavailable_candles_no_update BEFORE UPDATE ON unavailable_candles
  BEGIN SELECT RAISE(ABORT, 'unavailable_candles are immutable'); END;
CREATE TRIGGER unavailable_candles_no_delete BEFORE DELETE ON unavailable_candles
  BEGIN SELECT RAISE(ABORT, 'unavailable_candles are immutable'); END;
