#!/bin/sh
# Применяет миграции и запускает координатор. HTTPS включается, если смонтирован сертификат.
set -eu
alembic upgrade head
if [ -f /run/tls/server.crt ] && [ -f /run/tls/server.key ]; then
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000 \
        --ssl-certfile /run/tls/server.crt --ssl-keyfile /run/tls/server.key
fi
echo "ВНИМАНИЕ: сертификат не найден, координатор работает по HTTP" >&2
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
