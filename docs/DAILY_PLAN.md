# Ежедневный план обновления данных (Quotas_analytic)

Порядок шагов для ежедневного прогона. Скрипты можно запускать по очереди; при ошибке на одном шаге следующие всё равно имеют смысл (используют уже собранные данные).

---

## 1. Компании и группы (DaData + PostgreSQL)

```bash
cd /path/to/Quotas_analytic
python3 scripts/enrich_companies_to_db.py
```

- **Что делает:** берет компании из legacy CSV, обогащает уникальные ИНН через DaData, чистит артефакты и пишет в таблицу `companies` (PostgreSQL).
- **Результат:** upsert данных компаний в DWH.
- **Проверка лимита ФНС:** не требуется для этого шага.

---

## 2. Цербер (реестр экспортёров и судов)

**Вариант А — автоматическая выгрузка (Playwright):**

```bash
python3 scripts/cerberus_download_auto.py
```

При таймауте или изменении вёрстки можно попробовать с окном браузера: `python3 scripts/cerberus_download_auto.py --headed`

**Вариант Б — ручная выгрузка XLS с сайта:**

1. Скачать отчёт с [cerberus.vetrf.ru](https://cerberus.vetrf.ru/cerberus/certified/pub) (Рыба и морепродукты, при необходимости суда).
2. Положить файл в `data/cerberus_export_latest.xlsx`.
3. Запустить:
   ```bash
   python3 scripts/fetch_cerberus_export.py data/cerberus_export_latest.xlsx
   ```

- **Результат:** `data/cerberus_export.csv`, `output/companies_with_export.csv` (обновляются).

Цербер достаточно обновлять **раз в неделю** или по необходимости; в ежедневном прогоне можно пропускать.

---

## 3. Проливка групп в сводки

После обновления компаний и/или Цербера — пролить группы по ИНН в итоговые таблицы:

```bash
python3 scripts/fill_quota_summary_groups.py
python3 scripts/fill_companies_with_export_groups.py
```

- **Результат:** колонка «Группа_Компаний» в `output/quota_summary.csv` и `output/companies_with_export.csv`.

---

## 4. Суда: FleetPhoto (РМРС) — список и обогащение

**4.1. Кэш названий РМРС → FleetPhoto vessel_id** (при первом запуске или при обновлении списка РМРС):

```bash
python3 scripts/merge_rmrs_fleetphoto.py
```

При необходимости принудительно обновить список: `python3 scripts/merge_rmrs_fleetphoto.py --refresh`

**4.2. Обогащение карточек FleetPhoto (статус, фото, IMO, тип/проект):**

```bash
python3 scripts/enrich_fleetphoto_vessels.py
```

- **Результат:** в `data/gfw_our_vessels.json` дополняются поля `fleetphoto_status`, `fleetphoto_photo_url`, `fleetphoto_imo`, `fleetphoto_project` и др. для судов, найденных в FleetPhoto.
- **Статус:** сохраняется как на FleetPhoto (поле «Текущее состояние») — без перевода в коды: «Эксплуатируется», «Прочее», «Продан», «Утилизирован» и т.д. как есть. Тип/проект судна (`fleetphoto_project`) берётся со страницы, если есть ссылка на проект (например траулеры 1328, 394).

---

## 5. Суда: GFW (кэш для карты)

Требуется `GFW_API_TOKEN`. По умолчанию **инкрементально**: запрос к API только для судов без `gfw_id` в кэше.

```bash
export GFW_API_TOKEN=ваш_токен
python3 scripts/build_gfw_vessel_cache.py
```

- **Результат:** `data/gfw_our_vessels.json` (поле `gfw_id` для отображения на карте).
- **Полный пересбор** (редко): `python3 scripts/build_gfw_vessel_cache.py --full`

**Обогащение деталями GFW (флаг, владелец, оператор, активность рыбалки):**

```bash
python3 scripts/enrich_gfw_vessel_details.py          # только детали (flag, owner, operator, geartype)
python3 scripts/enrich_gfw_vessel_details.py --fishing  # + события рыбалки за 90 дней
python3 scripts/enrich_gfw_vessel_details.py --refresh --limit 100  # перезапросить детали для первых 100
```

- **Результат:** в `gfw_our_vessels.json` добавляются поля `gfw_flag`, `gfw_owner`, `gfw_operator`, `gfw_geartype`, `gfw_length_m`, `gfw_tonnage_gt`, при `--fishing` — `gfw_fishing_events_90d`, `gfw_last_fishing_date`. На карте они отображаются в карточке судна при наведении и в попапе.

---

## 6. Обогащение судов по компаниям из GFW (опционально)

Добавляет в базу суда, найденные в GFW по названиям компаний Цербера (если их ещё нет в выгрузке Цербера):

```bash
python3 scripts/enrich_vessels_from_gfw.py
```

С лимитом компаний (отладка): `python3 scripts/enrich_vessels_from_gfw.py --limit 20`

- **Результат:** `data/gfw_enriched_vessels.json`; на карте эти суда отображаются с пометкой «GFW».

---

## 7. Веб-интерфейс (карта и список судов)

```bash
pip install -r requirements-gfw.txt   # один раз
export GFW_API_TOKEN=...             # опционально; без токена карта без позиций
python3 web/app.py
```

Открыть в браузере: http://localhost:5000

---

## Краткий чеклист на день

| # | Шаг | Команда | Частота |
|---|-----|---------|---------|
| 1 | Компании + группы | `python3 scripts/enrich_companies_to_db.py` | Ежедневно |
| 2 | Цербер | `python3 scripts/cerberus_download_auto.py` (или ручная выгрузка) | Раз в неделю / по необходимости |
| 3 | Проливка групп | `python3 scripts/fill_quota_summary_groups.py` + `fill_companies_with_export_groups.py` | После шагов 1–2 |
| 4 | FleetPhoto | `python3 scripts/merge_rmrs_fleetphoto.py` (при необходимости), затем `enrich_fleetphoto_vessels.py` | После обновления Цербера или раз в неделю |
| 5 | GFW кэш | `python3 scripts/build_gfw_vessel_cache.py` | Инкрементально, при появлении новых судов или после 503 |
| 6 | GFW по компаниям | `python3 scripts/enrich_vessels_from_gfw.py` | По желанию |
| 7 | Веб | `python3 web/app.py` | По необходимости |

---

## Файлы данных

| Файл | Назначение |
|------|------------|
| `data/company_groups_enriched.csv` | Компании, группы, директора, учредители |
| `data/cerberus_export.csv` | Выгрузка Цербера (экспорт, суда) |
| `output/quota_summary.csv` | Сводка квот + группы |
| `output/companies_with_export.csv` | Компании с экспортом и судами + группы, FleetPhoto, GFW |
| `data/gfw_our_vessels.json` | Кэш судов Цербера → gfw_id, FleetPhoto (статус, IMO, тип) |
| `data/fns_requests_today.json` | Счётчик запросов к ФНС за день |
| `data/list_org_not_found_inns.txt` | ИНН, не найденные ни в одном источнике (для повтора через ФНС на следующий день) |
