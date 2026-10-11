#!/bin/sh
set -e
case "$1" in
  web)
    # Migrations run as the database owner (MIGRATION_DATABASE_URL); the app then
    # connects as the restricted, row-level-security role (DATABASE_URL).
    python -m alembic upgrade head
    exec uvicorn teampath.web.main:app --host 0.0.0.0 --port 8000 --proxy-headers \
         --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}" --ws websockets-sansio
    ;;
  worker)
    exec python -m teampath.worker.main
    ;;
  cli)
    shift
    exec python -m teampath.cli "$@"
    ;;
  *)
    exec "$@"
    ;;
esac
