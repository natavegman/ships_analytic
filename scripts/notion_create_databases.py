#!/usr/bin/env python3
"""
Создание баз данных Notion через API и импорт данных.

Требования:
    1. Создать интеграцию: https://www.notion.so/my-integrations
    2. Скопировать Internal Integration Secret
    3. Добавить в .env: NOTION_API_TOKEN=secret_xxx
    4. Создать страницу-хаб в Notion, подключить к ней интеграцию (Share → Invite)
    5. Скопировать ID страницы из URL (32 символа после последнего /)
    6. Добавить в .env: NOTION_PARENT_PAGE_ID=xxx

Запуск:
    # Только создать структуру баз (без данных):
    python3 scripts/notion_create_databases.py --create-dbs

    # Создать структуру + импортировать данные:
    python3 scripts/notion_create_databases.py --create-dbs --import-data

    # Только импорт (если базы уже созданы, ID в .env):
    python3 scripts/notion_create_databases.py --import-data
"""

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("pip install requests")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

ROOT = Path(__file__).resolve().parent.parent
IMPORT_DIR = ROOT / "notion_import"
ENV_PATH = ROOT / ".env"

NOTION_VERSION = "2022-06-28"
BASE_URL = "https://api.notion.com/v1"

# Rate limit: Notion allows ~3 requests/second
RATE_LIMIT_DELAY = 0.35


def get_token():
    token = os.environ.get("NOTION_API_TOKEN", "")
    if not token:
        sys.exit("NOTION_API_TOKEN not set. See --help.")
    return token


def get_parent_page_id():
    pid = os.environ.get("NOTION_PARENT_PAGE_ID", "")
    if not pid:
        sys.exit("NOTION_PARENT_PAGE_ID not set. See --help.")
    return pid.replace("-", "")


def headers():
    return {
        "Authorization": f"Bearer {get_token()}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }


def api_post(endpoint, payload, retries=5):
    url = f"{BASE_URL}/{endpoint}"
    for attempt in range(retries):
        try:
            resp = requests.post(url, headers=headers(), json=payload, timeout=30)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            wait = min(2 ** attempt, 30)
            print(f"  Connection error (attempt {attempt+1}/{retries}), retrying in {wait}s...")
            time.sleep(wait)
            continue
        if resp.status_code == 429:
            wait = float(resp.headers.get("Retry-After", 2))
            print(f"  Rate limited, waiting {wait}s...")
            time.sleep(wait)
            continue
        if resp.status_code == 502 or resp.status_code == 503:
            wait = min(2 ** attempt, 30)
            print(f"  Server error {resp.status_code}, retrying in {wait}s...")
            time.sleep(wait)
            continue
        if resp.status_code >= 400:
            print(f"  ERROR {resp.status_code}: {resp.text[:300]}")
            if resp.status_code == 400:
                return None
            if attempt < retries - 1:
                time.sleep(1)
                continue
            resp.raise_for_status()
        time.sleep(RATE_LIMIT_DELAY)
        return resp.json()
    return None


# ---------------------------------------------------------------------------
# Database schemas
# ---------------------------------------------------------------------------

def select_opts(values):
    return [{"name": v} for v in values]


DATABASES = {
    "companies": {
        "title": "Компании",
        "icon": "🏢",
        "properties": {
            "Название": {"title": {}},
            "ИНН": {"rich_text": {}},
            "ОГРН": {"rich_text": {}},
            "Группа компаний": {"select": {"options": []}},
            "Статус": {"select": {"options": select_opts([
                "Действует", "Ликвидирована", "Исключена",
            ])}},
            "Директор": {"rich_text": {}},
            "Контакты": {"rich_text": {}},
            "Регион": {"select": {"options": []}},
            "Цербер — страны экспорта": {"rich_text": {}},
            "Кол-во судов (Цербер)": {"number": {"format": "number"}},
        },
    },
    "vessels": {
        "title": "Суда",
        "icon": "🚢",
        "properties": {
            "Название судна": {"title": {}},
            "Бортовой номер": {"rich_text": {}},
            "IMO": {"rich_text": {}},
            "Состояние": {"select": {"options": select_opts([
                "Эксплуатация", "Ремонт", "Отстой",
            ])}},
            "Тип/Модель (проект)": {"select": {"options": select_opts([
                "СРТМ", "БАТМ", "БМРТ", "СТР", "РТМКС", "РТМС", "РТМ",
                "РТ", "СТМ", "ТР", "ПБ", "СДС", "СКТР", "РС", "МРТР",
            ])}},
            "Год постройки": {"number": {"format": "number"}},
            "Регион работы": {"select": {"options": select_opts([
                "Северный", "Дальневосточный", "Норвежский", "Прочий",
            ])}},
            "GFW ID": {"rich_text": {}},
            "GFW Name": {"rich_text": {}},
            "Регион регистрации": {"rich_text": {}},
            "Примечания": {"rich_text": {}},
        },
    },
    "quotas": {
        "title": "Квоты",
        "icon": "📊",
        "properties": {
            "Запись": {"title": {}},
            "Год": {"select": {"options": select_opts(["2023", "2024", "2025", "2026"])}},
            "Бассейн": {"select": {"options": select_opts([
                "Северный", "Дальневосточный", "Норвежский",
            ])}},
            "Объект лова": {"select": {"options": []}},
            "Тип квоты": {"select": {"options": select_opts([
                "Промышленная", "Инвестиционная", "Международная",
            ])}},
            "Доля, %": {"number": {"format": "percent"}},
            "Объем, тонн": {"number": {"format": "number"}},
            "Компания": {"rich_text": {}},
            "Компания ИНН": {"rich_text": {}},
            "Группа компаний": {"select": {"options": []}},
            "Дата начала договора": {"rich_text": {}},
            "Дата окончания договора": {"rich_text": {}},
            "Причина изменения": {"rich_text": {}},
        },
    },
    "winches": {
        "title": "Лебедки",
        "icon": "⚙️",
        "properties": {
            "Название / Маркировка": {"title": {}},
            "Судно (имя)": {"rich_text": {}},
            "Тип лебедки": {"select": {"options": select_opts([
                "Ваерная", "Вспомогательная", "Гиневая", "Кабель-зонда",
                "Траловая", "Грузовая", "Швартовная", "Якорная", "Прочая",
            ])}},
            "Производитель": {"rich_text": {}},
            "Модель": {"rich_text": {}},
            "Серийный номер": {"rich_text": {}},
            "Состояние": {"select": {"options": select_opts([
                "В работе", "Требует ремонта", "На ремонте", "Выведена из эксплуатации",
            ])}},
            "Дата последнего ТО": {"date": {}},
            "Примечания": {"rich_text": {}},
        },
    },
    "satellite_systems": {
        "title": "Спутниковые системы",
        "icon": "📡",
        "properties": {
            "Название": {"title": {}},
            "Судно (имя)": {"rich_text": {}},
            "Тип системы": {"select": {"options": select_opts([
                "VSAT", "FBB", "Starlink", "OneWeb",
            ])}},
            "Вид оборудования": {"select": {"options": select_opts([
                "Антенна", "Подпалубное оборудование", "Модем/Роутер", "Прочее",
            ])}},
            "Производитель": {"rich_text": {}},
            "Модель": {"rich_text": {}},
            "Серийный номер": {"rich_text": {}},
            "Состояние": {"select": {"options": select_opts([
                "Активна", "Неисправна", "На ремонте", "Деактивирована",
            ])}},
            "Провайдер": {"rich_text": {}},
            "Дата установки": {"date": {}},
            "Срок контракта до": {"date": {}},
            "Примечания": {"rich_text": {}},
        },
    },
    "trawl_control": {
        "title": "Контроль трала",
        "icon": "🎯",
        "properties": {
            "Название / S/N": {"title": {}},
            "Судно (имя)": {"rich_text": {}},
            "Система": {"select": {"options": select_opts(["Marport", "Scanmar", "Прочая"])}},
            "Тип компонента": {"select": {"options": select_opts([
                "Кабинет/Компьютер", "Датчик глубины", "Датчик расхождения",
                "Датчик температуры", "Датчик наполнения", "Датчик геометрии",
                "Датчик скорости", "Головной датчик", "Прочий датчик",
            ])}},
            "Серийный номер": {"rich_text": {}},
            "Состояние": {"select": {"options": select_opts([
                "В работе", "Требует ремонта", "На ремонте", "В запасе", "Списан",
            ])}},
            "Расположение": {"select": {"options": select_opts([
                "На трале", "В запасе на судне", "На берегу", "В ремонте (сервис)",
            ])}},
            "Дата последней проверки": {"date": {}},
            "Примечания": {"rich_text": {}},
        },
    },
    "additional_equipment": {
        "title": "Дополнительное оборудование",
        "icon": "🔧",
        "properties": {
            "Название": {"title": {}},
            "Судно (имя)": {"rich_text": {}},
            "Категория": {"select": {"options": select_opts([
                "Автотрал", "Счетчик натяжения ваеров",
                "Навигационное", "Гидроакустическое", "Прочее",
            ])}},
            "Производитель": {"rich_text": {}},
            "Модель": {"rich_text": {}},
            "Серийный номер": {"rich_text": {}},
            "Состояние": {"select": {"options": select_opts([
                "В работе", "Требует ремонта", "На ремонте", "Выведена из эксплуатации",
            ])}},
            "Дата последнего ТО": {"date": {}},
            "Примечания": {"rich_text": {}},
        },
    },
    "catch_production": {
        "title": "Вылов и продукция",
        "icon": "🐟",
        "properties": {
            "Запись": {"title": {}},
            "Судно (имя)": {"rich_text": {}},
            "Тип данных": {"select": {"options": select_opts(["Вылов", "Выпуск продукции"])}},
            "Вид продукции": {"select": {"options": select_opts([
                "Минтай", "Треска", "Пикша", "Сельдь", "Кальмар",
                "Краб", "Креветка", "Лосось", "Камбала", "Прочее",
            ])}},
            "Период": {"select": {"options": select_opts([
                "День", "Неделя", "Месяц", "Квартал", "Год",
            ])}},
            "Дата начала": {"date": {}},
            "Дата окончания": {"date": {}},
            "Объем, тонн": {"number": {"format": "number"}},
            "Источник данных": {"rich_text": {}},
            "Примечания": {"rich_text": {}},
        },
    },
    "spare_parts_orders": {
        "title": "Заявки ЗИП",
        "icon": "📋",
        "properties": {
            "Заявка": {"title": {}},
            "Судно (имя)": {"rich_text": {}},
            "Категория оборудования": {"select": {"options": select_opts([
                "Лебедка", "Спутниковая система", "Контроль трала", "Доп. оборудование",
            ])}},
            "Тип заявки": {"select": {"options": select_opts([
                "Заказ ЗИП", "Ремонт", "Замена", "Диагностика", "Прочее",
            ])}},
            "Статус": {"select": {"options": select_opts([
                "Новая", "Запрос поставщику", "Ожидание КП", "КП получено",
                "Согласование", "Оплачено", "В производстве", "Отгружено",
                "В пути", "Получено", "Установлено", "Закрыта", "Отменена",
            ])}},
            "Приоритет": {"select": {"options": select_opts([
                "Критический", "Высокий", "Средний", "Низкий",
            ])}},
            "Описание": {"rich_text": {}},
            "Наименования ЗИП": {"rich_text": {}},
            "Поставщик": {"rich_text": {}},
            "Стоимость": {"number": {"format": "number"}},
            "Валюта": {"select": {"options": select_opts(["RUB", "USD", "EUR", "NOK"])}},
            "Дата заявки": {"date": {}},
            "Дата ожидаемая": {"date": {}},
            "Дата исполнения": {"date": {}},
            "Документы": {"files": {}},
        },
    },
}


# ---------------------------------------------------------------------------
# Create databases
# ---------------------------------------------------------------------------

def create_database(parent_page_id: str, key: str, schema: dict) -> str:
    """Create a Notion database and return its ID."""
    title_text = schema["title"]
    icon = schema.get("icon", "")

    payload = {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "title": [{"type": "text", "text": {"content": title_text}}],
        "properties": schema["properties"],
        "is_inline": True,
    }
    if icon:
        payload["icon"] = {"type": "emoji", "emoji": icon}

    print(f"  Creating database: {icon} {title_text} ...")
    result = api_post("databases", payload)
    db_id = result["id"]
    print(f"    → ID: {db_id}")
    return db_id


def create_all_databases():
    parent_id = get_parent_page_id()
    print(f"Creating databases under page {parent_id} ...\n")

    db_ids = {}
    for key, schema in DATABASES.items():
        db_id = create_database(parent_id, key, schema)
        db_ids[key] = db_id

    save_db_ids(db_ids)

    print(f"\nAll {len(db_ids)} databases created.")
    print("Next steps:")
    print("  1. Open Notion and configure Relations between databases")
    print("  2. Run with --import-data to populate Компании, Суда, Квоты")
    return db_ids


# ---------------------------------------------------------------------------
# Add relations (after all DBs exist)
# ---------------------------------------------------------------------------

def add_relations(db_ids: dict):
    """Add relation properties between databases.
    Notion API doesn't support creating dual-property relations in one call,
    so we add them one by one after database creation.
    """
    print("\nAdding relations between databases...")

    relation_map = [
        ("vessels", "Судовладелец", "companies"),
        ("winches", "Судно", "vessels"),
        ("satellite_systems", "Судно", "vessels"),
        ("trawl_control", "Судно", "vessels"),
        ("additional_equipment", "Судно", "vessels"),
        ("catch_production", "Судно", "vessels"),
        ("spare_parts_orders", "Судно", "vessels"),
        ("quotas", "Компания (связь)", "companies"),
    ]

    for source_key, prop_name, target_key in relation_map:
        source_id = db_ids.get(source_key)
        target_id = db_ids.get(target_key)
        if not source_id or not target_id:
            print(f"  SKIP {source_key}.{prop_name} → {target_key} (missing IDs)")
            continue

        payload = {
            "properties": {
                prop_name: {
                    "relation": {
                        "database_id": target_id,
                        "type": "dual_property",
                        "dual_property": {},
                    }
                }
            }
        }
        url = f"{BASE_URL}/databases/{source_id}"
        resp = requests.patch(url, headers=headers(), json=payload, timeout=30)
        if resp.status_code >= 400:
            print(f"  WARN: {source_key}.{prop_name} → {target_key}: {resp.status_code} {resp.text[:200]}")
        else:
            print(f"  OK: {source_key}.{prop_name} → {target_key}")
        time.sleep(RATE_LIMIT_DELAY)


# ---------------------------------------------------------------------------
# Import data
# ---------------------------------------------------------------------------

def rich_text(value: str):
    if not value:
        return []
    return [{"type": "text", "text": {"content": str(value)[:2000]}}]


def sanitize_select(value: str) -> str:
    """Notion select options cannot contain commas."""
    return value.replace(",", ";") if value else value


def import_companies(db_id: str):
    csv_path = IMPORT_DIR / "companies.csv"
    if not csv_path.exists():
        print("  companies.csv not found, run prepare_notion_import.py first")
        return

    rows = list(csv.DictReader(open(csv_path, encoding="utf-8")))
    print(f"  Importing {len(rows)} companies...")

    for i, r in enumerate(rows):
        props = {
            "Название": {"title": rich_text(r["Название"])},
            "ИНН": {"rich_text": rich_text(r["ИНН"])},
            "ОГРН": {"rich_text": rich_text(r.get("ОГРН", ""))},
            "Директор": {"rich_text": rich_text(r["Директор"])},
            "Контакты": {"rich_text": rich_text(r["Контакты"])},
            "Цербер — страны экспорта": {"rich_text": rich_text(r.get("Цербер_страны_экспорта", ""))},
        }
        group = r.get("Группа_компаний", "").strip()
        if group:
            props["Группа компаний"] = {"select": {"name": sanitize_select(group)}}
        status = r.get("Статус", "").strip()
        if status:
            props["Статус"] = {"select": {"name": sanitize_select(status)}}

        cerb_ships = r.get("Цербер_судов", "")
        if cerb_ships and cerb_ships.isdigit():
            props["Кол-во судов (Цербер)"] = {"number": int(cerb_ships)}

        payload = {"parent": {"database_id": db_id}, "properties": props}
        api_post("pages", payload)

        if (i + 1) % 50 == 0:
            print(f"    ... {i + 1}/{len(rows)}")

    print(f"  → {len(rows)} companies imported")


def import_vessels(db_id: str):
    csv_path = IMPORT_DIR / "vessels.csv"
    if not csv_path.exists():
        print("  vessels.csv not found, run prepare_notion_import.py first")
        return

    rows = list(csv.DictReader(open(csv_path, encoding="utf-8")))
    print(f"  Importing {len(rows)} vessels...")

    for i, r in enumerate(rows):
        props = {
            "Название судна": {"title": rich_text(r["Название_судна"])},
            "Бортовой номер": {"rich_text": rich_text(r["Бортовой_номер"])},
            "IMO": {"rich_text": rich_text(r["IMO"])},
            "GFW ID": {"rich_text": rich_text(r["GFW_ID"])},
            "GFW Name": {"rich_text": rich_text(r["GFW_Name"])},
            "Регион регистрации": {"rich_text": rich_text(r["Регион_регистрации"])},
        }
        state = r.get("Состояние", "").strip()
        if state:
            props["Состояние"] = {"select": {"name": state}}
        vtype = r.get("Тип_Модель", "").strip()
        if vtype:
            props["Тип/Модель (проект)"] = {"select": {"name": vtype}}
        region = r.get("Регион_работы", "").strip()
        if region:
            props["Регион работы"] = {"select": {"name": region}}

        payload = {"parent": {"database_id": db_id}, "properties": props}
        api_post("pages", payload)

        if (i + 1) % 50 == 0:
            print(f"    ... {i + 1}/{len(rows)}")

    print(f"  → {len(rows)} vessels imported")


def import_quotas(db_id: str, limit: int = 500, offset: int = 0):
    """Import quotas. Default limit=500 to avoid very long runs (6951 rows × 0.35s ≈ 40 min)."""
    csv_path = IMPORT_DIR / "quotas.csv"
    if not csv_path.exists():
        print("  quotas.csv not found, run prepare_notion_import.py first")
        return

    rows = list(csv.DictReader(open(csv_path, encoding="utf-8")))
    total = len(rows)
    if offset:
        rows = rows[offset:]
        print(f"  Quotas: {total} total, skipping first {offset}")
    if limit and len(rows) > limit:
        print(f"  Importing {limit} of {len(rows)} remaining (use --quota-limit 0 for all)")
        rows = rows[:limit]

    print(f"  Importing {len(rows)} quotas (from offset {offset})...")

    for i, r in enumerate(rows):
        props = {
            "Запись": {"title": rich_text(r["Запись"])},
            "Компания": {"rich_text": rich_text(r["Компания"])},
            "Компания ИНН": {"rich_text": rich_text(r["Компания_ИНН"])},
            "Дата начала договора": {"rich_text": rich_text(r.get("Дата_начала_договора", ""))},
            "Дата окончания договора": {"rich_text": rich_text(r.get("Дата_окончания_договора", ""))},
            "Причина изменения": {"rich_text": rich_text(r.get("Причина_изменения", ""))},
        }
        year = r.get("Год", "").strip()
        if year:
            props["Год"] = {"select": {"name": year}}
        basin = r.get("Бассейн", "").strip()
        if basin:
            props["Бассейн"] = {"select": {"name": basin}}
        species = r.get("Объект_лова", "").strip()
        if species:
            props["Объект лова"] = {"select": {"name": sanitize_select(species)}}
        qtype = r.get("Тип_квоты", "").strip()
        if qtype:
            props["Тип квоты"] = {"select": {"name": sanitize_select(qtype)}}

        share = r.get("Доля_процент", "").strip()
        if share:
            try:
                props["Доля, %"] = {"number": float(share) / 100.0}
            except ValueError:
                pass
        volume = r.get("Объем_тонн", "").strip()
        if volume:
            try:
                props["Объем, тонн"] = {"number": float(volume)}
            except ValueError:
                pass

        group = r.get("Группа_компаний", "").strip()
        if group:
            props["Группа компаний"] = {"select": {"name": sanitize_select(group)}}

        payload = {"parent": {"database_id": db_id}, "properties": props}
        api_post("pages", payload)

        if (i + 1) % 100 == 0:
            print(f"    ... {i + 1}/{len(rows)}")

    print(f"  → {len(rows)} quotas imported")


# ---------------------------------------------------------------------------
# Persist database IDs
# ---------------------------------------------------------------------------

DB_IDS_FILE = ROOT / "notion_db_ids.json"


def save_db_ids(db_ids: dict):
    with open(DB_IDS_FILE, "w") as f:
        json.dump(db_ids, f, indent=2)
    print(f"\nDatabase IDs saved to {DB_IDS_FILE}")

    lines = []
    if ENV_PATH.exists():
        with open(ENV_PATH) as f:
            lines = f.readlines()

    existing_keys = {l.split("=")[0].strip() for l in lines if "=" in l}
    additions = []
    key_map = {
        "companies": "NOTION_DB_COMPANIES",
        "vessels": "NOTION_DB_VESSELS",
        "quotas": "NOTION_DB_QUOTAS",
        "winches": "NOTION_DB_WINCHES",
        "satellite_systems": "NOTION_DB_SATELLITE",
        "trawl_control": "NOTION_DB_TRAWL",
        "additional_equipment": "NOTION_DB_ADDITIONAL",
        "catch_production": "NOTION_DB_CATCH",
        "spare_parts_orders": "NOTION_DB_ORDERS",
    }
    for db_key, env_key in key_map.items():
        if db_key in db_ids and env_key not in existing_keys:
            additions.append(f"{env_key}={db_ids[db_key]}\n")

    if additions:
        with open(ENV_PATH, "a") as f:
            f.write("\n# Notion database IDs (auto-generated)\n")
            for line in additions:
                f.write(line)
        print(f"Database IDs appended to .env")


def load_db_ids() -> dict:
    if DB_IDS_FILE.exists():
        with open(DB_IDS_FILE) as f:
            return json.load(f)
    return {}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Notion database setup and import")
    parser.add_argument("--create-dbs", action="store_true", help="Create all databases in Notion")
    parser.add_argument("--import-data", action="store_true", help="Import companies, vessels, quotas")
    parser.add_argument("--quota-limit", type=int, default=500,
                        help="Max quotas to import (0 = all, default 500)")
    parser.add_argument("--quota-offset", type=int, default=0,
                        help="Skip first N quotas (to resume after partial import)")
    parser.add_argument("--skip-companies", action="store_true",
                        help="Skip companies import (if already done)")
    parser.add_argument("--skip-vessels", action="store_true",
                        help="Skip vessels import (if already done)")
    args = parser.parse_args()

    if not args.create_dbs and not args.import_data:
        parser.print_help()
        return

    db_ids = load_db_ids()

    if args.create_dbs:
        db_ids = create_all_databases()
        add_relations(db_ids)

    if args.import_data:
        if not db_ids:
            sys.exit("No database IDs found. Run --create-dbs first or create notion_db_ids.json.")

        print("\nImporting data into Notion databases...\n")
        if "companies" in db_ids and not args.skip_companies:
            import_companies(db_ids["companies"])
        elif args.skip_companies:
            print("  Skipping companies (already imported)")
        if "vessels" in db_ids and not args.skip_vessels:
            import_vessels(db_ids["vessels"])
        elif args.skip_vessels:
            print("  Skipping vessels (already imported)")
        if "quotas" in db_ids:
            import_quotas(db_ids["quotas"], limit=args.quota_limit, offset=args.quota_offset)

        print("\nImport complete!")
        print("Next: configure Relations in Notion UI to link Суда → Компании, Квоты → Компании")


if __name__ == "__main__":
    main()
