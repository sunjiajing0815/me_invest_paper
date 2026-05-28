-- param mon: date (Monday of operations week)
WITH sug AS (
  SELECT id, ticker, side, qty, limit_price
    FROM order_suggestion
   WHERE week_of = :mon
),
exec_all AS (
  SELECT oe.suggestion_id,
         oe.side,
         oe.ticker,
         oe.dry_run,
         COALESCE(oe.submitted_qty, oe.filled_qty)  AS routed_qty,
         oe.limit_price                              AS exec_limit,
         oe.filled_qty,
         oe.filled_price
    FROM order_execution oe
    JOIN sug ON oe.suggestion_id = sug.id
)
SELECT
  (SELECT COALESCE(SUM(routed_qty * COALESCE(exec_limit, 0)), 0)
     FROM exec_all WHERE dry_run = 0 AND side = 'buy')    AS buy_routed_usd,
  (SELECT COALESCE(SUM(routed_qty * COALESCE(exec_limit, 0)), 0)
     FROM exec_all WHERE dry_run = 0 AND side = 'sell')   AS sell_routed_usd,
  (SELECT COALESCE(SUM(filled_qty * filled_price), 0)
     FROM exec_all WHERE dry_run = 0 AND side = 'buy'
       AND filled_price IS NOT NULL)                       AS buy_filled_usd,
  (SELECT COALESCE(SUM(filled_qty * filled_price), 0)
     FROM exec_all WHERE dry_run = 0 AND side = 'sell'
       AND filled_price IS NOT NULL)                       AS sell_filled_usd,
  (SELECT COUNT(DISTINCT ticker)
     FROM exec_all WHERE dry_run = 0)                     AS distinct_tickers,
  (SELECT COALESCE(SUM(routed_qty * COALESCE(exec_limit, 0)), 0)
     FROM exec_all WHERE dry_run = 1 AND side = 'buy')    AS dry_run_buy_usd,
  (SELECT COALESCE(SUM(routed_qty * COALESCE(exec_limit, 0)), 0)
     FROM exec_all WHERE dry_run = 1 AND side = 'sell')   AS dry_run_sell_usd
