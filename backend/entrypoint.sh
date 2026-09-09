#!/bin/sh
set -eu

if [ "${AUTO_MIGRATE:-false}" = "true" ]; then
  alembic upgrade head
fi

if [ "$#" -gt 0 ]; then
  exec "$@"
fi

exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "${PORT:-8000}" \
  --workers "${WEB_CONCURRENCY:-2}" \
  --timeout-keep-alive "${WEB_TIMEOUT_SECONDS:-90}" \
  --proxy-headers \
  --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-127.0.0.1}"
