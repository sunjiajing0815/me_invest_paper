-- params mon, fri: dates (as strings 'YYYY-MM-DD'); broker_account_id: int
-- Scoped to one broker account. Uses positions_snapshot.weight_pct, which is stored as a
-- pct of TOTAL account equity (incl. cash) — the SAME basis target_pct is defined against
-- (see gap_allocation.sql). Do NOT recompute market_value / SUM(market_value): that excludes
-- cash, so with a large cash balance every weight is inflated ~1/(1-cash%) and an
-- under-target holding looks over-target.
WITH mon_snap AS (
  -- The single latest snapshot batch (one ts = one sync = one row per ticker). Using the
  -- latest *ts*, not all rows on the latest DATE, avoids fan-out when a day has multiple syncs
  -- (daily report + weekly review + manual all write a full snapshot the same day).
  SELECT ticker, weight_pct
    FROM positions_snapshot
   WHERE broker_account_id = :broker_account_id
     AND ts = (
       SELECT MAX(ts) FROM positions_snapshot
        WHERE broker_account_id = :broker_account_id AND DATE(ts) <= :mon
     )
),
fri_snap AS (
  SELECT ticker, weight_pct
    FROM positions_snapshot
   WHERE broker_account_id = :broker_account_id
     AND ts = (
       SELECT MAX(ts) FROM positions_snapshot
        WHERE broker_account_id = :broker_account_id AND DATE(ts) <= :fri
     )
),
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
  COALESCE(ms.weight_pct, 0)                   AS current_pct_mon,
  COALESCE(fs.weight_pct, 0)                   AS current_pct_fri,
  (SELECT MAX(DATE(ts)) FROM positions_snapshot
    WHERE broker_account_id = :broker_account_id AND DATE(ts) <= :mon) AS actual_mon_date
FROM targets t
LEFT JOIN mon_snap ms ON ms.ticker = t.ticker
LEFT JOIN fri_snap fs ON fs.ticker = t.ticker
ORDER BY t.target_pct DESC
