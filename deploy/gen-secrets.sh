#!/bin/sh
# Готовит секреты стенда (всё лежит в deploy/ и исключено из git):
#   secrets/downloader_ed25519[.pub]  SSH-ключ координатора для рабочих серверов
#   secrets/tls/server.crt|server.key самоподписанный сертификат координатора (SAN: coordinator, localhost, 127.0.0.1)
#   .env                              VF_SECRET_KEY (подпись сессий) и VF_ADMIN_PASSWORD (первый вход)
# Существующие файлы не перезаписываются. Перевыпуск сертификата: удалить secrets/tls и запустить снова.
set -eu

here="$(cd "$(dirname "$0")" && pwd)"
"$here/gen-keys.sh"

tls="$here/secrets/tls"
if [ ! -f "$tls/server.crt" ]; then
    mkdir -p "$tls"
    openssl req -x509 -newkey rsa:3072 -nodes -days 825 \
        -keyout "$tls/server.key" -out "$tls/server.crt" \
        -subj "/O=videofetch lab/CN=coordinator" \
        -addext "subjectAltName=DNS:coordinator,DNS:localhost,IP:127.0.0.1" 2>/dev/null
    chmod 600 "$tls/server.key"
    chmod 644 "$tls/server.crt"
    echo "Сертификат создан: $tls/server.crt"
fi

env="$here/.env"
if [ ! -f "$env" ]; then
    secret="$(openssl rand -hex 32)"
    # пароль: буквы обоих регистров и цифры, чтобы пройти проверку сложности
    pass="Vf$(openssl rand -hex 8)Aa1"
    umask 077
    {
        echo "VF_SECRET_KEY=$secret"
        echo "VF_ADMIN_PASSWORD=$pass"
    } > "$env"
    echo "Создан $env"
    echo "Логин: admin    Пароль: $pass   (сохраните; он же в deploy/.env)"
else
    echo ".env уже существует — не меняю."
fi
