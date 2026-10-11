#!/usr/bin/env bash
# Nightly encrypted backup of the database and uploaded files.
# Run from the deploy directory via cron, e.g.:
#   15 3 * * * cd /opt/teampath && ./scripts/backup.sh >> backups/backup.log 2>&1
# Copy the backups/ folder off the server (rclone, restic, rsync) — a backup that
# lives only on the VPS doesn't protect against losing the VPS.
set -euo pipefail
cd "$(dirname "$0")/.."
source .env
mkdir -p backups
stamp=$(date +%Y%m%d-%H%M%S)
docker compose exec -T db pg_dump -U teampath_owner -Fc teampath \
  | gpg --batch --yes --symmetric --cipher-algo AES256 --passphrase "$BACKUP_PASSPHRASE" -o "backups/db-$stamp.dump.gpg"
docker compose run --rm --no-deps --entrypoint tar app -C /data -cz files \
  | gpg --batch --yes --symmetric --cipher-algo AES256 --passphrase "$BACKUP_PASSPHRASE" -o "backups/files-$stamp.tgz.gpg"
find backups -name "*.gpg" -mtime +14 -delete
echo "backup $stamp ok"
