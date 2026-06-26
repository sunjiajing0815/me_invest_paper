# Operational Runbook

Single place for "if X happens, do Y" for the self-hosted deployment. Started for the
soak-window (P0.3); the CNN-scrape / OpenD-restart / API-key-rotation / structured-logs +
Sentry sections are **P4.3** (stubs below).

> **Where the DB lives (ADR-0026):** the SQLite OLTP database is on the Docker **named volume
> `me_invest_dbdata`** at `/app/db/investor.db` — *not* `./data/investor.db` on the host. Parquet
> bars + DuckDB stay on the `./data` bind mount. A `docker volume prune` wipes the DB; host backup
> tools (Time Machine on `./data`) do not cover the volume. Hence the backup procedure below.

---

## Database backup & restore

### Automated weekly backup
- A weekly APScheduler job (`db_backup`, **Sun 02:00 ET**) runs `services/backup.py::backup_database`,
  which does `VACUUM INTO data/backups/investor-<UTCstamp>.db` (consistent snapshot, safe against a
  live reader) and prunes to the newest `BACKUP_KEEP` (default 8).
- `data/backups/` is on the **`./data` bind mount**, so these copies ARE host-visible and
  Time-Machine-covered — this is what closes the ADR-0026 host-backup gap.
- Settings: `BACKUP_ENABLED` (default `true`), `BACKUP_DIR` (default `data/backups`),
  `BACKUP_KEEP` (default `8`). Disable with `BACKUP_ENABLED=false`.
- Verify it ran: `ls -lt data/backups/` and look for a recent `investor-*.db`; the scheduler log
  line lists `DB backup Sun 02:00 ET`.

### Manual backup (on demand / before a migration)
Either trigger the same code path, or copy the live DB straight off the volume:

```bash
# (a) consistent snapshot into the bind mount via the app:
docker compose exec app uv run python -c \
  "from investor.config import Settings; from investor.services.backup import backup_database; \
   s=Settings(); print(backup_database(s.sqlite_path, s.backup_dir, s.backup_keep))"

# (b) raw copy of the volume DB to the host (app can be running; DELETE-journal single-writer):
docker run --rm -v me_invest_dbdata:/db -v "$PWD":/out alpine \
  cp /db/investor.db /out/investor.db.manual-$(date -u +%Y%m%dT%H%M%SZ)
```

### Restore — verify against a SCRATCH volume first (non-destructive)
```bash
BK=data/backups/investor-XXXXXXXX.db            # choose a backup
docker volume create me_invest_dbdata_test
docker run --rm -v me_invest_dbdata_test:/db -v "$PWD":/in alpine \
  sh -c "cp /in/$BK /db/investor.db && chown 1000:1000 /db/investor.db"
# sanity-query a recent row (expects e.g. TSLA target for account 62):
docker run --rm -v me_invest_dbdata_test:/db keinos/sqlite3 \
  sqlite3 /db/investor.db \
  "SELECT ticker FROM target_allocation WHERE broker_account_id=62 AND effective_to IS NULL ORDER BY ticker;"
docker volume rm me_invest_dbdata_test          # cleanup
```

### Restore — to PRODUCTION (destructive; do the scratch check first)
```bash
docker compose stop app
# back up the current (possibly-bad) volume first, then overwrite:
docker run --rm -v me_invest_dbdata:/db -v "$PWD":/out alpine \
  cp /db/investor.db /out/investor.db.pre-restore-$(date -u +%Y%m%dT%H%M%SZ)
docker run --rm -v me_invest_dbdata:/db -v "$PWD":/in alpine \
  sh -c "cp /in/$BK /db/investor.db && chown 1000:1000 /db/investor.db"
docker compose up -d app
# confirm health + journal mode + a recent row:
curl -s localhost:8000/health >/dev/null && echo healthy
docker compose exec app uv run python -c \
  "import sqlite3; c=sqlite3.connect('/app/db/investor.db'); \
   print('journal:', c.execute('PRAGMA journal_mode').fetchone()[0]); \
   print('targets62:', c.execute(\"SELECT COUNT(*) FROM target_allocation WHERE broker_account_id=62 AND effective_to IS NULL\").fetchone()[0])"
```
Expected after restore: `journal: delete` (the connect listener re-asserts it — ADR-0026) and the
expected target count. `init_db` fails fast if the restored DB is somehow in WAL.

### Periodic check
- Skim `data/backups/` weekly (or rely on the canary once P1.5 lands). Run
  `scripts/audit_integrity.py` after any restore to confirm consistency.

---

## (P4.3 stubs — to fill when those items land)
- **CNN Fear & Greed scrape failure** (ADR-0030 decision matrix): header refresh / accept NULLs /
  paid feed.
- **Moomoo OpenD restart**: OpenD must listen on `0.0.0.0:11111`; `lsof -i :11111`; recreate the app
  (`docker compose up -d --force-recreate`) after OpenD is back — startup currently blocks if OpenD
  is unreachable.
- **Alpaca / Moomoo API-key rotation**; **Tavily / Anthropic / Finnhub key locations + monthly cost**.
- **Structured-log + Sentry conventions** (P4.1 / P4.2).
