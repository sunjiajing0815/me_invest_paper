-- Positions that exist in the portfolio but have no active target allocation.
WITH latest AS (
  SELECT ticker, qty, market_value, weight_pct, currency,
         ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY ts DESC) AS rn
  FROM positions_snapshot
  WHERE broker_account_id = :broker_account_id
),
active_targets AS (
  SELECT ticker FROM target_allocation
  WHERE effective_to IS NULL AND broker_account_id = :broker_account_id
)
SELECT l.ticker, l.qty, l.market_value, l.weight_pct, l.currency
FROM latest l
WHERE l.rn = 1
  AND l.qty > 0
  AND l.ticker NOT IN (SELECT ticker FROM active_targets)
ORDER BY l.market_value DESC
