-- Resolve a company by id, ticker or (partial) name. Every row carries `source_ref` for citation (design §5.5).
CREATE OR REPLACE FUNCTION {uc}.company_lookup(query STRING COMMENT 'company id (C0101), ticker (285A.T) or part of the name')
RETURNS TABLE (company_id STRING, ticker STRING, name STRING, gics_sector STRING, gics_industry STRING, country STRING, region STRING, msci_em BOOLEAN, source_ref STRING)
COMMENT 'Look up a company by id, ticker or name. SYNTHETIC DEMO DATA.'
RETURN
  SELECT c.company_id, c.ticker, c.name, c.gics_sector, c.gics_industry, c.country, c.region, c.msci_em,
         '{uc}.ref_companies' AS source_ref
  FROM {uc}.ref_companies c
  WHERE lower(c.company_id) = lower(trim(query))
     OR lower(c.ticker) = lower(trim(query))
     OR lower(c.name) LIKE concat('%', lower(trim(query)), '%')
  ORDER BY (CASE WHEN lower(c.company_id) = lower(trim(query)) OR lower(c.ticker) = lower(trim(query)) THEN 0 ELSE 1 END), c.market_cap_usd DESC
  LIMIT 10
