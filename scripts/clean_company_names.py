#!/usr/bin/env python3
"""
Очистка названий компаний от префикса "Контрагент" в company_groups_enriched.csv.
"""

import csv
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
ENRICHED_CSV = BASE_DIR / "data" / "company_groups_enriched.csv"
BACKUP_CSV = BASE_DIR / "data" / "company_groups_enriched.csv.backup3"


def clean_company_name(name: str) -> str:
    """Очищает название компании от префикса 'Контрагент'."""
    if not name:
        return ""
    
    cleaned = name.strip()
    # Убираем префикс "Контрагент" если есть
    if cleaned.startswith("Контрагент"):
        cleaned = cleaned.replace("Контрагент", "").strip()
    
    return cleaned


def main() -> None:
    if not ENRICHED_CSV.exists():
        print(f"Файл {ENRICHED_CSV} не найден")
        return
    
    # Создаем backup
    import shutil
    shutil.copy2(ENRICHED_CSV, BACKUP_CSV)
    print(f"Создан backup: {BACKUP_CSV}")
    
    # Читаем и очищаем данные
    rows = []
    cleaned_count = 0
    
    with ENRICHED_CSV.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        if not fieldnames:
            print("Ошибка: файл не содержит заголовков")
            return
        
        for row in reader:
            original_name = row.get("Юр_Лицо", "")
            cleaned_name = clean_company_name(original_name)
            
            if cleaned_name != original_name:
                cleaned_count += 1
                print(f"  Очищено: '{original_name}' -> '{cleaned_name}'")
            
            row["Юр_Лицо"] = cleaned_name
            rows.append(row)
    
    # Сохраняем очищенные данные
    with ENRICHED_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    
    print(f"\n✓ Очистка завершена")
    print(f"  Очищено названий: {cleaned_count}")
    print(f"  Всего записей: {len(rows)}")
    print(f"\nФайл сохранен: {ENRICHED_CSV}")
    print(f"Backup сохранен: {BACKUP_CSV}")


if __name__ == "__main__":
    main()
