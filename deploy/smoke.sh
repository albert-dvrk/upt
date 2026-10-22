#!/usr/bin/env bash
# Сквозная проверка стенда: 3 сервера → развёртывание агентов → задание на [0,299] → ожидание завершения.
# Запуск (после docker compose up -d --build):  ./smoke.sh
set -euo pipefail
API=${API:-http://localhost:8001}
j() { python3 -c "import sys,json;d=json.load(sys.stdin);print(eval(sys.argv[1]))" "$1"; }
post() { curl -fsS -X POST "$API$1" -H 'Content-Type: application/json' ${2:+-d "$2"}; }

ids=()
for n in 01 02 03; do
  r=$(curl -sS -X POST "$API/api/workers" -H 'Content-Type: application/json' \
    -d "{\"name\":\"worker-$n\",\"host\":\"worker-$n\",\"ssh_user\":\"downloader\",\"ssh_key_ref\":\"/run/secrets/worker-key\",\"storage_path\":\"/data/videos\",\"threads\":8,\"min_free_space_gb\":0}")
  id=$(echo "$r" | j "d.get('id') or ''")
  if [ -z "$id" ]; then id=$(curl -fsS "$API/api/workers" | j "[w['id'] for w in d if w['name']=='worker-$n'][0]"); fi
  ids+=("$id")
  echo "worker-$n id=$id"
  post "/api/workers/$id/agent/deploy" >/dev/null && echo "  развёрнут"
  post "/api/workers/$id/agent/start" | j "d['state']" | sed 's/^/  агент: /'
done

workers=$(printf '{"worker_id":%s},' "${ids[@]}"); workers="[${workers%,}]"
job=$(post /api/jobs "{\"name\":\"smoke\",\"range\":\"[0,299]\",\"chunk_size\":50,\"workers\":$workers}" | j "d['id']")
echo "job id=$job"; post "/api/jobs/$job/start" >/dev/null
for _ in $(seq 1 120); do
  st=$(curl -fsS "$API/api/jobs/$job")
  echo "$st" | j "d['status'], d['stats']['processed_ids'], d['stats']['items']"
  [ "$(echo "$st" | j "d['status']")" = completed ] && break
  sleep 2
done
echo "Готово. Файлы: docker compose exec worker-01 ls /data/videos"
