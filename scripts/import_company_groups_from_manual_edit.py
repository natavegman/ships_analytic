#!/usr/bin/env python3
"""
Импорт отредактированных данных из CSV обратно в справочники.

Читает data/company_groups_manual_edit.csv, обновляет в company_groups_enriched.csv:
  - Группа_Компаний
  - Комментарий
  - Исключить (маркер исключения из обработки)

Затем перезаписывает company_groups.csv (только строки с группой, без исключённых).

Использование:
  python3 scripts/import_company_groups_from_manual_edit.py
"""

from __future__ import annotations

import csv
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
EDIT_CSV = BASE_DIR / "data" / "company_groups_manual_edit.csv"
ENRICHED_CSV = BASE_DIR / "data" / "company_groups_enriched.csv"
GROUPS_CSV = BASE_DIR / "data" / "company_groups.csv"

GROUPS_FIELDS = [
    "Группа_Компаний",
    "Юр_Лицо",
    "ИНН",
    "Комментарий",
    "Контакты_ListOrg",
    "Директор_ListOrg",
    "ListOrg_URL",
]


def _norm_inn(inn_raw) -> str:
    if inn_raw is None:
        return ""
    if isinstance(inn_raw, (int, float)):
        inn_str = str(int(inn_raw))
        if len(inn_str) == 9:
            return "0" + inn_str
        if len(inn_str) == 11:
            return "0" + inn_str
        return inn_str
    inn = str(inn_raw).strip()
    return inn if inn.isdigit() else ""


def main() -> None:
    if not EDIT_CSV.exists():
        print(f"Файл не найден: {EDIT_CSV}")
        print("Сначала экспортируйте: python3 scripts/export_company_groups_for_manual_edit.py")
        return

    edits: dict[str, dict] = {}
    with EDIT_CSV.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            inn = _norm_inn(row.get("ИНН"))
            if not inn:
                continue
            edits[inn] = {
                "Группа_Компаний": (row.get("Группа_Компаний") or "").strip(),
                "Комментарий": (row.get("Комментарий") or "").strip(),
                "Исключить": (row.get("Исключить") or "").strip(),
            }

    if not edits:
        print("В файле правок нет записей с валидным ИНН.")
        return

    if not ENRICHED_CSV.exists():
        print(f"Файл не найден: {ENRICHED_CSV}")
        return

    enriched_rows = []
    applied = 0
    existing_fieldnames = []
    with ENRICHED_CSV.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        existing_fieldnames = list(reader.fieldnames or [])
        for row in reader:
            inn = _norm_inn(row.get("ИНН"))
            if not inn:
                enriched_rows.append(row)
                continue
            if inn in edits:
                row["Группа_Компаний"] = edits[inn]["Группа_Компаний"]
                row["Комментарий"] = edits[inn]["Комментарий"]
                row["Исключить"] = edits[inn]["Исключить"]
                applied += 1
            enriched_rows.append(row)

    if "Исключить" not in existing_fieldnames:
        idx = existing_fieldnames.index("Комментарий") + 1 if "Комментарий" in existing_fieldnames else 4
        existing_fieldnames.insert(idx, "Исключить")

    with ENRICHED_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=existing_fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in enriched_rows:
            writer.writerow({k: row.get(k, "") for k in existing_fieldnames})

    # company_groups.csv — только с группой и без маркера исключения
    groups_rows = [
        {
            "Группа_Компаний": r.get("Группа_Компаний", ""),
            "Юр_Лицо": r.get("Юр_Лицо", ""),
            "ИНН": r.get("ИНН", ""),
            "Комментарий": r.get("Комментарий", ""),
            "Контакты_ListOrg": r.get("Контакты_ListOrg", ""),
            "Директор_ListOrg": r.get("Директор_ListOrg", ""),
            "ListOrg_URL": r.get("ListOrg_URL", ""),
        }
        for r in enriched_rows
        if (r.get("ИНН")
            and (r.get("Группа_Компаний") or "").strip()
            and not (r.get("Исключить") or "").strip())
    ]

    GROUPS_CSV.parent.mkdir(parents=True, exist_ok=True)
    with GROUPS_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=GROUPS_FIELDS)
        w.writeheader()
        w.writerows(groups_rows)

    excluded_count = sum(1 for e in edits.values() if e["Исключить"])
    print(f"Импорт: применено правок по ИНН: {applied} из {len(edits)}")
    print(f"  Исключено компаний (маркер «Исключить»): {excluded_count}")
    print(f"  {ENRICHED_CSV.name} — обновлён")
    print(f"  {GROUPS_CSV.name} — записано строк с группой: {len(groups_rows)}")


if __name__ == "__main__":
    main()
