WITH ranked AS (
  SELECT *,
         ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY ts DESC) AS rn
  FROM positions_snapshot
)
SELECT ticker, ts, qty, avg_cost, market_value, weight_pct
FROM ranked
WHERE rn = 1
  AND qty > 0
ORDER BY weight_pct DESC
