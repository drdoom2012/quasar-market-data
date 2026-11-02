-- Schema version 2: open interest (ADR-0003 P-6). Additive; no v1 object is touched. Do not edit once released.
-- The value checks are the exact <decimal-shape> of 001_schema_v1.sql (ADR-0002): non-negative canonical decimal text.

CREATE TABLE open_interest (
  venue               TEXT    NOT NULL CHECK (length(venue) BETWEEN 1 AND 32),
  symbol              TEXT    NOT NULL CHECK (length(symbol) BETWEEN 1 AND 64),
  period              TEXT    NOT NULL CHECK (length(period) BETWEEN 2 AND 3),
  time_ms             INTEGER NOT NULL CHECK (time_ms BETWEEN 946684800000 AND 4102444800000),
  open_interest       TEXT    NOT NULL CHECK (typeof(open_interest) = 'text' AND length(open_interest) BETWEEN 1 AND 97 AND open_interest NOT GLOB '*[^0-9.]*' AND open_interest NOT GLOB '.*' AND open_interest NOT GLOB '*.' AND open_interest NOT GLOB '*.*.*' AND open_interest NOT GLOB '*.*0' AND open_interest NOT GLOB '0[0-9]*'),
  open_interest_value TEXT    NOT NULL CHECK (typeof(open_interest_value) = 'text' AND length(open_interest_value) BETWEEN 1 AND 97 AND open_interest_value NOT GLOB '*[^0-9.]*' AND open_interest_value NOT GLOB '.*' AND open_interest_value NOT GLOB '*.' AND open_interest_value NOT GLOB '*.*.*' AND open_interest_value NOT GLOB '*.*0' AND open_interest_value NOT GLOB '0[0-9]*'),
  ingested_at_ms      INTEGER NOT NULL,
  PRIMARY KEY (venue, symbol, period, time_ms)
) STRICT, WITHOUT ROWID;

CREATE TRIGGER open_interest_no_update BEFORE UPDATE ON open_interest
  BEGIN SELECT RAISE(ABORT, 'open_interest is immutable'); END;
CREATE TRIGGER open_interest_no_delete BEFORE DELETE ON open_interest
  BEGIN SELECT RAISE(ABORT, 'open_interest is immutable'); END;

CREATE TABLE open_interest_conflicts (
  conflict_id         INTEGER PRIMARY KEY,
  venue               TEXT    NOT NULL,
  symbol              TEXT    NOT NULL,
  period              TEXT    NOT NULL,
  time_ms             INTEGER NOT NULL,
  open_interest       TEXT    NOT NULL CHECK (typeof(open_interest) = 'text' AND length(open_interest) BETWEEN 1 AND 97 AND open_interest NOT GLOB '*[^0-9.]*' AND open_interest NOT GLOB '.*' AND open_interest NOT GLOB '*.' AND open_interest NOT GLOB '*.*.*' AND open_interest NOT GLOB '*.*0' AND open_interest NOT GLOB '0[0-9]*'),
  open_interest_value TEXT    NOT NULL CHECK (typeof(open_interest_value) = 'text' AND length(open_interest_value) BETWEEN 1 AND 97 AND open_interest_value NOT GLOB '*[^0-9.]*' AND open_interest_value NOT GLOB '.*' AND open_interest_value NOT GLOB '*.' AND open_interest_value NOT GLOB '*.*.*' AND open_interest_value NOT GLOB '*.*0' AND open_interest_value NOT GLOB '0[0-9]*'),
  first_seen_at_ms    INTEGER NOT NULL
) STRICT;

CREATE UNIQUE INDEX open_interest_conflicts_observation ON open_interest_conflicts
  (venue, symbol, period, time_ms, open_interest, open_interest_value);
CREATE INDEX open_interest_conflicts_slot ON open_interest_conflicts (venue, symbol, period, time_ms);

CREATE TRIGGER open_interest_conflicts_no_update BEFORE UPDATE ON open_interest_conflicts
  BEGIN SELECT RAISE(ABORT, 'open_interest_conflicts are immutable'); END;
CREATE TRIGGER open_interest_conflicts_no_delete BEFORE DELETE ON open_interest_conflicts
  BEGIN SELECT RAISE(ABORT, 'open_interest_conflicts are immutable'); END;
