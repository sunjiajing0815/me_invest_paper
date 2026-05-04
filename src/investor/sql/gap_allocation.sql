WITH latest AS (
  SELECT ticker, weight_pct, market_value,
         ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY ts DESC) AS rn
  FROM positions_snapshot
),
current AS (SELECT ticker, weight_pct, market_value FROM latest WHERE rn = 1),
account AS (SELECT equity_usd FROM broker_account WHERE effective_to IS NULL ORDER BY last_sync DESC LIMIT 1),
targets AS (
  SELECT ticker, target_pct, band_low_pct, band_high_pct
  FROM target_allocation
  WHERE effective_to IS NULL
)
SELECT
  t.ticker,
  -- weight_pct is stored as pct of total equity (incl. cash).
  -- Targets sum to 100 - cash_buffer_pct, so both sides share the same denominator — no scaling needed.
  COALESCE(c.weight_pct, 0)                                        AS current_pct,
  t.target_pct,
  t.target_pct - COALESCE(c.weight_pct, 0)                         AS gap_pct,
  (t.target_pct - COALESCE(c.weight_pct, 0)) / 100 * a.equity_usd AS gap_usd,
  CASE
    WHEN COALESCE(c.weight_pct, 0) < t.band_low_pct  THEN 'under'
    WHEN COALESCE(c.weight_pct, 0) > t.band_high_pct THEN 'over'
    ELSE 'in_band'
  END                                                               AS band_status
FROM targets t
LEFT JOIN current c USING (ticker)
CROSS JOIN account a
ORDER BY ABS(gap_pct) DESC
