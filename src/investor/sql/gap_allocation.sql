WITH latest AS (
  SELECT ticker, weight_pct, market_value,
         ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY ts DESC) AS rn
  FROM positions_snapshot
),
current AS (SELECT ticker, weight_pct, market_value FROM latest WHERE rn = 1),
account AS (SELECT equity_usd FROM broker_account ORDER BY last_sync DESC LIMIT 1),
targets AS (
  SELECT ticker, target_pct, band_low_pct, band_high_pct
  FROM target_allocation
  WHERE effective_to IS NULL
)
SELECT
  t.ticker,
  COALESCE(c.weight_pct, 0)                                        AS current_pct,
  t.target_pct,
  t.target_pct - COALESCE(c.weight_pct, 0)                         AS gap_pct,
  (t.target_pct - COALESCE(c.weight_pct, 0)) / 100 * a.equity_usd AS gap_usd
FROM targets t
LEFT JOIN current c USING (ticker)
CROSS JOIN account a
ORDER BY ABS(gap_pct) DESC
