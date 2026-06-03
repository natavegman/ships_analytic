#!/usr/bin/env python3
"""Resume quota import from a given offset."""
import csv
import json
import os
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
_env = ROOT / ".env"
if _env.exists():
    try:
        from dotenv import load_dotenv

        load_dotenv(_env)
    except ImportError:
        pass

TOKEN = os.environ.get("NOTION_API_TOKEN", "")
if not TOKEN:
    sys.exit("NOTION_API_TOKEN not set")
H = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28",
}

with open(ROOT / "notion_db_ids.json") as f:
    DB = json.load(f)["quotas"]

OFFSET = int(sys.argv[1]) if len(sys.argv) > 1 else 0

rows = list(csv.DictReader(open(ROOT / "notion_import" / "quotas.csv", encoding="utf-8")))
total = len(rows)
rows = rows[OFFSET:]
print(f"Offset {OFFSET}, importing {len(rows)} of {total} quotas", flush=True)


def rich_text(v):
    if not v:
        return []
    return [{"type": "text", "text": {"content": str(v)[:2000]}}]


def sanitize(v):
    return v.replace(",", ";") if v else v


imported = 0
errors = 0
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
        props["Бассейн"] = {"select": {"name": sanitize(basin)}}
    species = r.get("Объект_лова", "").strip()
    if species:
        props["Объект лова"] = {"select": {"name": sanitize(species)}}
    qtype = r.get("Тип_квоты", "").strip()
    if qtype:
        props["Тип квоты"] = {"select": {"name": sanitize(qtype)}}
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
        props["Группа компаний"] = {"select": {"name": sanitize(group)}}

    payload = {"parent": {"database_id": DB}, "properties": props}

    ok = False
    for attempt in range(5):
        try:
            resp = requests.post(
                "https://api.notion.com/v1/pages",
                headers=H, json=payload, timeout=15,
            )
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            wait = min(2 ** attempt, 16)
            print(f"  Conn err row {OFFSET+i}, retry {attempt+1} in {wait}s", flush=True)
            time.sleep(wait)
            continue
        if resp.status_code == 429:
            time.sleep(float(resp.headers.get("Retry-After", 2)))
            continue
        if resp.status_code in (502, 503):
            time.sleep(min(2 ** attempt, 16))
            continue
        if resp.status_code == 200:
            ok = True
            break
        if resp.status_code == 400:
            print(f"  WARN row {OFFSET+i}: {resp.text[:200]}", flush=True)
            break
        print(f"  ERR row {OFFSET+i}: {resp.status_code}", flush=True)
        time.sleep(1)

    if ok:
        imported += 1
    else:
        errors += 1
    time.sleep(0.34)
    if (i + 1) % 200 == 0:
        print(f"  ... {i+1}/{len(rows)} (ok={imported}, err={errors})", flush=True)

print(f"DONE: {imported} imported, {errors} errors out of {len(rows)}", flush=True)
