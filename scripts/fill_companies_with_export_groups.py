#!/usr/bin/env python3
"""
Обновляет колонку Группа_Компаний в output/companies_with_export.csv по ИНН
из data/company_groups_enriched.csv и data/company_groups.csv.

Не трогает остальные колонки (Цербер, РМРС, FleetPhoto и т.д.).
Запуск: python3 scripts/fill_companies_with_export_groups.py
"""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
ENRICHED_CSV = BASE_DIR / "data" / "company_groups_enriched.csv"
GROUPS_CSV = BASE_DIR / "data" / "company_groups.csv"
COMPANIES_WITH_EXPORT_CSV = BASE_DIR / "output" / "companies_with_export.csv"


def main() -> None:
    import csv

    inn_to_group: dict[str, str] = {}
    excluded_inns: set[str] = set()
    for path in (ENRICHED_CSV, GROUPS_CSV):
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                inn = (row.get("ИНН") or "").strip()
                if inn and (row.get("Исключить") or "").strip():
                    excluded_inns.add(inn)
                grp = (row.get("Группа_Компаний") or "").strip()
                if inn and grp and inn not in excluded_inns:
                    inn_to_group[inn] = grp

    if not inn_to_group:
        print("Нет записей с группой в company_groups_enriched.csv и company_groups.csv")
        return

    print(f"Загружено ИНН с группой: {len(inn_to_group)}")

    if not COMPANIES_WITH_EXPORT_CSV.exists():
        print(f"Файл не найден: {COMPANIES_WITH_EXPORT_CSV}")
        return

    rows = []
    updated = 0
    with COMPANIES_WITH_EXPORT_CSV.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        for row in reader:
            inn = (row.get("ИНН") or "").strip()
            if inn and inn in inn_to_group:
                row["Группа_Компаний"] = inn_to_group[inn]
                updated += 1
            rows.append(row)

    with COMPANIES_WITH_EXPORT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    print(f"Обновлено строк в companies_with_export.csv: {updated}")
    print(f"Записано: {COMPANIES_WITH_EXPORT_CSV}")


if __name__ == "__main__":
    main()
