SELECT last_sync
FROM broker_account
WHERE account_ref = :broker_account_id AND effective_to IS NULL
ORDER BY last_sync DESC
LIMIT 1
