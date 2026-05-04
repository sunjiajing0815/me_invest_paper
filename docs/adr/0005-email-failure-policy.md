# ADR-0005: Email Failure Policy — Re-raise, Don't Swallow

**Date:** 2026-04-28
**Status:** Accepted

## Context

The daily report job (`run_daily_report`) sends an email at the end of each trading day. The job is invoked by APScheduler's `BackgroundScheduler`. A design decision was needed: what happens when `emailer.send()` raises an exception?

Two options were considered:

| Option | Behaviour on send failure |
|---|---|
| A | Re-raise — let APScheduler record the failure |
| B | Catch and log — job always succeeds, email loss is silent |

## Decision

**Option A** — re-raise on email failure.

```python
# run_daily_report — no try/except around emailer.send()
emailer.send(to=settings.email_to, subject=subject, html=html, text=text)
# if this raises, the exception propagates to APScheduler
```

## Rationale

### Silent swallow (Option B) is dangerous for a notification-only system

The entire value of Phase 1 is the daily email. If the email silently fails, the user receives no signal, loses awareness of drift, and may miss a rebalance window. A job that always "succeeds" makes monitoring meaningless.

### APScheduler already has job failure semantics

When a job raises, APScheduler logs the exception at `ERROR` level and records the failure in its internal job store. The `misfire_grace_time=1800` setting means: if the scheduler restarts within 30 minutes of the missed 16:15 ET fire time, it will run the job immediately. This provides a natural retry on transient SMTP failures (e.g., Gmail rate limiting, brief network outage).

### Alert threshold

Because the user monitors the inbox, two consecutive trading-day emails missing is the practical alert threshold. Investigation steps:

1. Check `docker compose logs -f app` for `ERROR` lines containing `run_daily_report`
2. Verify SMTP credentials with `curl -s -X POST localhost:8000/admin/run-daily-report`
3. Check Gmail App Password hasn't been revoked at `https://myaccount.google.com/apppasswords`

## Consequences

- `run_daily_report` does not have a try/except around `emailer.send()`
- The position snapshot is already written to DB before the email step — a send failure does not lose position data, only the notification
- Phase 2 monitoring (if added) can detect consecutive failures via APScheduler's job history or log scraping
- `FakeEmailer` used in tests never raises, so tests are unaffected by this policy
