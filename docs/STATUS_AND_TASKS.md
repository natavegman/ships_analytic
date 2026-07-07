# Статус проекта и каркас задач (Quotas_analytic)

Краткий срез: что есть, что делать, как запускать.

---

## Что за проект

Аналитика квот и флота: компании, суда (Цербер, FleetPhoto, GFW), квоты, карта судов.

**Источники:** Цербер (реестр экспортёров/судов), ФНС ЕГРЮЛ / list-org (компании), FleetPhoto (фото, проект судна), Global Fishing Watch (позиции, события), расчёты 2026 (calculations.fish.gov.ru), квоты (quota_summary).

---

## Текущий статус (срез)

| Что | Статус |
|-----|--------|
| **Данные** | `gfw_our_vessels.json` — суда; квоты, компании, группы, Цербер, FleetPhoto подключаются. |
| **Веб-карта** | Список судов с поиском, фильтрами (группа, проект FleetPhoto, район промысла), сокращённые названия компаний (ООО, АО). Название судна и проект FleetPhoto разделены. |
| **Точки на карте** | Позиции из GFW (fishing/port events за 30 дней). Если точек нет — смотреть `/api/debug` (`positions_from_gfw`). |
| **Обогащение GFW** | Только события: траление, заходы в порт, перегрузки (без vessel details). |
| **GFW Fleet Analytics** | Модуль эффективности флота по CSV events: KPI, вылов×GFW, набор груза, ремонты, RMRS. Пилот: НБАМР (7 судов). См. `docs/GFW_FLEET_ANALYTICS.md`. |

---

## Каркас задач (пайплайн)

### 1. Компании и группы

**Основной скрипт (DataNewton API — рекомендуется):**
```bash
# Добавить в .env: DATANEWTON_API_KEY=ваш_ключ
python3 scripts/enrich_via_datanewton.py              # полный цикл
python3 scripts/enrich_via_datanewton.py --links-only # только граф связей
python3 scripts/enrich_via_datanewton.py --dry-run    # показать что будет сделано
```
DataNewton: 200 запросов/мин, структурированный JSON, граф связей до 2-го уровня.

**Спринт 1 скрипт (DaData -> PostgreSQL):**
```bash
python3 scripts/enrich_companies_to_db.py
```

**ИИ-разбор конфликтов и недостающих групп (перед ручной проверкой):**
```bash
python3 scripts/resolve_company_groups_ai.py --dry-run   # план без вызовов API
python3 scripts/resolve_company_groups_ai.py              # разбор через OpenAI (нужен OPENAI_API_KEY в .env)
```
propagate_by_director() в enrich_via_datanewton.py останавливается там, где один
директор формально относится к нескольким известным группам (частое явление —
номинальный/профессиональный директор). Этот скрипт разбирает такие конфликты и
неназначенные группы через LLM, используя только данные из enriched CSV (директор,
ИНН директора, учредители, связанные компании, адрес, известные группы с примерами
компаний) — без домыслов. Уверенные случаи назначаются сразу, неоднозначные явно
помечаются `Требует проверки (ИИ): <причина>` в Комментарии — чтобы в ручной
проверке ниже оставались только они, а не весь список. Заодно чистит накопленные
дубли текста `Конфликт групп по директору (...)` в Комментарии.

**Ручное исправление групп (только то, что реально нужно проверить руками):**
```bash
python3 scripts/export_company_groups_for_manual_edit.py   # → data/company_groups_manual_edit.csv
# отредактировать в Excel/Sheets колонки Группа_Компаний, Комментарий
python3 scripts/import_company_groups_from_manual_edit.py  # обновляет enriched + company_groups.csv
```

### 2. Цербер
```bash
python3 scripts/cerberus_download_auto.py
# или ручная выгрузка + fetch_cerberus_export.py
```
Реестр экспортёров/судов → `data/cerberus_export.csv`.

### 3. Проливка групп
```bash
python3 scripts/fill_quota_summary_groups.py
python3 scripts/fill_companies_with_export_groups.py
```
Колонка «Группа_Компаний» в квотах и в компаниях с экспортом.

### 4. FleetPhoto (суда)
```bash
python3 scripts/merge_rmrs_fleetphoto.py
python3 scripts/enrich_fleetphoto_vessels.py
```
Проект судна, фото, статус → `gfw_our_vessels.json`.

### 5. GFW — кэш судов
```bash
export GFW_API_TOKEN=...
python3 scripts/build_gfw_vessel_cache.py
```
Сопоставление с GFW, `gfw_id` → `gfw_our_vessels.json`.

### 6. GFW — события (траление, порты, перегрузки)
```bash
python3 scripts/enrich_gfw_vessel_details.py
```
События за 90 дней в том же JSON (без флага/владельца).

### 6b. GFW Fleet Analytics (эффективность флота, вылов, продажи)
```bash
# Положить CSV выгрузки GFW events в data/nbamr_events/ (или свою папку)
# Справочник вылова: data/reference/nbamr_vessel_catch.csv
.venv/bin/python scripts/gfw_fleet_analytic.py --input-dir data/nbamr_events --rmrs-dir output
# Одиночное судно — ремонты/докования:
.venv/bin/python scripts/gfw_repairs_analytic.py --input "data/nbamr_events/ALEXANDR BELYAKOV(RUS)-events-....csv"
```
Результат: `output/gfw_fleet/` (scorecard, benchmark, yearly, encounters).  
Документация: `docs/GFW_FLEET_ANALYTICS.md`. Правило для AI: `.cursor/rules/gfw-fleet-analytics.mdc`.

### 7. Квоты 2026 (при необходимости)
```bash
python3 scripts/fetch_calculations_2026_quotas.py
```
Расчёты 2026 в пайплайн квот.

### 8. Веб
```bash
python3 web/app.py
# или PORT=5001 python3 web/app.py
```
Карта: список, фильтры, поиск, район промысла из квот, группы из `company_groups.csv`. Позиции с GFW при наличии токена.

### 9. Notion — рабочая база флота
```bash
# Подготовить CSV для импорта (компании, суда, квоты + шаблоны оборудования):
python3 scripts/prepare_notion_import.py

# Автоматическое создание баз через Notion API:
python3 scripts/notion_create_databases.py --create-dbs --import-data
```
9 взаимосвязанных баз: Суда, Компании, Квоты, Лебедки, Спутниковые системы, Контроль трала, Доп. оборудование, Вылов/Продукция, Заявки ЗИП. Подробнее: `docs/NOTION_STRUCTURE.md`.

---

## Основные файлы данных

| Файл | Назначение |
|------|------------|
| `data/company_groups.csv` | Группы судовладельцев (для фильтра на карте). |
| `data/company_groups_enriched.csv` | Компании + группы + обогащение. |
| `data/cerberus_export.csv` | Цербер: суда, экспортёры. |
| `data/gfw_our_vessels.json` | Суда + gfw_id, FleetPhoto, события GFW. |
| `data/gfw_enriched_vessels.json` | Доп. суда из GFW по компаниям. |
| `output/quota_summary.csv` | Квоты + группы; район промысла (бассейн) для карты. |
| `output/companies_with_export.csv` | Компании с экспортом и судами. |
| `data/nbamr_events/` | GFW events CSV по флоту НБАМР (2012→2027). |
| `data/reference/nbamr_vessel_catch.csv` | Тип судна, вылов сезона, трюм (редактируемый). |
| `output/gfw_fleet/` | Scorecard, benchmark, yearly, encounters. |
| `output/rmrs_events_<IMO>.json` | Кэш RMRS для классового прогноза. |

---

## Ежедневный чеклист

| # | Шаг | Команда | Частота |
|---|-----|---------|---------|
| 1 | Компании + группы | `enrich_companies_to_db.py` | Ежедневно |
| 1b | ИИ-разбор конфликтов групп | `resolve_company_groups_ai.py` | После 1, при новых конфликтах |
| 2 | Цербер | `cerberus_download_auto.py` или ручная выгрузка | Раз в неделю |
| 3 | Проливка групп | `fill_quota_summary_groups.py` + `fill_companies_with_export_groups.py` | После 1–2 |
| 4 | FleetPhoto | `merge_rmrs_fleetphoto.py`, затем `enrich_fleetphoto_vessels.py` | После Цербера / раз в неделю |
| 5 | GFW кэш | `build_gfw_vessel_cache.py` | Инкрементально |
| 6 | GFW события | `enrich_gfw_vessel_details.py` | По желанию |
| 7 | Веб | `python3 web/app.py` | По необходимости |
| 8 | Notion импорт | `prepare_notion_import.py` → `notion_create_databases.py` | При обновлении данных |

---

## Диагностика

- **Веб:** http://localhost:5001/api/debug — пути, наличие файлов, число судов, число позиций GFW.
- **Авто-проверка «чего не хватает на сегодня»:**
  - `python3 tests/check_today.py` — отчёт в консоль (что есть, чего не хватает).
  - `pytest tests/check_today.py -v` — тесты (падение = нет обязательных файлов/структуры).

См. также: [DAILY_PLAN.md](DAILY_PLAN.md), [CERBERUS_EXPORT_AND_FLEET.md](CERBERUS_EXPORT_AND_FLEET.md).
