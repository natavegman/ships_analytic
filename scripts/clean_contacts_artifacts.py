#!/usr/bin/env python3
"""
Очистка company_groups_enriched.csv от артефактов парсинга в контактах.
Удаляет нереальные данные типа "(скромный)", "(минимальный)", "(заметный)" и т.п.
"""

import csv
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
ENRICHED_CSV = BASE_DIR / "data" / "company_groups_enriched.csv"
BACKUP_CSV = BASE_DIR / "data" / "company_groups_enriched.csv.backup2"


def clean_contacts(contacts: str) -> str:
    """Очищает контакты от артефактов парсинга."""
    if not contacts:
        return ""
    
    # Разбиваем на части по ";"
    parts = contacts.split(";")
    cleaned_parts = []
    
    for part in parts:
        part = part.strip()
        if not part:
            continue
        
        # Пропускаем артефакты
        invalid_patterns = [
            r"^Телефон:\s*\(?(скромный|минимальный|заметный)\)?$",
            r"^Телефон:\s*(доля|показать|с какой даты|сумма к оплате)",
            r"^Телефон:\s*(рыболовство|переработка|торговля|деятельность)",
            r"^Телефон:\s*[А-ЯЁ][а-яё]+\s+[А-ЯЁ]\.[А-ЯЁ]\.\s*\([0-9%]+",  # Имена с долями
            r"^Телефон:\s*[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+\s*\([а-яё]+\)",  # Имена с должностями в скобках
        ]
        
        # Проверяем, является ли это артефактом
        is_artifact = False
        for pattern in invalid_patterns:
            if re.search(pattern, part, re.IGNORECASE):
                is_artifact = True
                break
        
        # Также проверяем, содержит ли часть реальные данные
        if not is_artifact:
            # Телефон должен содержать цифры
            if part.startswith("Телефон:") and not re.search(r'\d', part):
                is_artifact = True
            # Адрес должен содержать ключевые слова
            elif part.startswith("Адрес:") and not any(kw in part.lower() for kw in ["ул.", "улица", "проспект", "дом", "город", "г.", "область"]):
                is_artifact = True
            # Сайт должен содержать доменное имя
            elif part.startswith("Сайт:") and not re.search(r'\.(ru|com|org|net|рф)', part, re.IGNORECASE):
                is_artifact = True
        
        if not is_artifact:
            cleaned_parts.append(part)
    
    return "; ".join(cleaned_parts)


def clean_director(director: str) -> str:
    """Очищает имя директора от артефактов."""
    if not director:
        return ""
    
    # Убираем артефакты
    invalid_patterns = [
        r"^\(?(скромный|минимальный|заметный|доля|показать)\)?$",
    ]
    
    director_clean = director.strip()
    for pattern in invalid_patterns:
        if re.match(pattern, director_clean, re.IGNORECASE):
            return ""
    
    # Проверяем, что это похоже на имя (содержит буквы и пробелы)
    if re.search(r'[а-яёА-ЯЁ]', director_clean) and len(director_clean.split()) >= 2:
        return director_clean
    
    return ""


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
            original_contacts = row.get("Контакты_ListOrg", "")
            original_director = row.get("Директор_ListOrg", "")
            
            cleaned_contacts = clean_contacts(original_contacts)
            cleaned_director = clean_director(original_director)
            
            if cleaned_contacts != original_contacts or cleaned_director != original_director:
                cleaned_count += 1
                print(f"  Очищено: {row.get('Юр_Лицо', '')} (ИНН: {row.get('ИНН', '')})")
            
            row["Контакты_ListOrg"] = cleaned_contacts
            row["Директор_ListOrg"] = cleaned_director
            rows.append(row)
    
    # Сохраняем очищенные данные
    with ENRICHED_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    
    print(f"\n✓ Очистка завершена")
    print(f"  Очищено записей: {cleaned_count}")
    print(f"  Всего записей: {len(rows)}")
    print(f"\nФайл сохранен: {ENRICHED_CSV}")
    print(f"Backup сохранен: {BACKUP_CSV}")


if __name__ == "__main__":
    main()
