# Карта наших судов (Global Fishing Watch + веб-интерфейс)

Веб-интерфейс показывает **только наши суда** (из выгрузки Цербера) на карте. Позиции подгружаются из Global Fishing Watch API при наличии токена.

## Шаги

### 1. Токен GFW

- Зарегистрироваться и получить токен: https://globalfishingwatch.org/our-apis/tokens  
- Сохранить в переменную окружения: `export GFW_API_TOKEN=ваш_токен`  
- Либо положить в файл `.env` в корне проекта: `GFW_API_TOKEN=ваш_токен` (для Flask можно использовать `python-dotenv` при желании).

### 2. Кэш «наши суда → GFW id»

Скрипт читает суда из `data/cerberus_export.csv` (где Судно=1), ищет каждое в GFW по названию и сохраняет соответствие в `data/gfw_our_vessels.json`:

```bash
export GFW_API_TOKEN=...
python scripts/build_gfw_vessel_cache.py
```

Без токена скрипт выдаст ошибку (поиск судов требует авторизации).

### 3. Обогащение судами из GFW (опционально)

Скрипт ищет в GFW суда по **названиям наших компаний** (из Цербера), сверяет owner/operator в ответе API и добавляет суда, которых нет в выгрузке Цербера, в отдельный список:

```bash
export GFW_API_TOKEN=...
python scripts/enrich_vessels_from_gfw.py
# или ограничить число компаний: python scripts/enrich_vessels_from_gfw.py --limit 20
```

Результат: `data/gfw_enriched_vessels.json`. На карте и в списке эти суда отображаются вместе с судами из Цербера (в интерфейсе помечены как «GFW»).

### 4. Запуск веб-приложения

```bash
pip install -r requirements-gfw.txt
export GFW_API_TOKEN=...   # опционально; без него карта покажет только список судов
python web/app.py
```

Открыть в браузере: http://localhost:5000  

- **Список судов** (слева) — суда из Цербера (кэш) + обогащение из GFW (помечены «GFW»).  
- **Карта** — точки появляются, если задан `GFW_API_TOKEN` и для судна есть события с координатами за последние 14 дней в GFW.

### Переменные окружения

| Переменная       | Описание |
|------------------|----------|
| `GFW_API_TOKEN`  | Токен API Global Fishing Watch (обязателен для поиска судов и для позиций на карте). |
| `PORT`           | Порт веб-сервера (по умолчанию 5000). |

## Файлы

- `scripts/gfw_client.py` — клиент GFW API v3 (vessels search, vessel by id, events).  
- `scripts/build_gfw_vessel_cache.py` — построение кэша наши суда → GFW id.  
- `scripts/enrich_vessels_from_gfw.py` — обогащение: по компаниям Цербера ищем в GFW суда (owner/operator), добавляем новые в базу.  
- `data/gfw_our_vessels.json` — кэш судов из Цербера (имя, ИНН, компания, gfw_id).  
- `data/gfw_enriched_vessels.json` — суда, найденные в GFW по компаниям (нет в Цербере).  
- `web/app.py` — Flask: объединённый список судов и GeoJSON позиций.  
- `web/static/index.html` — карта (Leaflet) и боковая панель со списком судов.

## Ссылки

- [Документация GFW API](https://globalfishingwatch.org/our-apis/documentation)  
- [Токены GFW](https://globalfishingwatch.org/our-apis/tokens)
