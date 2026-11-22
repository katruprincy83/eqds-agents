-- oms_confirm_conflict@1
WITH flows AS (
  SELECT t.company_id,
         SUM(CASE WHEN t.side = 'B' THEN t.value_usd ELSE -t.value_usd END) / 1e6 AS net_musd,
         COUNT(DISTINCT CASE WHEN t.side = 'S' THEN t.account_id END) AS sell_accounts,
         COUNT(DISTINCT CASE WHEN t.side = 'B' THEN t.account_id END) AS buy_accounts,
         MAX(CAST(t.is_rebalance AS INT)) AS any_rebalance
  FROM {fc}.oms.trades t
  WHERE t.trade_date BETWEEN :start AND :end {id_filter}
  GROUP BY t.company_id
),
universe AS ({universe})
SELECT u.company_id, c.name, u.score, u.buyers, ROUND(COALESCE(f.net_musd, 0), 2) AS net_musd,
       COALESCE(f.sell_accounts, 0) AS sell_accounts, COALESCE(f.buy_accounts, 0) AS buy_accounts, COALESCE(f.any_rebalance, 0) AS any_rebalance,
       CASE WHEN f.net_musd >=  :strong_musd THEN 'strong_confirm'
            WHEN f.net_musd <= -:strong_musd THEN 'strong_conflict'
            WHEN f.net_musd > 0 THEN 'confirm' WHEN f.net_musd < 0 THEN 'conflict' ELSE 'none' END AS class
FROM universe u LEFT JOIN flows f ON f.company_id = u.company_id LEFT JOIN {uc}.ref_companies c ON c.company_id = u.company_id
ORDER BY ABS(COALESCE(f.net_musd, 0)) DESC
