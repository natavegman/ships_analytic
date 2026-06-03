# RMRS Coverage Report

Дата: 2026-06-02

## Итог по статусам (events)

- `ok`: 9
- `ok_via_regbook`: 96
- `not_access`: 57

Всего обработано IMO: **162**

## Итог по survey-данным

- Всего строк в `rmrs_surveys_staging`: **462**
- Полные surveys из `getves` (`source_status=ok`): **366**
- Synthetic fallback из regbook (`REGBOOK.CLASS_STATUS`): **96**

## История названий

- IMO с историей имен: **162**
- Всего записей в `vessel_names_history`: **267**

## Файлы для работы

- Общий срез: `output/reports/rmrs_coverage_snapshot.csv`
- Полные surveys (`ok`): `output/reports/rmrs_ok_full_surveys.csv`
- Карточки только через regbook (`ok_via_regbook`): `output/reports/rmrs_ok_via_regbook.csv`
- Очередь `not_access` на дообогащение: `output/reports/rmrs_not_access_queue.csv`

## Рекомендованный приоритет

1. В аналитику ремонтов сначала брать `rmrs_ok_full_surveys.csv` (самые детальные данные).
2. Затем подключать `rmrs_ok_via_regbook.csv` как weaker-signal слой.
3. `rmrs_not_access_queue.csv` отправлять в fallback-пайплайн (AIS/TrustedDocks/ручной доступ RMRS).
