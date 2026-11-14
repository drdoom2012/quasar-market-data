-- Row readability (ADR-0005 P-2). The cut-over lies above every stamp stored so far and is taken under this
-- migration's write lock (the runner binds :applied_at_ms after BEGIN IMMEDIATE), so every row stored before it was
-- readable by then. Every later row and conflict record must belong to a registered write transaction.

CREATE TABLE readability_cutover (
  id           INTEGER PRIMARY KEY CHECK (id = 1),
  cutover_ms   INTEGER NOT NULL CHECK (cutover_ms BETWEEN 946684800000 AND 4102444800000)
) STRICT;
INSERT INTO readability_cutover (id, cutover_ms) SELECT 1, max(
  :applied_at_ms,
  coalesce((SELECT max(ingested_at_ms)   FROM candles), 0),
  coalesce((SELECT max(first_seen_at_ms) FROM candle_conflicts), 0),
  coalesce((SELECT max(ingested_at_ms)   FROM open_interest), 0),
  coalesce((SELECT max(first_seen_at_ms) FROM open_interest_conflicts), 0),
  coalesce((SELECT max(ingested_at_ms)   FROM funding_rates), 0),
  coalesce((SELECT max(first_seen_at_ms) FROM funding_rate_conflicts), 0)) + 1;
CREATE TRIGGER readability_cutover_no_update BEFORE UPDATE ON readability_cutover
  BEGIN SELECT RAISE(ABORT, 'readability_cutover is immutable'); END;
CREATE TRIGGER readability_cutover_no_delete BEFORE DELETE ON readability_cutover
  BEGIN SELECT RAISE(ABORT, 'readability_cutover is immutable'); END;

CREATE TABLE write_transactions (
  table_name   TEXT    NOT NULL CHECK (table_name IN ('candles', 'open_interest', 'funding_rates')),
  stamp_ms     INTEGER NOT NULL CHECK (stamp_ms BETWEEN 946684800000 AND 4102444800000),
  PRIMARY KEY (table_name, stamp_ms)
) STRICT, WITHOUT ROWID;
CREATE TRIGGER write_transactions_after_cutover BEFORE INSERT ON write_transactions
  WHEN NEW.stamp_ms <= (SELECT cutover_ms FROM readability_cutover WHERE id = 1)
  BEGIN SELECT RAISE(ABORT, 'write_transactions: stamp at or before the cut-over'); END;
CREATE TRIGGER write_transactions_no_update BEFORE UPDATE ON write_transactions
  BEGIN SELECT RAISE(ABORT, 'write_transactions is immutable'); END;
CREATE TRIGGER write_transactions_no_delete BEFORE DELETE ON write_transactions
  BEGIN SELECT RAISE(ABORT, 'write_transactions is immutable'); END;

CREATE TABLE write_confirmations (
  table_name      TEXT    NOT NULL,
  stamp_ms        INTEGER NOT NULL,
  confirmed_at_ms INTEGER NOT NULL CHECK (confirmed_at_ms BETWEEN 946684800000 AND 4102444800000),
  PRIMARY KEY (table_name, stamp_ms)
) STRICT, WITHOUT ROWID;
CREATE TRIGGER write_confirmations_registered BEFORE INSERT ON write_confirmations
  WHEN NOT EXISTS (SELECT 1 FROM write_transactions WHERE table_name = NEW.table_name AND stamp_ms = NEW.stamp_ms)
  BEGIN SELECT RAISE(ABORT, 'write_confirmations: no registered write transaction'); END;
CREATE TRIGGER write_confirmations_no_update BEFORE UPDATE ON write_confirmations
  BEGIN SELECT RAISE(ABORT, 'write_confirmations is immutable'); END;
CREATE TRIGGER write_confirmations_no_delete BEFORE DELETE ON write_confirmations
  BEGIN SELECT RAISE(ABORT, 'write_confirmations is immutable'); END;

CREATE TRIGGER candles_registered BEFORE INSERT ON candles
  WHEN NOT EXISTS (SELECT 1 FROM write_transactions WHERE table_name = 'candles' AND stamp_ms = NEW.ingested_at_ms)
  BEGIN SELECT RAISE(ABORT, 'candles: row without a registered write transaction'); END;
CREATE TRIGGER candle_conflicts_registered BEFORE INSERT ON candle_conflicts
  WHEN NOT EXISTS (SELECT 1 FROM write_transactions WHERE table_name = 'candles' AND stamp_ms = NEW.first_seen_at_ms)
  BEGIN SELECT RAISE(ABORT, 'candle_conflicts: row without a registered write transaction'); END;
CREATE TRIGGER open_interest_registered BEFORE INSERT ON open_interest
  WHEN NOT EXISTS (SELECT 1 FROM write_transactions WHERE table_name = 'open_interest' AND stamp_ms = NEW.ingested_at_ms)
  BEGIN SELECT RAISE(ABORT, 'open_interest: row without a registered write transaction'); END;
CREATE TRIGGER open_interest_conflicts_registered BEFORE INSERT ON open_interest_conflicts
  WHEN NOT EXISTS (SELECT 1 FROM write_transactions WHERE table_name = 'open_interest' AND stamp_ms = NEW.first_seen_at_ms)
  BEGIN SELECT RAISE(ABORT, 'open_interest_conflicts: row without a registered write transaction'); END;
CREATE TRIGGER funding_rates_registered BEFORE INSERT ON funding_rates
  WHEN NOT EXISTS (SELECT 1 FROM write_transactions WHERE table_name = 'funding_rates' AND stamp_ms = NEW.ingested_at_ms)
  BEGIN SELECT RAISE(ABORT, 'funding_rates: row without a registered write transaction'); END;
CREATE TRIGGER funding_rate_conflicts_registered BEFORE INSERT ON funding_rate_conflicts
  WHEN NOT EXISTS (SELECT 1 FROM write_transactions WHERE table_name = 'funding_rates' AND stamp_ms = NEW.first_seen_at_ms)
  BEGIN SELECT RAISE(ABORT, 'funding_rate_conflicts: row without a registered write transaction'); END;
