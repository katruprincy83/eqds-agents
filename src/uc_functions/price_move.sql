-- % price move over N trading days from a start date (UC prices_daily).
CREATE OR REPLACE FUNCTION {uc}.price_move(company_id STRING, from_date DATE COMMENT 'start date (first trading day on or after)', days INT COMMENT 'trading days forward')
RETURNS TABLE (company_id STRING, from_date DATE, to_date DATE, start_close DOUBLE, end_close DOUBLE, pct_move DOUBLE, trading_days INT, source_ref STRING)
COMMENT 'Percent price move over N trading days from a date. SYNTHETIC DEMO DATA.'
RETURN
  WITH p AS (
    SELECT date, close, row_number() OVER (ORDER BY date) AS rn
    FROM {uc}.prices_daily WHERE company_id = price_move.company_id
  ),
  s AS (SELECT date, close, rn FROM p WHERE date >= price_move.from_date ORDER BY date LIMIT 1),
  e AS (SELECT p.date, p.close, p.rn FROM p, s WHERE p.rn <= s.rn + price_move.days ORDER BY p.rn DESC LIMIT 1)
  SELECT price_move.company_id, s.date, e.date, s.close, e.close, (e.close / s.close - 1) * 100,
         CAST(e.rn - s.rn AS INT), '{uc}.prices_daily'
  FROM s, e
