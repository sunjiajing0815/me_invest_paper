-- params mon, fri: dates (as strings 'YYYY-MM-DD')
WITH mon_snap AS (
  SELECT ticker, market_value
    FROM positions_snapshot
   WHERE DATE(ts) = (
     SELECT MAX(DATE(ts)) FROM positions_snapshot WHERE DATE(ts) <= :mon
   )
),
fri_snap AS (
  SELECT ticker, market_value
    FROM positions_snapshot
   WHERE DATE(ts) = (
     SELECT MAX(DATE(ts)) FROM positions_snapshot WHERE DATE(ts) <= :fri
   )
),
mon_equity AS (SELECT COALESCE(SUM(market_value), 0) AS total FROM mon_snap),
fri_equity AS (SELECT COALESCE(SUM(market_value), 0) AS total FROM fri_snap),
targets AS (
  SELECT ticker, target_pct
    FROM target_allocation
   WHERE effective_from <= :fri
     AND (effective_to IS NULL OR effective_to > :fri)
)
SELECT
  t.ticker,
  t.target_pct,
  CASE WHEN (SELECT total FROM mon_equity) > 0
       THEN COALESCE(ms.market_value, 0) / (SELECT total FROM mon_equity) * 100
       ELSE 0 END                              AS current_pct_mon,
  CASE WHEN (SELECT total FROM fri_equity) > 0
       THEN COALESCE(fs.market_value, 0) / (SELECT total FROM fri_equity) * 100
       ELSE 0 END                              AS current_pct_fri,
  (SELECT MAX(DATE(ts)) FROM positions_snapshot WHERE DATE(ts) <= :mon) AS actual_mon_date
FROM targets t
LEFT JOIN mon_snap ms ON ms.ticker = t.ticker
LEFT JOIN fri_snap fs ON fs.ticker = t.ticker
ORDER BY t.target_pct DESC
