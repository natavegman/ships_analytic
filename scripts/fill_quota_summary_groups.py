#!/usr/bin/env python3
"""
Заполняет колонку Группа_Компаний в output/quota_summary.csv по совпадению ИНН
с data/company_groups_enriched.csv и data/company_groups.csv.

Используются только строки, где группа уже задана (не пустая).
Запуск: python3 scripts/fill_quota_summary_groups.py
"""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
ENRICHED_CSV = BASE_DIR / "data" / "company_groups_enriched.csv"
GROUPS_CSV = BASE_DIR / "data" / "company_groups.csv"
QUOTA_SUMMARY_CSV = BASE_DIR / "output" / "quota_summary.csv"


def main() -> None:
    import csv

    # Строим маппинг ИНН -> Группа_Компаний (только непустые группы)
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
        print("Нет ни одной записи с группой в company_groups_enriched.csv и company_groups.csv")
        return

    print(f"Загружено ИНН с группой: {len(inn_to_group)}")

    if not QUOTA_SUMMARY_CSV.exists():
        print(f"Файл не найден: {QUOTA_SUMMARY_CSV}")
        return

    rows: list[dict[str, str]] = []
    filled = 0
    with QUOTA_SUMMARY_CSV.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        for row in reader:
            inn = (row.get("ИНН") or "").strip()
            if inn and inn in inn_to_group:
                row["Группа_Компаний"] = inn_to_group[inn]
                filled += 1
            rows.append(row)

    with QUOTA_SUMMARY_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Заполнено строк в quota_summary.csv: {filled}")
    print(f"Записано: {QUOTA_SUMMARY_CSV}")


if __name__ == "__main__":
    main()
