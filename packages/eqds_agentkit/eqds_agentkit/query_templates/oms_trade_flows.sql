-- oms_trade_flows@1  (Spark SQL over the UC foreign catalog; {placeholders} are validated, :params are bound)
WITH f AS (
  SELECT t.company_id, CAST({dim} AS STRING) AS group_key,
         SUM(CASE WHEN t.side = 'B' THEN t.value_usd ELSE -t.value_usd END) / 1e6 AS net_musd,
         SUM(t.value_usd) / 1e6 AS gross_musd,
         COUNT(DISTINCT CASE WHEN t.side = 'B' THEN t.account_id END) AS buy_accounts,
         COUNT(DISTINCT CASE WHEN t.side = 'S' THEN t.account_id END) AS sell_accounts,
         COUNT(*) AS trades,
         MAX(CAST(t.is_rebalance AS INT)) AS any_rebalance
  FROM {fc}.oms.trades t
  WHERE t.company_id IN ({ids}) AND t.trade_date BETWEEN :start AND :end
  GROUP BY t.company_id, CAST({dim} AS STRING)
)
SELECT f.company_id, c.name, f.group_key, ROUND(f.net_musd, 2) AS net_musd, ROUND(f.gross_musd, 2) AS gross_musd,
       f.buy_accounts, f.sell_accounts, f.trades, f.any_rebalance
FROM f LEFT JOIN {uc}.ref_companies c ON c.company_id = f.company_id
ORDER BY f.company_id, ABS(f.net_musd) DESC
