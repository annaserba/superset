# Apache Superset — локальный стенд на Яндекс.Диске

Проект Superset перенесён с `~/superset` сюда, превращён в git-репозиторий
(клон `apache/superset` + форк на GitHub) и **починен**: три независимых
дефекта, из-за которых инстанс не показывал данные.

| Что | Значение |
|---|---|
| Каталог | `~/Yandex.Disk.localized/Projects/apache-superset` |
| UI | http://localhost:8088 |
| Логин | `admin` / пароль из `deploy/.env` (сейчас `admin`) |
| Версия | Apache Superset **6.1.0** |
| Метаданные | PostgreSQL 17, база `superset_metadata` |
| Данные | PostgreSQL 17, база `superset` (таблицы `sales`, `employees`) |
| Кэш / лимиты | Redis 7 |
| Форк | https://github.com/annaserba/superset (`parent: apache/superset`) |
| Upstream | https://github.com/apache/superset |

---

## 1. Что было сломано и как починено

### Дефект №1 — опечатка в имени переменной: метаданные считались «не в том месте»

```yaml
# было в ~/superset/docker-compose.yml
SUPERSET__DATABASE__SQLALCHY_URI: postgresql+psycopg2://superset:superset@db:5432/superset
#                              ^ пропущена буква E (SQLALCHEMY)
```

Superset читает конфигурацию из `SUPERSET__<СЕКЦИЯ>__<КЛЮЧ>`. Неизвестное имя
молча игнорируется, поэтому URI никогда не применялся, а CLI-команды
(`superset init`, `superset fab …`), запущенные без env контейнера, работали
с дефолтом — **SQLite** в `/app/superset_home/superset.db`. Отсюда путаница:
в Postgres-томе лежали одни метаданные, в SQLite — другие, а часть команд
правила не тот файл.

Починено: конфигурация задаётся явно (`deploy/superset_config.py` +
`SUPERSET_METADATA_DB_URI` в `deploy/docker-compose.yml`), метаданные живут в
отдельной базе `superset_metadata`, а не в той, где лежат данные.

### Дефект №2 — `superset init` не выполнялся: 403 Forbidden по всему UI

Официальный вход в контейнер (`/app/docker/docker-init.sh`) делает
`superset db upgrade` + `superset fab create-admin` + `superset init`.
Прежний минимальный compose запускал `run-server.sh` напрямую, без init,
поэтому роль `Admin` получила только те права, что успели создаться:

| | было | стало |
|---|---|---|
| `ab_permission_view` | 30 | 234 |
| прав у роли `Admin` | 30 | 226 |
| роли | Admin, Public | Admin, Public, Alpha, Gamma, sql_lab |

Именно это давало `403 Forbidden` в логах и в браузере (`/api/v1/me/` → 403,
`/api/v1/log/recent_activity/` → 403, баннер «Forbidden» на главной).
В новом стеке права синхронизирует one-shot сервис `superset-init`.

### Дефект №3 — чарты legacy-формата: «Chart's query context does not exist»

Пять пользовательских чартов были сохранены старым explore-эндпоинтом:

* `query_context` = NULL, поэтому Superset 6 не мог их отрисовать
  (`Chart's query context does not exist`), а плитки на дашборде оставались
  пустыми;
* `granularity_sqla` содержал не колонку, а гранулярность (`"P1M"`), из-за
  чего SQL получался вида `GROUP BY "P1M"` → `column "P1M" does not exist`;
* размерности дублировались в `groupby` и `x_axis` → `Duplicate column/metric
  labels: "product"`;
* метрики были ad-hoc и после экспорта/импорта терялись (в датасете остаётся
  только `count`), а метрика с меткой вида `"Revenue"` подставлялась в SQL как
  имя метрики и не находилась.

Починено: `query_context` пересобран, параметры приведены к формату, который
пишет сам Superset (`x_axis` + `time_grain_sqla` + `groupby` + `metrics`),
метрики стали SIMPLE ad-hoc объектами с выражением в `label`
(`SUM(revenue)`, `AVG(salary)`, `COUNT(id)`).

### Сопутствующие дефекты развёртывания

| Дефект | Решение |
|---|---|
| `image: apache/superset:latest` — любое обновление меняет версию | тег `6.1.0` (`SUPERSET_VERSION`) |
| `SUPERSET_SECRET_KEY` захардкожен в compose | `deploy/.env` (в git не попадает, `chmod 600`) |
| нет Redis → кэш и лимиты запросов в памяти каждого воркера | сервис `redis` + `CACHE_CONFIG`, `RATELIMIT_STORAGE_URI` |
| нет `superset-init` → нет миграций и синхронизации прав | one-shot сервис `superset-init` |
| нет healthcheck у приложения | healthcheck на `/health`, `restart: unless-stopped` |
| в образе нет драйвера Postgres и нет компилятора | тонкий образ `deploy/Dockerfile` (psycopg2-binary + pg8000 через `uv`) |
| `recent activity` давал баннер Forbidden | `DASHBOARD_RECENT_ACTIVITY = False` |
| `/static/service-worker.js` → 404 (шум в консоли) | известное поведение, smoke-тест допускает 404 |
| порт 5432 был опубликован наружу | база доступна только внутри сети compose |
| один том Postgres на два стека (риск порчи данных) | тестовый стек удалён, `name: superset` фиксирует имя проекта |
| старый `~/superset/docker-compose.yml` вводил в заблуждение | переименован в `~/superset/docker-compose.yml.disabled` |

---

## 2. Структура

```
apache-superset/                  # клон apache/superset, ветка master-fork
├── deploy/                       # всё локальное развёртывание
│   ├── docker-compose.yml        # redis + db + superset-init + superset + worker
│   ├── superset_config.py        # кэш, celery, безопасность, лимиты, локаль
│   ├── Dockerfile                # psycopg2-binary (в базовом образе его нет)
│   ├── docker-entrypoint-initdb.d/01-create-databases.sql
│   ├── .env / .env.example       # секреты (в git только шаблон)
│   ├── smoke_test.py             # проверка живого стенда (см. §5)
│   ├── Makefile                  # up/down/logs/smoke/backup/restore/…
│   └── tools/
│       ├── screenshot.mjs                 # скриншот UI через headless Chrome
│       ├── modernize_charts.py            # нормализация метрик и фильтров
│       └── rebuild_query_context.py       # пересборка query_context
├── superset/                     # исходники (python)
└── superset-frontend/            # исходники (react)
```

Резервные копии прежнего состояния:
`~/Documents/deepseek-harness/default-workspace/.superset-backup/`

| Файл | Что внутри |
|---|---|
| `docker-compose.yml.orig` | исходный сломанный compose |
| `old_sqlite_metadata_20261010.db` | SQLite-метаданные прежнего инстанса |
| `old_postgres_data_20261010.sql` | дамп прежнего Postgres (метаданные + `sales`/`employees`) |
| `assets_export_20261010.zip` | экспорт всех ассетов Superset (формат v1, переносится между инстансами) |
| `superset_home.tgz`, `superset_db_20261010.sql` | более ранние снимки |

---

## 3. Запуск

```bash
cd ~/Yandex.Disk.localized/Projects/apache-superset/deploy
cp .env.example .env       # если файла нет
$EDITOR .env               # SUPERSET_SECRET_KEY обязателен
docker compose up -d --build
./smoke_test.py            # проверка: health, логин, ассеты, выполнение чартов
open http://localhost:8088
```

`make up`, `make logs`, `make smoke`, `make backup` — то же самое короче.

Первый старт: `superset-init` применяет миграции, создаёт `admin`, синхронизирует
роли и права. Примеры Superset (`sales`, `employees` и ~100 демо-чартов) грузятся
командой:

```bash
docker compose exec superset /app/.venv/bin/superset load_examples
```

## 4. Ежедневные операции

```bash
docker compose up -d / down          # поднять / остановить (данные в томах)
docker compose logs -f superset      # логи приложения
docker compose exec superset superset init        # если «пропали» права (403)
docker compose exec superset superset fab list-users
docker compose exec db psql -U superset -d superset_metadata   # метаданные
docker compose exec db psql -U superset -d superset            # данные

make backup      # pg_dump метаданных + tar тома superset_home в deploy/backups/
make restore FILE=backups/superset_metadata_*.sql
```

Обновление версии:

```bash
sed -i '' 's/^SUPERSET_VERSION=.*/SUPERSET_VERSION=6.2.0/' .env
docker compose up -d --build     # superset-init применит миграции
```

## 5. Проверка работоспособности

```bash
./smoke_test.py                       # 8 чартов (по умолчанию) + все проверки
./smoke_test.py --all-charts          # выполнить все чарты (~108)
node tools/screenshot.mjs http://localhost:8088/superset/dashboard/10/ /tmp/dash.png
```

`smoke_test.py` проверяет: `/health`, отдачу фронтенда, 404 сервис-воркера,
логин, `/api/v1/me/`, наличие дашбордов/чартов/датасетов/подключений, тест
каждого подключения и **реальное выполнение чартов** через
`PUT /api/v1/chart/warm_up_cache` (он прогоняет запрос чарта и возвращает
`viz_error`, если что-то не так). Сырой `POST /api/v1/chart/data` для этого не
годится: он требует, чтобы вызывающий сам собрал query object, и отвечает
`Request is incorrect` на валидные запросы.

## 6. Если чарты снова показывают ошибку данных

Симптом: дашборд открывается, но в плитках «Data error».

```bash
# 1. посмотреть, что именно вернул Superset
./smoke_test.py --all-charts | grep FAIL

# 2. пересобрать query_context у чартов без него
docker cp tools/rebuild_query_context.py superset-superset-1:/tmp/rq.py
docker exec superset-superset-1 /app/.venv/bin/python /tmp/rq.py

# 3. если чарт пришёл из старой версии Superset — нормализовать метрики и фильтры
docker cp tools/modernize_charts.py superset-superset-1:/tmp/mc.py
docker exec superset-superset-1 /app/.venv/bin/python /tmp/mc.py --chart-id 104
```

Инструменты идемпотентны и умеют `--dry-run`.

## 7. Работа с форком

```bash
cd ~/Yandex.Disk.localized/Projects/apache-superset
git fetch upstream                 # свежий upstream
git checkout master-fork
git rebase upstream/master         # или merge
git checkout -b fix/my-change
git commit -am "fix: ..."
git push origin fix/my-change      # PR в apache/superset при желании
```

* `origin` → `https://github.com/annaserba/superset.git` (форк, сюда пушим);
* `upstream` → `https://github.com/apache/superset.git` (только чтение);
* `core.fileMode=false` — Яндекс.Диск не сохраняет POSIX-права бит-в-бит.

## 8. Особенности Яндекс.Диска

* Клиент синхронизирует и `.git`, поэтому история доступна с других машин; но
  не запускайте два `git` одновременно и не коммитьте во время активной
  синхронизации больших изменений (возможны конфликты `index`/`index.lock`).
* Если синхронизация мешает, исключите `.git` в настройках Яндекс.Диска:
  история останется локально, а форк — на GitHub.
* Docker-тома Яндекс.Диск не синхронизирует: данные Superset живут в Docker,
  а не в файлах проекта. Бэкап — `make backup`.

## 9. Границы

* Продакшн-конфигурация (TLS, reverse proxy, внешний Postgres, алерты) не
  настраивалась: официальный Superset прямо предупреждает, что docker compose
  для продакшна не предназначен.
* Async SQL Lab / алерты и отчёты: сервис `superset-worker` в стеке есть, но
  `ASYNC_QUERIES` и `ALERT_REPORTS` выключены в `superset_config.py`; включать
  вместе с `allow_run_async` у подключения к БД.
* Сборка фронтенда из исходников (`superset-frontend`) не выполнялась —
  используется готовый бандл из образа. Для неё нужны node/pnpm и ~20 минут.
* 4 дашборда `[ untitled dashboard ]` остались пустыми, как и были: в них
  изначально нет ни одной плитки. Чарты, которые к ним не привязаны, лежат в
  списке Charts.
