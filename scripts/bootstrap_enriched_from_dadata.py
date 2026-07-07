#!/usr/bin/env python3
"""
Бутстрап data/company_groups_enriched.csv из DaData (официальный реестр ФНС).

Зачем: company_groups_enriched.csv в репозитории не хранится (gitignore) — его
создаёт enrich_via_datanewton.py, но для этого нужен DATANEWTON_API_KEY, а он
пока не выдан. DaData даёт более скромный набор полей (без графа связей и
учредителей), но зато официальный адрес и директора по каждому ИНН — и это
уже заметно улучшает качество resolve_company_groups_ai.py: в первом реальном
прогоне именно совпадение адреса стало основанием для двух единственных
уверенных решений, а большинство "Требует проверки" — как раз случаи, где
адреса не было вообще (пустой Контакты_ListOrg в company_groups.csv).

Не заменяет enrich_via_datanewton.py — не даёт ОГРН, ИНН директора, учредителей,
граф связей (Фаза 3 разбора остаётся недоступна без DataNewton). Просто честно
бутстрапит то, что можно получить от DaData, в ожидаемом enriched-формате.

Использование:
    python3 scripts/bootstrap_enriched_from_dadata.py --dry-run
    python3 scripts/bootstrap_enriched_from_dadata.py
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dadata_client import DaDataEnricher

BASE_DIR = Path(__file__).resolve().parents[1]
GROUPS_CSV = BASE_DIR / "data" / "company_groups.csv"
ENRICHED_CSV = BASE_DIR / "data" / "company_groups_enriched.csv"

ENRICHED_FIELDS = [
    "Группа_Компаний", "Юр_Лицо", "ИНН", "ОГРН", "Комментарий", "Исключить",
    "Контакты_ListOrg", "Директор_ListOrg", "Директор_ИНН_ФЛ",
    "Учредители_JSON", "Связанные_Компании_JSON", "ListOrg_URL",
    "Финансовые_Данные_JSON",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Бутстрап company_groups_enriched.csv через DaData")
    parser.add_argument("--dry-run", action="store_true", help="Показать план без вызовов API")
    parser.add_argument("--limit", type=int, default=0, help="Максимум компаний (0 = все)")
    parser.add_argument("--source", type=Path, default=GROUPS_CSV,
                         help="Исходный CSV (по умолчанию data/company_groups.csv)")
    args = parser.parse_args()

    if not args.source.exists():
        raise SystemExit(f"Не найден {args.source}")

    with args.source.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    if args.limit:
        rows = rows[:args.limit]

    print(f"Компаний в {args.source.name}: {len(rows)}")

    if args.dry_run:
        print("[DRY RUN] Запросов к DaData не будет.")
        return

    enriched_rows = []
    ok = failed = 0
    with DaDataEnricher() as enricher:
        for i, r in enumerate(rows, 1):
            inn = (r.get("ИНН") or "").strip()
            name = (r.get("Юр_Лицо") or "").strip()
            print(f"[{i}/{len(rows)}] {inn} {name[:40]}...", end=" ", flush=True)

            info = None
            try:
                info = enricher.get_info(inn) if inn else None
            except Exception as exc:
                print(f"✗ ошибка: {exc}")

            financial = {}
            director = (r.get("Директор_ListOrg") or "").strip()
            address = (r.get("Контакты_ListOrg") or "").strip()
            legal_name = name

            if info:
                ok += 1
                if info.name_full:
                    legal_name = info.name_full
                if info.director_name:
                    director = info.director_name
                if info.address_text:
                    address = f"Адрес: {info.address_text}"
                financial = {
                    "capital": info.capital,
                    "status": info.status,
                    "okved": info.okved,
                    "role": info.role,
                }
                print("✓")
            else:
                failed += 1
                print("✗ (не найден в DaData)")

            enriched_rows.append({
                "Группа_Компаний": (r.get("Группа_Компаний") or "").strip(),
                "Юр_Лицо": legal_name,
                "ИНН": inn,
                "ОГРН": "",
                "Комментарий": (r.get("Комментарий") or "").strip(),
                "Исключить": "",
                "Контакты_ListOrg": address,
                "Директор_ListOrg": director,
                "Директор_ИНН_ФЛ": "",
                "Учредители_JSON": "",
                "Связанные_Компании_JSON": "",
                "ListOrg_URL": (r.get("ListOrg_URL") or "").strip(),
                "Финансовые_Данные_JSON": json.dumps(financial, ensure_ascii=False) if financial else "",
            })
            time.sleep(0.15)  # мягкий рейт-лимит

    with ENRICHED_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=ENRICHED_FIELDS)
        writer.writeheader()
        writer.writerows(enriched_rows)

    print(f"\nСохранено: {ENRICHED_CSV} (найдено в DaData: {ok}, не найдено: {failed})")
    print("Дальше: python3 scripts/resolve_company_groups_ai.py --dry-run")


if __name__ == "__main__":
    main()
