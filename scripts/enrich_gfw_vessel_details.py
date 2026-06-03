#!/usr/bin/env python3
"""
Обогащение записей в data/gfw_our_vessels.json данными GFW Events API.

Только события (без vessel_by_id): траление, заходы в порт, перегрузки (encounters).
Для каждого судна с gfw_id запрашивает за последние 90 дней:
  - fishing events → gfw_fishing_events_90d, gfw_last_fishing_date
  - port visits → gfw_port_visits_90d, gfw_last_port_visit
  - encounters → gfw_encounters_90d, gfw_last_encounter_date

Требует GFW_API_TOKEN. Запуск:
  python3 scripts/enrich_gfw_vessel_details.py
  python3 scripts/enrich_gfw_vessel_details.py --limit 100
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_env = ROOT / ".env"
if _env.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_env)
    except ImportError:
        import os
        for line in _env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip().strip('"').strip("'")

from scripts.gfw_client import (
    get_token,
    get_fishing_events_summary,
    get_port_visits_summary,
    get_encounters_summary,
)

DATA = ROOT / "data"
CACHE_PATH = DATA / "gfw_our_vessels.json"


def main() -> None:
    # Чтобы вывод был виден сразу (без буферизации)
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="Обогащение gfw_our_vessels событиями GFW (траление, порты, перегрузки)")
    ap.add_argument("--limit", type=int, default=0, help="Макс. судов для запроса событий (0 = все)")
    args = ap.parse_args()

    print("Старт обогащения GFW (события за 90 дней)...", flush=True)

    if not get_token():
        print("Задайте GFW_API_TOKEN в окружении.")
        sys.exit(1)

    if not CACHE_PATH.exists():
        print(f"Файл не найден: {CACHE_PATH}")
        sys.exit(1)

    print("Загрузка кэша судов...", flush=True)
    vessels = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    if not isinstance(vessels, list):
        print("Ожидается список записей в JSON.")
        sys.exit(1)

    gfw_ids = [v["gfw_id"] for v in vessels if v.get("gfw_id")]
    if args.limit and args.limit > 0:
        gfw_ids = gfw_ids[: args.limit]
    print(f"Судов с gfw_id: {len(gfw_ids)}. Запросы к API идут батчами по 50, может занять несколько минут.", flush=True)

    # Fishing
    print("Запрос fishing events...", flush=True)
    try:
        fishing = get_fishing_events_summary(gfw_ids, days_back=90)
        print("  fishing: готово.", flush=True)
    except Exception as e:
        print(f"Ошибка fishing events: {e}", flush=True)
        fishing = {}
    # Port visits
    print("Запрос port visits...", flush=True)
    try:
        port_visits = get_port_visits_summary(gfw_ids, days_back=90)
        print("  port visits: готово.", flush=True)
    except Exception as e:
        print(f"Ошибка port visits: {e}", flush=True)
        port_visits = {}
    # Encounters (перегрузки)
    print("Запрос encounters (перегрузки)...", flush=True)
    try:
        encounters = get_encounters_summary(gfw_ids, days_back=90)
        print("  encounters: готово.", flush=True)
    except Exception as e:
        print(f"Ошибка encounters: {e}", flush=True)
        encounters = {}

    for i, v in enumerate(vessels):
        gid = v.get("gfw_id")
        if not gid:
            continue
        if gid in fishing:
            s = fishing[gid]
            vessels[i]["gfw_fishing_events_90d"] = s.get("count") or 0
            vessels[i]["gfw_last_fishing_date"] = s.get("last_date") or ""
        if gid in port_visits:
            s = port_visits[gid]
            vessels[i]["gfw_port_visits_90d"] = s.get("count") or 0
            vessels[i]["gfw_last_port_visit"] = s.get("last_date") or ""
        if gid in encounters:
            s = encounters[gid]
            vessels[i]["gfw_encounters_90d"] = s.get("count") or 0
            vessels[i]["gfw_last_encounter_date"] = s.get("last_date") or ""

    CACHE_PATH.write_text(json.dumps(vessels, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Сохранено: {CACHE_PATH}", flush=True)


if __name__ == "__main__":
    main()
