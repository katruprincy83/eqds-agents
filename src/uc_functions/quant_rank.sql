-- EM quant score and quintile for companies (matched by id, ticker or name). Names outside the MSCI EM
-- quant universe are returned with in_universe = false so the specialist can state the gap (PF-02/03/19).
CREATE OR REPLACE FUNCTION {uc}.quant_rank(tickers ARRAY<STRING> COMMENT 'company ids, tickers or names', as_of DATE COMMENT 'quant month-end (latest on or before is used)')
RETURNS TABLE (query STRING, company_id STRING, name STRING, ticker STRING, found BOOLEAN, in_universe BOOLEAN,
               score DOUBLE, quintile INT, drivers MAP<STRING, DOUBLE>, month_end DATE, source_ref STRING)
COMMENT 'EM quant score (MSCI EM universe only; quintile 1 = best, 5 = worst). SYNTHETIC DEMO DATA.'
RETURN
  WITH m AS (SELECT max(q.month_end) AS me FROM {uc}.em_quant_scores q WHERE q.month_end <= quant_rank.as_of),
  req AS (SELECT explode(array_distinct(quant_rank.tickers)) AS query),
  matched AS (
    SELECT r.query, c.company_id, c.name, c.ticker
    FROM req r LEFT JOIN {uc}.ref_companies c
      ON lower(c.company_id) = lower(trim(r.query)) OR lower(c.ticker) = lower(trim(r.query)) OR lower(c.name) = lower(trim(r.query))
  )
  SELECT mt.query, mt.company_id, mt.name, mt.ticker, mt.company_id IS NOT NULL AS found, s.company_id IS NOT NULL AS in_universe,
         s.score, CAST(s.quintile AS INT), s.drivers, m.me AS month_end,
         concat('{uc}.em_quant_scores@', CAST(m.me AS STRING)) AS source_ref
  FROM matched mt CROSS JOIN m LEFT JOIN {uc}.em_quant_scores s ON s.company_id = mt.company_id AND s.month_end = m.me
