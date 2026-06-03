# Установка и запуск enrichment (Sprint 1)

Скрипт `scripts/enrich_companies_to_db.py`:
- читает legacy CSV компаний (`data/company_groups.csv` или `COMPANY_SOURCE_CSV`),
- обогащает уникальные ИНН через DaData,
- чистит артефакты (`"Проверка контрагента..."`),
- записывает результат напрямую в PostgreSQL таблицу `companies`.

## Требования

- Python 3.10+
- Доступ к PostgreSQL (переменная `DATABASE_URL` или `POSTGRES_*`)
- Ключи DaData в `.env`:
  - `DADATA_API_KEY`
  - `DADATA_SECRET_KEY`

## Установка зависимостей

```bash
cd /Users/natalia/Code/Quotas_analytic
source .venv/bin/activate
python3 -m pip install --upgrade pip
pip install -r requirements.txt
```

## Запуск

```bash
python3 scripts/enrich_companies_to_db.py
```

Опционально можно указать другой источник CSV:

```bash
COMPANY_SOURCE_CSV=/path/to/companies.csv python3 scripts/enrich_companies_to_db.py
```

## Проверка

- В логе скрипта должна быть строка вида:
  - `Записано/обновлено компаний в PostgreSQL: N`
- В таблице `companies` обновляются поля:
  - `inn`, `name`, `group_companies`, `role_in_holding`.

