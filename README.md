# data-profiler

Инструмент **закрытого контура**: подключается к реальной БД (PostgreSQL/Greenplum),
профилирует таблицы и генерирует **синтетические, но правдоподобные** сэмплы —
чтобы на открытом контуре можно было строить тестовую БД и гонять ИИ, который
пишет SQL-ETL как будто на реальных данных.

Ключевое:
- **Профиль значений** (`profiles/*.json`): типы, min/max (не для чувствительных),
  **все категории** (если уникальных ≤ `MAX_CATEGORIES`), кардинальность, доля null,
  **PK-гипотеза** (в DDL Greenplum ключей нет — считается по сэмплу на pandas).
- **Синтетический сэмпл** (`samples/*.csv`): сохранены кардинальность, null/not-null,
  реальные категории; чувствительные поля (ИНН/ФИО/деньги) — полностью фейковые;
  корреляции внутри групп (напр. `task_subtype` ↔ `task_questionary`) не перемешиваются.
- Вся тяжёлая работа — **в pandas по сэмплу** `SELECT * WHERE random() < frac LIMIT n`
  (frac от `pg_class.reltuples`), без нагрузки на БД.

## Управление

Одна ячейка в [run_profiler.ipynb](run_profiler.ipynb): переменные + `run(locals())`.
Вся логика — в пакете `profiler/`.

```
pip install -r requirements.txt          # method="qwen" ещё требует langchain-gigachat
```

Основные переменные ячейки: `DB_URL` (или части `DB_*`), `TABLES` (`["schema.table", …]`),
`CORRELATED_GROUPS`, `MAX_CATEGORIES`, `SYNTH_ROWS`, `OUTPUT_DIR`, `LLM`.

## LLM (закрытый контур)

`method` — это **транспорт** (не модель); модель задаётся отдельно и работает в
любом транспорте. URL и токен — из env `GIGACHAT_API_URL` / `JPY_API_TOKEN`.
По умолчанию — `http` + `Qwen3.5-397b`.

```python
LLM = dict(method="http", model="Qwen3.5-397b")     # requests → /chat/completions (по умолчанию)
LLM = dict(method="http", model="glm-5.1")          # та же схема, другая модель
LLM = dict(method="gigachat", model="Qwen3.5-397b") # langchain_gigachat.GigaChat
LLM = dict(method="http", model="deepseek-chat", base_url="https://api.deepseek.com")  # тест
LLM = dict(method=None)                              # без LLM: детерминированный фейкер
```

LLM генерит **пулы** фейковых значений для free-text/чувствительных полей (в т.ч.
опросник — свой для каждого подтипа задачи). Строки набираются ресэмплом из пулов,
поэтому 1000+ строк не упираются в LLM. При недоступности LLM — фолбэк на
детерминированный фейкер (`profiler/faker.py`).

## Заглушка описаний (redirect)

Из исходного проекта: для схемы `*_sn_uzp` описания-комментарии берутся из парной
`*_sn_view` (та же таблица), для `*_sn_t_uzp` и прочих — из своей схемы.
См. `Db.comments_schema` в [profiler/db.py](profiler/db.py).

## Структура

```
profiler/
  config.py        параметры ячейки → RunConfig
  db.py            подключение, сэмплинг random()<frac, интроспекция, комментарии (redirect)
  sensitivity.py   классификатор PII/финансов по имени колонки
  profile.py       профиль (pandas): кардинальность, null%, min/max, категории, PK-гипотеза
  llm.py           клиент qwen/glm/openai-совместимый
  faker.py         офлайн-фейкер (ИНН/ФИО/деньги/…), явно синтетические значения
  synth.py         синтез: пулы + ресэмпл, корр-группы, null/кардинальность
  runner.py        оркестрация → файлы
  io.py            запись profiles/*.json, samples/*.csv, manifest.json
tests/seed.py      наполнение тест-контейнера Postgres правдоподобными данными
```

## Тест на контейнере

```bash
docker run -d --name dp_pg -e POSTGRES_PASSWORD=pass -e POSTGRES_USER=user \
  -e POSTGRES_DB=db -p 55432:5432 postgres:16
python tests/seed.py
# затем запустить ячейку тетрадки с DB_URL на localhost:55432
```
