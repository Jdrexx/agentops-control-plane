#!/usr/bin/env bash
set -euo pipefail

database_url="${DATABASE_URL:-${AGENTOPS_DATABASE:-data/agentops.db}}"
backup_dir="${1:-backups}"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$backup_dir"

case "$database_url" in
  postgres://*|postgresql://*)
    output="$backup_dir/agentops-$timestamp.dump"
    pg_dump --format=custom --no-owner --no-privileges --file="$output" "$database_url"
    ;;
  *)
    output="$backup_dir/agentops-$timestamp.db"
    sqlite3 "$database_url" ".backup '$output'"
    ;;
esac

sha256sum "$output" > "$output.sha256"
printf 'Backup created: %s\n' "$output"
