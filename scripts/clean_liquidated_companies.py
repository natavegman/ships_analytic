#!/usr/bin/env python3
"""
Очистка company_groups_enriched.csv от ликвидированных организаций.
Проверяет статус организаций и удаляет ликвидированные.
"""

import csv
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
ENRICHED_CSV = BASE_DIR / "data" / "company_groups_enriched.csv"
BACKUP_CSV = BASE_DIR / "data" / "company_groups_enriched.csv.backup"


def is_liquidated(row: dict) -> bool:
    """Проверяет, является ли организация ликвидированной по данным в строке."""
    # Проверяем в комментариях и контактах
    comment = (row.get("Комментарий") or "").lower()
    contacts = (row.get("Контакты_ListOrg") or "").lower()
    combined = comment + " " + contacts
    
    liquidation_keywords = [
        "ликвидир",
        "прекращена деятельность",
        "исключен",
        "исключена",
        "реорганизован",
        "реорганизована",
        "в процессе ликвидации",
        "ликвидация",
    ]
    
    return any(keyword in combined for keyword in liquidation_keywords)


def main() -> None:
    if not ENRICHED_CSV.exists():
        print(f"Файл {ENRICHED_CSV} не найден")
        return
    
    # Создаем backup
    import shutil
    shutil.copy2(ENRICHED_CSV, BACKUP_CSV)
    print(f"Создан backup: {BACKUP_CSV}")
    
    # Читаем данные
    rows = []
    liquidated_count = 0
    
    with ENRICHED_CSV.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        if not fieldnames:
            print("Ошибка: файл не содержит заголовков")
            return
        
        for row in reader:
            if is_liquidated(row):
                liquidated_count += 1
                print(f"  Ликвидирована: {row.get('Юр_Лицо', '')} (ИНН: {row.get('ИНН', '')})")
            else:
                rows.append(row)
    
    # Сохраняем очищенные данные
    with ENRICHED_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    
    print(f"\n✓ Очистка завершена")
    print(f"  Всего записей: {len(rows) + liquidated_count}")
    print(f"  Ликвидированных удалено: {liquidated_count}")
    print(f"  Действующих осталось: {len(rows)}")
    print(f"\nФайл сохранен: {ENRICHED_CSV}")
    print(f"Backup сохранен: {BACKUP_CSV}")


if __name__ == "__main__":
    main()
