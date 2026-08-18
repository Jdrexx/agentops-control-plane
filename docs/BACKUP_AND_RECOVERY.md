# Backup and recovery

Back up the operational database and `AGENTOPS_ENCRYPTION_KEY` separately. A database
backup without its original encryption key cannot recover encrypted project secrets.

## Create a backup

Install the PostgreSQL client for PostgreSQL deployments or `sqlite3` for local mode,
then run:

```bash
./scripts/backup_database.sh ./backups
```

The command above backs up the default `data/agentops.db` SQLite database. To back up a
PostgreSQL database instead, provide its URL:

```bash
DATABASE_URL='postgresql://...' ./scripts/backup_database.sh /secure/agentops-backups
```

The command creates a timestamped backup and SHA-256 checksum. Copy both files to
encrypted storage outside the application environment. Do not commit backups.

## Verify a restore

Restore into an empty staging database before relying on a backup:

```bash
DATABASE_URL='postgresql://staging...' ./scripts/restore_database.sh \
  /secure/agentops-backups/agentops-YYYYMMDDTHHMMSSZ.dump
```

The restore command verifies the checksum first. It may replace objects in the target
database, so never point it at production during a routine test.

Recommended cadence: daily backups, 30-day retention, and a documented restore drill
at least once per quarter.

## Optional S3-compatible backups

`scripts/backup_to_s3.py` creates a PostgreSQL custom-format dump, stores its SHA-256
checksum as object metadata, uploads it to S3-compatible storage, and removes objects
older than `BACKUP_RETENTION_DAYS`. This is not needed for the personal SQLite setup. If
the project later moves to PostgreSQL, run it from any private cron or scheduled-task
environment with only `DATABASE_URL` and bucket credentials; it does not require the
application API key or encryption key.
