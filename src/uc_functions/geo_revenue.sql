-- Revenue by region for a company (shares sum to 100% per fiscal year).
CREATE OR REPLACE FUNCTION {uc}.geo_revenue(company_id STRING COMMENT 'company id, e.g. C0102')
RETURNS TABLE (company_id STRING, fiscal_year STRING, region STRING, revenue_usd DOUBLE, share_pct DOUBLE, source STRING, source_ref STRING)
COMMENT 'Geographic revenue split by fiscal year. SYNTHETIC DEMO DATA.'
RETURN
  SELECT g.company_id, g.fiscal_year, g.region, g.revenue_usd, g.share_pct, g.source, '{uc}.geo_revenue' AS source_ref
  FROM {uc}.geo_revenue g WHERE g.company_id = geo_revenue.company_id
  ORDER BY g.fiscal_year, g.region
