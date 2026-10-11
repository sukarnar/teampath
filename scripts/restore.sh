#!/usr/bin/env bash
# Restore a backup made by backup.sh:  ./scripts/restore.sh backups/db-XXXX.dump.gpg backups/files-XXXX.tgz.gpg
# Practise this on a test server; an untested backup is not a backup.
set -euo pipefail
cd "$(dirname "$0")/.."
source .env
db="$1"; files="$2"
docker compose stop app worker
gpg --batch --decrypt --passphrase "$BACKUP_PASSPHRASE" "$db" \
  | docker compose exec -T db pg_restore -U teampath_owner -d teampath --clean --if-exists
gpg --batch --decrypt --passphrase "$BACKUP_PASSPHRASE" "$files" \
  | docker compose run --rm --no-deps -T --entrypoint tar app -C /data -xz
docker compose start app worker
echo "restored"
