-- External-manager buy basket: names bought by the better-half managers, with score, buyers, region and sector.
CREATE OR REPLACE FUNCTION {uc}.efm_buy_basket(as_of DATE COMMENT 'latest month-end on or before this date is used', top_n INT COMMENT 'number of names, ranked with the tested top-50 basket first, then by score and net manager buying')
RETURNS TABLE (rank INT, company_id STRING, name STRING, ticker STRING, country STRING, region STRING, gics_sector STRING,
               score INT, buyers INT, net_buy_usd DOUBLE, in_top50 BOOLEAN, month_end DATE, source_ref STRING)
COMMENT 'External-manager (EFM) buy basket. score = consecutive month-ends (0-3) bought by better-half managers. SYNTHETIC DEMO DATA.'
RETURN
  WITH m AS (SELECT max(month_end) AS me FROM {uc}.efm_buy_basket WHERE month_end <= as_of),
  ranked AS (
    SELECT b.*, row_number() OVER (ORDER BY b.in_top50 DESC, b.score DESC, b.net_buy_usd DESC, b.buyers DESC, b.company_id) AS rk
    FROM {uc}.efm_buy_basket b JOIN m ON b.month_end = m.me
  )
  SELECT CAST(r.rk AS INT) AS rank, r.company_id, c.name, c.ticker, c.country, c.region, c.gics_sector,
         CAST(r.score AS INT) AS score, CAST(r.buyers AS INT) AS buyers, r.net_buy_usd, r.in_top50, r.month_end,
         concat('{uc}.efm_buy_basket@', CAST(r.month_end AS STRING)) AS source_ref
  FROM ranked r JOIN {uc}.ref_companies c USING (company_id)
  WHERE r.rk <= top_n
  ORDER BY r.rk
