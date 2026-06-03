# Шаблон проверки ремонтов (рыболовные суда)

Цель: по каждому IMO собрать сигналы ремонта из двух источников:
- `TrustedDocks`: справочник верфей RU/KR/CN (и далее сопоставление визитов).
- `RMRS`: события освидетельствований (`Surveys`) для судов, где есть публичный доступ.

Equasis в этом контуре не используем как основной источник (фокус здесь на рыболовных судах и профильных реестрах/верфях).

## 1) Предзагрузка верфей TrustedDocks

```bash
python3 scripts/prefetch_trusteddocks_shipyards.py \
  --countries ru kr cn \
  --out data/reference/trusteddocks_shipyards_ru_kr_cn.csv
```

Что попадет в CSV:
- `country_code`, `shipyard_id`, `shipyard_url`, `name`
- `address`, `website`, `phone`, `email`
- `lat`, `lon`

Практический смысл:
- это реестр верфей, к которому потом матчим порт-визиты (AIS/коммерческий feed).

## 2) События RMRS по IMO

```bash
python3 scripts/fetch_rmrs_events_template.py --imo 9157820
```

Если есть доступ в закрытый контур RMRS, можно передать cookie-сессию:

```bash
export RMRS_COOKIE='PHPSESSID=...; other_cookie=...'
python3 scripts/fetch_rmrs_events_template.py --imo 9157820
```

Скрипт ходит на:
- `https://rs-class.org/c/getves.php?imo=<IMO>`

И извлекает:
- `vessel_data` (включая `Class status`, `Type of vessel`, `RS Number`)
- `surveys` (тип, код, дата последнего, дата следующего, статус)

Если RMRS вернул `NOT ACCESS`, значит по этому IMO нет публичной карточки/доступа в текущем интерфейсе RMRS.

## 3) Правило интерпретации событий RMRS

`Surveys` = официальные события классификационного контроля, а не прямой акт "ремонт завершен".

Как использовать в аналитике:
- `DUE` + близкая/просроченная дата => вероятный ремонтный слот.
- `Special Periodical Survey`, `Bottom Survey`, `Propeller Shaft...` => сильный техсигнал докования.
- проверять вместе с фактическим стоянием в районе верфи (AIS).

## 4) Минимальный SQL каркас

```sql
CREATE TABLE IF NOT EXISTS vessel_repairs_signals (
    id BIGSERIAL PRIMARY KEY,
    imo BIGINT NOT NULL,
    source TEXT NOT NULL,                -- RMRS / TRUSTEDDOCKS / AIS
    event_type TEXT,
    event_code TEXT,
    event_status TEXT,
    event_date_last DATE,
    event_date_next DATE,
    shipyard_name TEXT,
    shipyard_country TEXT,
    confidence NUMERIC(3,2) DEFAULT 0.50,
    payload JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_vessel_repairs_signals_imo
    ON vessel_repairs_signals (imo);
```

## 5) Рекомендуемый batch-порядок

1. Прогнать `prefetch_trusteddocks_shipyards.py` и обновить реестр верфей.
2. Для списка IMO прогнать `batch_fetch_rmrs_to_db.py`.
3. Для IMO со статусом `not_access` использовать fallback на AIS/коммерческие shipyard visits.
4. Сложить все сигналы в `vessel_repairs_signals` и считать итоговую вероятность ремонта.

### Batch-команда (из `vessels` -> staging PostgreSQL)

```bash
# Проверочный прогон без записи в БД
python3 scripts/batch_fetch_rmrs_to_db.py --limit 50 --dry-run

# Боевой прогон с записью
python3 scripts/batch_fetch_rmrs_to_db.py --limit 200
```

Что пишет скрипт:
- таблица `rmrs_events_staging` (создается автоматически)
- upsert по `imo` (всегда хранится последний снимок RMRS)

### Второй loader: surveys по строкам

```bash
# После batch (или отдельно, если events уже в staging)
python3 scripts/load_rmrs_surveys_staging.py

# Один IMO
python3 scripts/load_rmrs_surveys_staging.py --imo 9157820

# Batch + раскладка surveys одной командой
python3 scripts/batch_fetch_rmrs_to_db.py --limit 200 --expand-surveys
```

Таблица `rmrs_surveys_staging`:
- одна строка = одно освидетельствование из блока `Surveys`
- поля: `survey_type`, `survey_name`, `survey_code`, `date_last`, `date_next`, `survey_status`, `row_css_class`
- для каждого IMO старые строки удаляются и вставляются заново (идемпотентно)
