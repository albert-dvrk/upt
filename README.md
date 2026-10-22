# videofetch — распределённая система обнаружения и загрузки видеоматериалов

Лабораторная работа №1. Координатор раздаёт агентам небольшие арендуемые блоки
диапазона идентификаторов; агенты (запускаются на рабочих серверах по SSH) проверяют
идентификаторы и скачивают найденные файлы.

**Целевой сервис в этой реализации — собственный mock-хостинг** (`mockserver/`).
Система не обращается к сторонним сайтам: единственный источник данных — mock,
адрес которого задаётся в настройках задания.

## Структура

```
coordinator/   API, логика, модели БД, миграции, тесты координатора
agent/         агент (заливается на рабочие серверы по SSH)
mockserver/    управляемый mock-хостинг: 200/403/404/429/500, задержки, обрывы, Range
deploy/        Docker Compose стенд: postgres, mock, coordinator, 3 SSH-воркера
docs/          диаграммы PlantUML, описание API, руководство оператора, отчёт
loadtests/     нагрузочные тесты
```

## Быстрый старт

Репозиторий лучше держать в файловой системе WSL (`~/videofetch`), а не в `/mnt/c/...`:
иначе права на SSH-ключ будут некорректными.

```bash
# 1. зависимости для разработки и тестов
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt

# 2. тесты
pytest

# 3. стенд
cd deploy
./gen-keys.sh
docker compose up --build
```

После запуска: координатор — http://localhost:8001/healthz (Swagger: /docs), mock — http://localhost:8080/.

## Первая миграция БД

Модели описаны в `coordinator/app/models.py`. Начальную ревизию создаёт Alembic
из моделей; выполняется один раз, результат коммитится в репозиторий:

```bash
cd coordinator
VF_DATABASE_URL=sqlite:////tmp/vf.db alembic revision --autogenerate -m "initial schema" --rev-id 0001
```

Затем в созданный файл нужно добавить заполнение таблицы `roles`
(`viewer`, `operator`, `administrator`) и проверить `alembic upgrade head` / `downgrade base`
на PostgreSQL из стенда.

## Управление mock-сервисом

```bash
curl -X POST localhost:8080/_control/rate_limit -H 'content-type: application/json' -d '{"seconds": 30}'  # 429 на 30 с
curl -X POST localhost:8080/_control/config     -H 'content-type: application/json' -d '{"delay_ms": 500}'
curl -X POST localhost:8080/_control/config     -H 'content-type: application/json' -d '{"drop_permille": 300}'  # обрывы загрузок
curl localhost:8080/_control/stats
curl -X POST localhost:8080/_control/reset
```

## Статус

Неделя 1: каркас, парсер диапазона и блоки, статусы и переходы задания, схема БД,
mock-сервис, Compose-стенд. Дальше — API координатора, аренда блоков и агент (неделя 2).

## API недели 2

- `/api/workers` — серверы (создание, правка, `register`, `heartbeat`, `lease`, `progress`, `complete`, `fail`, `metrics`).
- `/api/jobs` — задания (создание, `start/pause/resume/stop`, `PATCH` потоков и лимитов на лету, `items`, `chunks`, `events`).
- Протокол аренды блоков: агент берёт блок `lease`, шлёт `progress` (чекпоинт `next_id` + результаты), завершает `complete`. Повторные сообщения идемпотентны; при потере аренды ответ `409 lease_lost`.
- Цель задания ограничена списком `VF_ALLOWED_TARGET_HOSTS` (по умолчанию только mock).
- Тесты PostgreSQL: `VF_TEST_DATABASE_URL=postgresql+psycopg://videofetch:videofetch@localhost:5433/videofetch_test pytest coordinator/tests/test_pg_concurrency.py`.

## Развёртывание агентов по SSH

- `POST /api/workers/{id}/agent/check|deploy|start|stop`, `GET /api/workers/{id}/agent` — координатор заходит на сервер по ключу (`ssh_key_ref`, файл только внутри `/run/secrets`), заливает пакет агента архивом через stdin и запускает его в фоне (`agent.pid`, `agent.log`).
- Первый запуск всего стенда одной командой: `cd deploy && ./smoke.sh`.
