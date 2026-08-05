# Backup and recovery

Back up the operational database and `AGENTOPS_ENCRYPTION_KEY` separately. A database
backup without its original encryption key cannot recover encrypted project secrets.

## Create a backup

Install the PostgreSQL client for PostgreSQL deployments or `sqlite3` for local mode,
then run:

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
