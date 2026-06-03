# Quotas_analytic

Аналитика распределения квот и мониторинг конкурентов в рыбной отрасли РФ: приказы Росрыболовства, группы компаний, флот (Цербер, GFW, IMO), экспорт и AI-разбор сайтов конкурентов.

---

## AI Quota Competitor Monitor (desktop)

Desktop-приложение для парсинга сайтов конкурентов, AI-анализа и обогащения данными проекта (группы, квоты, Цербер, флот, PostgreSQL).

### Модули

| Файл | Назначение |
|------|------------|
| `main.py` | FastAPI: `/health`, `/parse`, `/monitor`, `/analyze-enrich`, `/enrich` |
| `openaiservice.py` | `/analyzetext`, `/analyzeimage` (OpenAI gpt-4o, strict JSON) |
| `parsingservice.py` | Selenium headless + извлечение PDF со страниц (`pypdf`) |
| `vesselservice.py` | Сопоставление названий судов с IMO (реестр, GFW API) |
| `enrichservice.py` | Связка с группами, квотами, Цербером, полным флотом группы, БД |
| `gui.py` | PyQt6 GUI, фоновый вызов `/monitor` |
| `build.py` | Сборка `QuotaCompetitorMonitor.app` (PyInstaller) |

### Установка

```bash
cd Quotas_analytic
python3.14 -m venv .venv314
.venv314/bin/pip install -r requirements-desktop.txt
cp .env.example .env   # OPENAI_API_KEY, опционально GFW_API_TOKEN, DATABASE_URL
```

### Запуск

**GUI** (поднимает API на `http://127.0.0.1:8000` автоматически):

```bash
.venv314/bin/python gui.py
```

**Только API:**

```bash
.venv314/bin/python main.py
# или: uvicorn main:app --reload --port 8000
```

**Сборка .app для macOS:**

```bash
.venv314/bin/python build.py
# → dist/QuotaCompetitorMonitor.app
```

### Эндпоинты

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/health` | Проверка сервера |
| POST | `/parse` | Selenium-парсинг URL → текст |
| POST | `/monitor` | Полный цикл: парсинг → AI → обогащение → снимок JSON |
| POST | `/analyzetext` | AI-анализ текста / PDF |
| POST | `/analyzeimage` | AI-анализ скриншота |
| POST | `/analyze-enrich` | AI + обогащение без парсинга URL |
| POST | `/enrich` | Обогащение готового JSON-отчёта |

### Пример `/monitor`

```bash
curl -s -X POST http://127.0.0.1:8000/monitor \
  -H "Content-Type: application/json" \
  -d '{"url": "https://okeanrybflot.ru/dislocation/"}' | jq '.company, .group_fleet_count, .vessels_with_imo'
```

Ответ включает: `company` (ИНН, группа), `group_companies`, `quotas_rosrybolovstvo`, `vessels[]` (полный флот группы + диспетчерская), `analysis`, `snapshot_path`.

### Переменные окружения (desktop)

См. `.env.example`: `OPENAI_API_KEY`, `OPENAI_MODEL`, `GFW_API_TOKEN`, `DATABASE_URL`, `NOTION_SYNC`, `NOTION_API_TOKEN`.

### Проверенные источники

- [Океанрыбflot — дислокация](https://okeanrybflot.ru/dislocation/) — PDF с таблицей судов
- [РРПК / Russian Fishery](https://russianfishery.ru/) — корпоративные PDF и стратегия

---

## ETL квот Росрыболовства

### Цель

Сводная таблица (CSV) распределения квот по группам компаний на основе приказов Росрыболовства и Минсельхоза (ОДУ, доли, инвестквоты, международные квоты) за 2023–2026 годы.

### Структура

- `src/etl_quota.py` — ETL: загрузка приказов (PDF/Excel) с `fish.gov.ru`, нормализация, агрегация, `output/quota_summary.csv`
- `data/company_groups.csv`, `data/company_groups_enriched.csv` — маппинг юрлиц → группы
- `output/quota_summary.csv` — итоговая сводка

```bash
pip install -r requirements.txt
python src/etl_quota.py
```

Колонки: `Группа_Компаний, Юр_Лицо, ИНН, Год, Бассейн, Объект_Лова, Тип_Квоты, Доля_%, Объем_Тонн, Причина_Изменения`

---

## PostgreSQL (Docker)

```bash
docker compose up -d
.venv314/bin/python scripts/init_database.py --seed-vessels
```

В `.env`: `DATABASE_URL=postgresql+psycopg2://quotas:quotas_local_dev@localhost:5432/quotas_analytic`

---

## Данные по компаниям и экспорту

- **Обогащение компаний** (ФНС, audit-it, list-org):
  ```bash
  python3 scripts/enrich_companies_to_db.py
  ```

- **Реестр Цербер** (Россельхознадзор) — экспортёры, объекты-суда:
  ```bash
  pip install -r requirements-cerberus.txt && playwright install chromium
  python3 scripts/cerberus_download_auto.py
  ```
  Ручная выгрузка XLS → `python3 scripts/fetch_cerberus_export.py data/cerberus_export_latest.xlsx`  
  Подробнее: [docs/CERBERUS_EXPORT_AND_FLEET.md](docs/CERBERUS_EXPORT_AND_FLEET.md)

- **Обновление групп в сводках** после правок CSV:
  ```bash
  python3 scripts/fill_quota_summary_groups.py
  python3 scripts/fill_companies_with_export_groups.py
  ```

### Источники данных

| Что | Источник | Обновление |
|-----|----------|------------|
| Квоты | fish.gov.ru, ETL | по приказам |
| Компании | ФНС ЕГРЮЛ, audit-it | cron / скрипты |
| Экспорт, суда | cerberus.vetrf.ru | раз в неделю |
| Позиции судов | Global Fishing Watch API | по запросу |
| VetIS.API (план) | api.vetrf.ru | реестр + эВСД «Меркурий» |

---

## Карта судов (GFW)

```bash
export GFW_API_TOKEN=...
python scripts/build_gfw_vessel_cache.py
pip install -r requirements-gfw.txt && python web/app.py
```

→ http://localhost:5000 — суда из Цербера, позиции GFW.  
Подробнее: [docs/GFW_MAP.md](docs/GFW_MAP.md)

---

## Notion

```bash
python3 scripts/prepare_notion_import.py
python3 scripts/notion_create_databases.py --create-dbs --import-data
```

Структура баз: [docs/NOTION_STRUCTURE.md](docs/NOTION_STRUCTURE.md)

---

## Документация

- [docs/DAILY_PLAN.md](docs/DAILY_PLAN.md) — ежедневный прогон ETL
- [docs/STATUS_AND_TASKS.md](docs/STATUS_AND_TASKS.md) — статус и задачи
- [docs/ARCHITECTURE_V2.md](docs/ARCHITECTURE_V2.md) — архитектура

---

## Безопасность

Секреты (`OPENAI_API_KEY`, токены GFW/Notion/PostgreSQL) хранятся только в `.env` (файл в `.gitignore`). Не коммитьте ключи в репозиторий.
