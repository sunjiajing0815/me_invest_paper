-- Positions that exist in the portfolio but have no active target allocation.
WITH latest AS (
  SELECT ticker, qty, market_value, weight_pct,
         ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY ts DESC) AS rn
  FROM positions_snapshot
),
active_targets AS (
  SELECT ticker FROM target_allocation WHERE effective_to IS NULL
)
SELECT l.ticker, l.qty, l.market_value, l.weight_pct
FROM latest l
WHERE l.rn = 1
  AND l.qty > 0
  AND l.ticker NOT IN (SELECT ticker FROM active_targets)
ORDER BY l.market_value DESC
