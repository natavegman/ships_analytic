#!/usr/bin/env python3
"""
Экспорт компаний и групп в CSV для ручного редактирования.

Создаёт data/company_groups_manual_edit.csv с колонками:
  ИНН, Юр_Лицо, Группа_Компаний, Исключить, Комментарий, Директор_ListOrg

Колонка «Исключить»:
  Пустая — компания обрабатывается как обычно.
  Любое непустое значение (например «Каспий», «килька», «не рыба») — компания
  исключается из обогащения, группировки, веб-карты и company_groups.csv.
  Маркер сохраняется и не теряется при будущих запусках обогащения.

Использование:
  python3 scripts/export_company_groups_for_manual_edit.py

После правки в Excel/Sheets импортируйте обратно:
  python3 scripts/import_company_groups_from_manual_edit.py
"""

from __future__ import annotations

import csv
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
ENRICHED_CSV = BASE_DIR / "data" / "company_groups_enriched.csv"
EDIT_CSV = BASE_DIR / "data" / "company_groups_manual_edit.csv"


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
    if not ENRICHED_CSV.exists():
        print(f"Файл не найден: {ENRICHED_CSV}")
        print("Сначала запустите: python3 scripts/enrich_companies_to_db.py")
        return

    rows = []
    with ENRICHED_CSV.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            inn = _norm_inn(row.get("ИНН"))
            if not inn:
                continue
            rows.append({
                "ИНН": inn,
                "Юр_Лицо": (row.get("Юр_Лицо") or "").strip(),
                "Группа_Компаний": (row.get("Группа_Компаний") or "").strip(),
                "Исключить": (row.get("Исключить") or "").strip(),
                "Комментарий": (row.get("Комментарий") or "").strip(),
                "Директор_ListOrg": (row.get("Директор_ListOrg") or "").strip(),
            })

    EDIT_CSV.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["ИНН", "Юр_Лицо", "Группа_Компаний", "Исключить", "Комментарий", "Директор_ListOrg"]
    with EDIT_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    with_group = sum(1 for r in rows if r["Группа_Компаний"])
    excluded = sum(1 for r in rows if r["Исключить"])
    print(f"Экспорт: {len(rows)} компаний → {EDIT_CSV}")
    print(f"  С группой: {with_group}, исключённых: {excluded}, без группы: {len(rows) - with_group - excluded}")
    print()
    print("Колонка «Исключить»: впишите причину (Каспий, килька, не рыба и т.д.)")
    print("  для компаний, которые не нужно обрабатывать.")
    print()
    print("После правки: python3 scripts/import_company_groups_from_manual_edit.py")


if __name__ == "__main__":
    main()
