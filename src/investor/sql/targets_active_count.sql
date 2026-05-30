SELECT COUNT(*)
FROM target_allocation
WHERE effective_to IS NULL AND broker_account_id = :broker_account_id
