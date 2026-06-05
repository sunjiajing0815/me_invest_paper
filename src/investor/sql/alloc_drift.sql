-- params mon, fri: dates (as strings 'YYYY-MM-DD'); broker_account_id: int
-- All snapshot + target reads are scoped to one broker account so a ticker targeted in
-- multiple accounts doesn't produce duplicate drift rows or a mixed-equity denominator.
WITH mon_snap AS (
  -- The single latest snapshot batch (one ts = one sync = one row per ticker). Using the
  -- latest *ts*, not all rows on the latest DATE, avoids fan-out when a day has multiple
  -- syncs (daily report + weekly review + manual all write a full snapshot the same day).
  SELECT ticker, market_value
    FROM positions_snapshot
   WHERE broker_account_id = :broker_account_id
     AND ts = (
       SELECT MAX(ts) FROM positions_snapshot
        WHERE broker_account_id = :broker_account_id AND DATE(ts) <= :mon
     )
),
fri_snap AS (
  SELECT ticker, market_value
    FROM positions_snapshot
   WHERE broker_account_id = :broker_account_id
     AND ts = (
       SELECT MAX(ts) FROM positions_snapshot
        WHERE broker_account_id = :broker_account_id AND DATE(ts) <= :fri
     )
),
mon_equity AS (SELECT COALESCE(SUM(market_value), 0) AS total FROM mon_snap),
fri_equity AS (SELECT COALESCE(SUM(market_value), 0) AS total FROM fri_snap),
targets AS (
  SELECT ticker, target_pct
    FROM target_allocation
   WHERE broker_account_id = :broker_account_id
     AND effective_from <= :fri
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
  (SELECT MAX(DATE(ts)) FROM positions_snapshot
    WHERE broker_account_id = :broker_account_id AND DATE(ts) <= :mon) AS actual_mon_date
FROM targets t
LEFT JOIN mon_snap ms ON ms.ticker = t.ticker
LEFT JOIN fri_snap fs ON fs.ticker = t.ticker
ORDER BY t.target_pct DESC
