# Документация проекта Quotas_analytic

- **[CERBERUS_EXPORT_AND_FLEET.md](CERBERUS_EXPORT_AND_FLEET.md)** — реестр Цербер (Россельхознадзор): экспортёры рыбы/морепродуктов по странам, объекты-суда, как выгружать и связывать с компаниями по ИНН.

Основные скрипты данных:
- `scripts/enrich_companies_to_db.py` — обогащение компаний через DaData и запись в `companies` (PostgreSQL).
- `scripts/cerberus_download_auto.py` — автоматическая выгрузка реестра Цербер в XLS и разбор.
- `scripts/fetch_cerberus_export.py` — разбор уже скачанного XLS Цербера и слияние с `company_groups_enriched.csv`.
