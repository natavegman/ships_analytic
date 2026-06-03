📌 1. Контекст и Целевая Архитектура
Проект: Аналитика распределения квот, вылова и финансов рыбопромыслового флота РФ (вкл. холдинги ОРФ, Гидрострой и др.).
Текущая проблема: Данные разрознены по CSV, Notion не справляется с ролью БД для сырых данных (АМП, GFW), парсинг компаний через веб-скрапинг выдает артефакты (капчи), невозможно отследить "Тайм-чартеры" (облов чужих квот).
Целевое решение: 1. DWH: Центральное хранилище на PostgreSQL.
2. Обогащение: Первичный сбор данных о компаниях строго через API DaData (с классификацией ролей по ОКВЭД).
3. Бизнес-логика: Внедрение таблицы quota_transfers для динамического перераспределения вылова между юрлицами.
4. Финансы: Интеграция UN Comtrade для расчета расчетной выручки.
5. Визуализация: Notion переводится в режим Read-Only дашборда (синхронизация агрегатов из БД).

🏗 2. Схема Базы Данных (SQLAlchemy 2.0+)
Файл: database/models.py

companies (Компании и Холдинги)

inn (String, PK)

name_full (String) — чистое из DaData

group_name (String) — Холдинг (ГК Гидрострой, ОРФ)

role (String) — Вычисляется по ОКВЭД (Добыча, Торговля, Банк, Строительство)

okved, status, capital (String/Float) — из DaData

director_name (String)

geo_lat, geo_lon (Float) — для дашборда на карте

dadata_last_updated (Date) — контроль кэша (лимит 10к/день)

vessels (Флот)

imo / gfw_id (String, PK / Unique)

name, project (String)

base_owner_inn (FK -> companies.inn)

quotas_limits (Лимиты из приказов)

id (PK), year (Int), owner_inn (FK), basin, species (String), volume_tons (Float)

quota_transfers (Аренда и Тайм-чартер)

id (PK), vessel_id (FK), actual_quota_owner_inn (FK), start_date, end_date (Date)

daily_catches (Фактический вылов)

id (PK), date (Date), vessel_id (FK), volume_tons (Float), source (Enum: 'AMP', 'GFW_CALC')

market_prices (Цены ВЭД из UN Comtrade)

year_month (Date), species (String), price_usd_kg (Float)

🚀 3. RoadMap для Разработки (Спринты)
Спринт 1: Инфраструктура БД и Умное Обогащение (DaData)
Цель: Поднять БД, очистить мусор в ИНН и настроить надежное обогащение.

Настроить PostgreSQL, models.py и Alembic.

Создать scripts/dadata_client.py (класс DaDataEnricher, метод get_info(inn), маппинг ролей по ОКВЭД).

Использовать `scripts/enrich_companies_to_db.py` как канонический pipeline. Логика:

Читаем ИНН. Идем в таблицу companies.

Если ИНН нет или dadata_last_updated > 30 дней -> запрос к DaData -> UPDATE базы.

Искать "Проверка контрагента" в старых CSV и автоматически исправлять через DaData.

Спринт 2: Миграция существующих данных и Трансферы
Цель: Перенести текущие CSV в базу и добавить логику подмены владельца.

Обновить etl_quota.py: после парсинга fish.gov.ru писать df.to_sql в quotas_limits.

Написать класс CatchAllocator в scripts/allocator.py.

Метод get_actual_owner(vessel_id, date). Делает SELECT в quota_transfers. Если судно в аренде — возвращает ИНН арендатора, иначе base_owner_inn.

Спринт 3: Парсеры Факта (АМП и Comtrade)
Цель: Начать собирать тонны и деньги.

Создать scripts/parser_amp.py (BeautifulSoup) для сбора графиков подходов судов, запись в daily_catches.

Создать scripts/fetch_comtrade.py. Запрос импорта Китая (HS 030367, 030614) из РФ. Запись в market_prices.

Спринт 4: Сборка P&L и выгрузка в Notion
Цель: Свести факт с лимитом и обновить дашборды.

Создать SQL View (или Python-агрегатор) v_financial_report:

(Сумма daily_catches по ИНН) * (market_prices) = Расчетная выручка.

Отрефакторить prepare_notion_import.py — он должен читать готовые агрегаты из PostgreSQL и пушить их в Notion API.