-- oms_positions@1
SELECT p.company_id, c.name, MAX(p.as_of) AS as_of, COUNT(DISTINCT p.account_id) AS accounts,
       ROUND(SUM(p.market_value_usd) / 1e6, 2) AS market_value_musd, SUM(p.quantity) AS quantity,
       ROUND(AVG(p.weight_bps), 1) AS avg_weight_bps, ROUND(AVG(p.active_weight_bps), 1) AS avg_active_weight_bps
FROM {fc}.oms.positions p
JOIN {fc}.oms.accounts a ON a.account_id = p.account_id
LEFT JOIN {uc}.ref_companies c ON c.company_id = p.company_id
WHERE p.company_id IN ({ids}) AND p.as_of = (SELECT MAX(as_of) FROM {fc}.oms.positions WHERE as_of <= :as_of)
  AND (:fund = '' OR a.fund_code = :fund)
GROUP BY p.company_id, c.name ORDER BY p.company_id
