-- params mon: date (Monday of operations week); broker_account_id: int
WITH sug AS (
  SELECT id, ticker, side, qty
    FROM order_suggestion
   WHERE week_of = :mon
     AND broker_account_id = :broker_account_id
),
exec_live AS (
  SELECT oe.suggestion_id,
         oe.ticker,
         COALESCE(oe.submitted_qty, oe.filled_qty) AS routed_qty,
         oe.filled_qty,
         oe.filled_price
    FROM order_execution oe
    JOIN sug s ON oe.suggestion_id = s.id
   WHERE oe.dry_run = 0
     AND oe.status IN ('accepted_for_routing','filled','partially_filled','broker_cancelled')
)
SELECT
  s.ticker,
  s.side                                                                             AS side_suggested,
  s.qty                                                                              AS suggested_qty,
  COALESCE(SUM(e.routed_qty), 0)                                                   AS routed_qty_live,
  COALESCE(SUM(CASE WHEN e.filled_price IS NOT NULL THEN e.filled_qty  ELSE 0 END), 0) AS filled_qty_live,
  COALESCE(SUM(CASE WHEN e.filled_price IS NOT NULL
                    THEN e.filled_qty * e.filled_price ELSE 0 END), 0)             AS filled_usd_live
FROM sug s
LEFT JOIN exec_live e ON e.suggestion_id = s.id
GROUP BY s.ticker, s.side, s.qty
ORDER BY s.ticker
