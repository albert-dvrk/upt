#!/bin/sh
# Генерирует SSH-ключ, которым координатор заходит на рабочие серверы стенда.
# Ключ лежит в deploy/secrets/ (в .gitignore) и монтируется в контейнеры только на чтение.
set -eu

dir="$(cd "$(dirname "$0")" && pwd)/secrets"
mkdir -p "$dir"
if [ ! -f "$dir/downloader_ed25519" ]; then
    ssh-keygen -t ed25519 -N '' -C videofetch-lab -f "$dir/downloader_ed25519"
fi
chmod 600 "$dir/downloader_ed25519"
echo "Ключи готовы: $dir"
