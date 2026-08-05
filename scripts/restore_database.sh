#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  printf 'Usage: %s <backup.dump|backup.db>\n' "$0" >&2
  exit 2
fi

backup="$1"
database_url="${DATABASE_URL:-${AGENTOPS_DATABASE:-data/agentops.db}}"
sha256sum --check "$backup.sha256"

case "$database_url" in
  postgres://*|postgresql://*)
    pg_restore --clean --if-exists --no-owner --no-privileges --dbname="$database_url" "$backup"
    ;;
  *)
    mkdir -p "$(dirname "$database_url")"
    sqlite3 "$database_url" ".restore '$backup'"
    ;;
esac

printf 'Restore completed: %s\n' "$database_url"
