-- param mon: date (Monday of operations week)
WITH suggestions AS (
  SELECT id, status
    FROM order_suggestion
   WHERE week_of = :mon
),
live_exec AS (
  SELECT oe.suggestion_id, oe.status
    FROM order_execution oe
    JOIN suggestions s ON oe.suggestion_id = s.id
   WHERE oe.dry_run = 0
),
dry_exec AS (
  SELECT oe.suggestion_id
    FROM order_execution oe
    JOIN suggestions s ON oe.suggestion_id = s.id
   WHERE oe.dry_run = 1
)
SELECT
  (SELECT COUNT(*) FROM suggestions)                                          AS suggested,
  (SELECT COUNT(*) FROM suggestions WHERE status = 'accepted')                AS accepted,
  (SELECT COUNT(DISTINCT suggestion_id) FROM live_exec
     WHERE status IN ('accepted_for_routing','filled','partially_filled','broker_cancelled'))
                                                                              AS routed_live,
  (SELECT COUNT(DISTINCT suggestion_id) FROM live_exec
     WHERE status = 'filled')                                                 AS filled_live,
  (SELECT COUNT(DISTINCT suggestion_id) FROM live_exec
     WHERE status = 'partially_filled')                                       AS partial_live,
  (SELECT COUNT(*) FROM suggestions s2
     WHERE s2.status = 'accepted'
       AND NOT EXISTS (
             SELECT 1 FROM live_exec le WHERE le.suggestion_id = s2.id))     AS accepted_not_routed,
  (SELECT COUNT(*) FROM suggestions WHERE status = 'rejected')                AS rejected,
  (SELECT COUNT(*) FROM suggestions WHERE status = 'expired')                 AS expired,
  (SELECT COUNT(DISTINCT suggestion_id) FROM dry_exec)                        AS dry_run_count
