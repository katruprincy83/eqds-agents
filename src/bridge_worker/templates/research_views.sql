-- research_views@1
SELECT view_id, company_id, analyst, rating, active_weight_bps, effective_date, rationale
FROM {fc}.research.analyst_views WHERE company_id = :company_id AND effective_date >= :since
ORDER BY effective_date DESC
