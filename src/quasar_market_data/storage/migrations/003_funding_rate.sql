-- Schema version 3: funding rate history (ADR-0004 P-6). Additive; no v1 or v2 object is touched. Do not edit once released.
-- funding_rate is a SIGNED canonical decimal: an optional leading '-' and then the exact <decimal-shape> of
-- 001_schema_v1.sql; '-0' and a bare '-' are refused. mark_price is NULL or the non-negative <decimal-shape>.

CREATE TABLE funding_rates (
  venue           TEXT    NOT NULL CHECK (length(venue) BETWEEN 1 AND 32),
  symbol          TEXT    NOT NULL CHECK (length(symbol) BETWEEN 1 AND 64),
  funding_time_ms INTEGER NOT NULL CHECK (funding_time_ms BETWEEN 946684800000 AND 4102444800000),
  rate_type       TEXT    NOT NULL CHECK (rate_type IN ('Regular', 'Special')),
  funding_rate    TEXT    NOT NULL CHECK (typeof(funding_rate) = 'text' AND funding_rate <> '-0' AND length(CASE WHEN substr(funding_rate, 1, 1) = '-' THEN substr(funding_rate, 2) ELSE funding_rate END) BETWEEN 1 AND 97 AND CASE WHEN substr(funding_rate, 1, 1) = '-' THEN substr(funding_rate, 2) ELSE funding_rate END NOT GLOB '*[^0-9.]*' AND CASE WHEN substr(funding_rate, 1, 1) = '-' THEN substr(funding_rate, 2) ELSE funding_rate END NOT GLOB '.*' AND CASE WHEN substr(funding_rate, 1, 1) = '-' THEN substr(funding_rate, 2) ELSE funding_rate END NOT GLOB '*.' AND CASE WHEN substr(funding_rate, 1, 1) = '-' THEN substr(funding_rate, 2) ELSE funding_rate END NOT GLOB '*.*.*' AND CASE WHEN substr(funding_rate, 1, 1) = '-' THEN substr(funding_rate, 2) ELSE funding_rate END NOT GLOB '*.*0' AND CASE WHEN substr(funding_rate, 1, 1) = '-' THEN substr(funding_rate, 2) ELSE funding_rate END NOT GLOB '0[0-9]*'),
  mark_price      TEXT             CHECK (mark_price IS NULL OR (typeof(mark_price) = 'text' AND length(mark_price) BETWEEN 1 AND 97 AND mark_price NOT GLOB '*[^0-9.]*' AND mark_price NOT GLOB '.*' AND mark_price NOT GLOB '*.' AND mark_price NOT GLOB '*.*.*' AND mark_price NOT GLOB '*.*0' AND mark_price NOT GLOB '0[0-9]*')),
  ingested_at_ms  INTEGER NOT NULL,
  PRIMARY KEY (venue, symbol, funding_time_ms, rate_type)
) STRICT, WITHOUT ROWID;

CREATE TRIGGER funding_rates_no_update BEFORE UPDATE ON funding_rates
  BEGIN SELECT RAISE(ABORT, 'funding_rates is immutable'); END;
CREATE TRIGGER funding_rates_no_delete BEFORE DELETE ON funding_rates
  BEGIN SELECT RAISE(ABORT, 'funding_rates is immutable'); END;

CREATE TABLE funding_rate_conflicts (
  conflict_id      INTEGER PRIMARY KEY,
  venue            TEXT    NOT NULL,
  symbol           TEXT    NOT NULL,
  funding_time_ms  INTEGER NOT NULL,
  rate_type        TEXT    NOT NULL,
  funding_rate     TEXT    NOT NULL CHECK (typeof(funding_rate) = 'text' AND funding_rate <> '-0' AND length(CASE WHEN substr(funding_rate, 1, 1) = '-' THEN substr(funding_rate, 2) ELSE funding_rate END) BETWEEN 1 AND 97 AND CASE WHEN substr(funding_rate, 1, 1) = '-' THEN substr(funding_rate, 2) ELSE funding_rate END NOT GLOB '*[^0-9.]*' AND CASE WHEN substr(funding_rate, 1, 1) = '-' THEN substr(funding_rate, 2) ELSE funding_rate END NOT GLOB '.*' AND CASE WHEN substr(funding_rate, 1, 1) = '-' THEN substr(funding_rate, 2) ELSE funding_rate END NOT GLOB '*.' AND CASE WHEN substr(funding_rate, 1, 1) = '-' THEN substr(funding_rate, 2) ELSE funding_rate END NOT GLOB '*.*.*' AND CASE WHEN substr(funding_rate, 1, 1) = '-' THEN substr(funding_rate, 2) ELSE funding_rate END NOT GLOB '*.*0' AND CASE WHEN substr(funding_rate, 1, 1) = '-' THEN substr(funding_rate, 2) ELSE funding_rate END NOT GLOB '0[0-9]*'),
  mark_price       TEXT             CHECK (mark_price IS NULL OR (typeof(mark_price) = 'text' AND length(mark_price) BETWEEN 1 AND 97 AND mark_price NOT GLOB '*[^0-9.]*' AND mark_price NOT GLOB '.*' AND mark_price NOT GLOB '*.' AND mark_price NOT GLOB '*.*.*' AND mark_price NOT GLOB '*.*0' AND mark_price NOT GLOB '0[0-9]*')),
  first_seen_at_ms INTEGER NOT NULL
) STRICT;

-- COALESCE: SQLite treats NULLs as distinct in a UNIQUE index; '' is not a valid canonical decimal.
CREATE UNIQUE INDEX funding_rate_conflicts_observation ON funding_rate_conflicts
  (venue, symbol, funding_time_ms, rate_type, funding_rate, COALESCE(mark_price, ''));
CREATE INDEX funding_rate_conflicts_event ON funding_rate_conflicts (venue, symbol, funding_time_ms);

CREATE TRIGGER funding_rate_conflicts_no_update BEFORE UPDATE ON funding_rate_conflicts
  BEGIN SELECT RAISE(ABORT, 'funding_rate_conflicts are immutable'); END;
CREATE TRIGGER funding_rate_conflicts_no_delete BEFORE DELETE ON funding_rate_conflicts
  BEGIN SELECT RAISE(ABORT, 'funding_rate_conflicts are immutable'); END;

-- The one mutable table of this family: progress, not market data. It may only grow, and only the single
-- merge statement of the funding writer grows it (adjacency is not enforceable by a trigger, ADR-0004 P-6, P-7).
CREATE TABLE funding_rate_verified (
  venue          TEXT    NOT NULL CHECK (length(venue) BETWEEN 1 AND 32),
  symbol         TEXT    NOT NULL CHECK (length(symbol) BETWEEN 1 AND 64),
  from_ms        INTEGER NOT NULL CHECK (from_ms    BETWEEN 946684800000 AND 4102444800000),
  through_ms     INTEGER NOT NULL CHECK (through_ms BETWEEN 946684800000 AND 4102444800000),
  verified_at_ms INTEGER NOT NULL,
  CHECK (through_ms >= from_ms),
  PRIMARY KEY (venue, symbol)
) STRICT, WITHOUT ROWID;

CREATE TRIGGER funding_rate_verified_only_grows BEFORE UPDATE ON funding_rate_verified
  WHEN NEW.venue <> OLD.venue OR NEW.symbol <> OLD.symbol
    OR NEW.from_ms > OLD.from_ms OR NEW.through_ms < OLD.through_ms
    OR NEW.verified_at_ms < OLD.verified_at_ms
  BEGIN SELECT RAISE(ABORT, 'funding_rate_verified only grows'); END;
CREATE TRIGGER funding_rate_verified_no_delete BEFORE DELETE ON funding_rate_verified
  BEGIN SELECT RAISE(ABORT, 'funding_rate_verified is not deletable'); END;
