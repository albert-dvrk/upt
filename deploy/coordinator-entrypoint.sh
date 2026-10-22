#!/bin/sh
# Применяет миграции и запускает координатор. HTTPS включается, если смонтирован сертификат.
set -eu
alembic upgrade head
WORKERS="${VF_WORKERS:-1}"
if [ "$WORKERS" -gt 1 ] && [ -z "${VF_SECRET_KEY:-}" ]; then
    echo "VF_WORKERS>1 требует заданный VF_SECRET_KEY (общий ключ сессий для всех процессов)" >&2
    exit 1
fi
if [ -f /run/tls/server.crt ] && [ -f /run/tls/server.key ]; then
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers "$WORKERS" \
        --ssl-certfile /run/tls/server.crt --ssl-keyfile /run/tls/server.key
fi
echo "ВНИМАНИЕ: сертификат не найден, координатор работает по HTTP" >&2
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers "$WORKERS"
