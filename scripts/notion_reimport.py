#!/usr/bin/env python3
"""
Full reimport: archive all pages in companies/vessels/quotas DBs, then import fresh data.

Usage:
    PYTHONUNBUFFERED=1 python3 -u scripts/notion_reimport.py
"""

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

TOKEN = os.environ.get("NOTION_API_TOKEN", "")
if not TOKEN:
    sys.exit("NOTION_API_TOKEN not set")

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28",
}

DB_IDS_FILE = ROOT / "notion_db_ids.json"
with open(DB_IDS_FILE) as f:
    DB_IDS = json.load(f)

DB_COMPANIES = DB_IDS["companies"]
DB_VESSELS = DB_IDS["vessels"]
DB_QUOTAS = DB_IDS["quotas"]


def api_post(endpoint, payload, retries=5):
    url = f"https://api.notion.com/v1/{endpoint}"
    for attempt in range(retries):
        try:
            resp = requests.post(url, headers=HEADERS, json=payload, timeout=30)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            wait = min(2 ** attempt, 30)
            print(f"  Connection error, retry in {wait}s...")
            time.sleep(wait)
            continue
        if resp.status_code == 429:
            wait = float(resp.headers.get("Retry-After", 2))
            time.sleep(wait)
            continue
        if resp.status_code in (502, 503):
            time.sleep(min(2 ** attempt, 30))
            continue
        if resp.status_code == 400:
            print(f"  WARN 400: {resp.text[:200]}")
            return None
        if resp.status_code >= 400:
            print(f"  ERROR {resp.status_code}: {resp.text[:200]}")
            if attempt < retries - 1:
                time.sleep(1)
                continue
            return None
        time.sleep(0.34)
        return resp.json()
    return None


def archive_page(page_id):
    for attempt in range(3):
        try:
            resp = requests.patch(
                f"https://api.notion.com/v1/pages/{page_id}",
                headers=HEADERS, json={"archived": True}, timeout=15
            )
            if resp.status_code == 200:
                return True
            if resp.status_code == 429:
                time.sleep(float(resp.headers.get("Retry-After", 2)))
                continue
        except Exception:
            time.sleep(2)
    return False


def query_all(db_id):
    pages = []
    start_cursor = None
    while True:
        payload = {"page_size": 100}
        if start_cursor:
            payload["start_cursor"] = start_cursor
        resp = requests.post(
            f"https://api.notion.com/v1/databases/{db_id}/query",
            headers=HEADERS, json=payload, timeout=30
        )
        if resp.status_code != 200:
            print(f"  Query error: {resp.status_code}")
            break
        data = resp.json()
        pages.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        start_cursor = data.get("next_cursor")
        time.sleep(0.35)
    return pages


def clear_db(db_id, label):
    print(f"\n  Clearing {label}...")
    pages = query_all(db_id)
    total = len(pages)
    print(f"  Found {total} pages to archive")
    archived = 0
    for i, page in enumerate(pages):
        if archive_page(page["id"]):
            archived += 1
        time.sleep(0.34)
        if (i + 1) % 50 == 0:
            print(f"    ... {i + 1}/{total}")
    print(f"  Archived: {archived}/{total}")
    return archived


def rich_text(value):
    if not value:
        return []
    return [{"type": "text", "text": {"content": str(value)[:2000]}}]


def sanitize_select(value):
    return value.replace(",", ";") if value else value


def import_companies():
    csv_path = IMPORT_DIR / "companies.csv"
    rows = list(csv.DictReader(open(csv_path, encoding="utf-8")))
    print(f"\n  Importing {len(rows)} companies...")

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
            props["Статус"] = {"select": {"name": status}}
        cerb_ships = r.get("Цербер_судов", "")
        if cerb_ships and cerb_ships.isdigit():
            props["Кол-во судов (Цербер)"] = {"number": int(cerb_ships)}

        api_post("pages", {"parent": {"database_id": DB_COMPANIES}, "properties": props})
        if (i + 1) % 50 == 0:
            print(f"    ... {i + 1}/{len(rows)}")

    print(f"  Done: {len(rows)} companies")


def import_vessels():
    csv_path = IMPORT_DIR / "vessels.csv"
    rows = list(csv.DictReader(open(csv_path, encoding="utf-8")))
    print(f"\n  Importing {len(rows)} vessels...")

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

        api_post("pages", {"parent": {"database_id": DB_VESSELS}, "properties": props})
        if (i + 1) % 50 == 0:
            print(f"    ... {i + 1}/{len(rows)}")

    print(f"  Done: {len(rows)} vessels")


def import_quotas():
    csv_path = IMPORT_DIR / "quotas.csv"
    rows = list(csv.DictReader(open(csv_path, encoding="utf-8")))
    print(f"\n  Importing {len(rows)} quotas...")

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
            props["Бассейн"] = {"select": {"name": sanitize_select(basin)}}
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

        api_post("pages", {"parent": {"database_id": DB_QUOTAS}, "properties": props})
        if (i + 1) % 100 == 0:
            print(f"    ... {i + 1}/{len(rows)}")

    print(f"  Done: {len(rows)} quotas")


def main():
    print("=" * 60)
    print("NOTION REIMPORT — полная перезагрузка данных")
    print("=" * 60)

    print("\n--- Фаза 1: Очистка баз данных ---")
    clear_db(DB_COMPANIES, "Компании")
    clear_db(DB_VESSELS, "Суда")
    clear_db(DB_QUOTAS, "Квоты")

    print("\n--- Фаза 2: Импорт свежих данных ---")
    import_companies()
    import_vessels()
    import_quotas()

    print("\n" + "=" * 60)
    print("ГОТОВО!")
    print("=" * 60)


if __name__ == "__main__":
    main()
