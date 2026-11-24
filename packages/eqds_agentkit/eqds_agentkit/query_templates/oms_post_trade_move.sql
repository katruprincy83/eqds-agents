-- oms_post_trade_move@1
WITH days AS (
  SELECT t.trade_date, SUM(CASE WHEN t.side = 'B' THEN t.value_usd ELSE -t.value_usd END) AS net_usd
  FROM {fc}.oms.trades t WHERE t.company_id = :company_id AND t.trade_date BETWEEN :start AND :end GROUP BY t.trade_date
),
p AS (SELECT date, close, row_number() OVER (ORDER BY date) AS rn FROM {uc}.prices_daily WHERE company_id = :company_id),
mx AS (SELECT max(rn) AS m FROM p),
s AS (
  SELECT trade_date, net_usd, rn, close AS c0 FROM (
    SELECT d.trade_date, d.net_usd, p.rn, p.close, row_number() OVER (PARTITION BY d.trade_date ORDER BY p.date) AS k
    FROM days d JOIN p ON p.date >= d.trade_date) WHERE k = 1
),
e AS (SELECT s.trade_date, s.net_usd, s.c0, p.close AS c1, p.date AS d1 FROM s CROSS JOIN mx JOIN p ON p.rn = LEAST(s.rn + :horizon_days, mx.m))
SELECT :company_id AS company_id, COUNT(*) AS trade_days, MIN(trade_date) AS first_trade_date, MAX(d1) AS last_horizon_date,
       ROUND(SUM(net_usd) / 1e6, 2) AS net_musd,
       ROUND(SUM(ABS(net_usd) * (c1 / c0 - 1)) / NULLIF(SUM(ABS(net_usd)), 0) * 100, 2) AS weighted_return_pct,
       :horizon_days AS horizon_days
FROM e
