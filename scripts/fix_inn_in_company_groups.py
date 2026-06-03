#!/usr/bin/env python3
"""
Исправление ИНН в data/company_groups.csv на основе реальных данных из output/quota_summary.csv.

Скрипт:
1. Загружает уникальные пары (Юр_Лицо, ИНН) из quota_summary.csv
2. Нормализует названия компаний для сопоставления
3. Сопоставляет компании из company_groups.csv с данными из quota_summary.csv
4. Обновляет ИНН в company_groups.csv
"""

import csv
import re
from pathlib import Path
from typing import Dict, Tuple, Optional

BASE_DIR = Path(__file__).resolve().parents[1]
QUOTA_CSV = BASE_DIR / "output" / "quota_summary.csv"
GROUPS_CSV = BASE_DIR / "data" / "company_groups.csv"
BACKUP_CSV = BASE_DIR / "data" / "company_groups.csv.backup"


def normalize_company_name(name: str) -> str:
    """
    Нормализует название компании для сопоставления:
    - Убирает кавычки
    - Приводит к верхнему регистру
    - Убирает лишние пробелы
    - Убирает ООО/АО/ИП и т.п. для более гибкого сопоставления
    """
    if not name:
        return ""
    # Убираем кавычки
    name = name.replace('"', "").replace("«", "").replace("»", "")
    # Приводим к верхнему регистру
    name = name.upper().strip()
    # Убираем лишние пробелы
    name = re.sub(r"\s+", " ", name)
    # Убираем префиксы ООО/АО/ИП для более гибкого сопоставления
    name = re.sub(r"^(ООО|АО|ИП|ПАО|ЗАО)\s+", "", name)
    return name.strip()


def load_quota_companies() -> Dict[str, str]:
    """
    Загружает уникальные пары (нормализованное_название -> ИНН) из quota_summary.csv.
    Если для одного названия есть несколько ИНН, берем наиболее частый.
    """
    name_to_inn: Dict[str, Dict[str, int]] = {}  # normalized_name -> {inn: count}
    
    with QUOTA_CSV.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            legal_name = (row.get("Юр_Лицо") or "").strip()
            inn = (row.get("ИНН") or "").strip()
            if not legal_name or not inn:
                continue
            
            normalized = normalize_company_name(legal_name)
            if not normalized:
                continue
            
            if normalized not in name_to_inn:
                name_to_inn[normalized] = {}
            name_to_inn[normalized][inn] = name_to_inn[normalized].get(inn, 0) + 1
    
    # Выбираем наиболее частый ИНН для каждого названия
    result: Dict[str, str] = {}
    for normalized_name, inn_counts in name_to_inn.items():
        most_common_inn = max(inn_counts.items(), key=lambda x: x[1])[0]
        result[normalized_name] = most_common_inn
    
    return result


def load_company_groups() -> list[Dict[str, str]]:
    """Загружает company_groups.csv в список словарей."""
    rows = []
    if not GROUPS_CSV.exists():
        return rows
    
    with GROUPS_CSV.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def find_matching_inn(
    company_name: str, quota_map: Dict[str, str]
) -> Tuple[Optional[str], str]:
    """
    Ищет соответствующий ИНН для названия компании.
    Возвращает (ИНН, способ_сопоставления).
    """
    normalized = normalize_company_name(company_name)
    
    # Точное совпадение
    if normalized in quota_map:
        return quota_map[normalized], "точное_совпадение"
    
    # Частичное совпадение - ищем компании, которые содержат ключевые слова
    # Извлекаем ключевые слова из названия (убираем общие слова)
    words = normalized.split()
    key_words = [
        w for w in words
        if len(w) > 3 and w not in ["ГРУППА", "КОМПАНИЯ", "ОБЪЕДИНЕНИЕ"]
    ]
    
    if key_words:
        # Ищем компании, содержащие хотя бы одно ключевое слово
        best_match = None
        best_score = 0
        
        for quota_name, quota_inn in quota_map.items():
            score = sum(1 for word in key_words if word in quota_name)
            if score > best_score and score >= len(key_words) * 0.5:  # хотя бы половина слов совпадает
                best_score = score
                best_match = quota_inn
        
        if best_match:
            return best_match, f"частичное_совпадение_{best_score}"
    
    return None, "не_найдено"


def main() -> None:
    print("Загрузка данных из quota_summary.csv...")
    quota_map = load_quota_companies()
    print(f"Найдено {len(quota_map)} уникальных компаний в quota_summary.csv")
    
    print("\nЗагрузка company_groups.csv...")
    groups_rows = load_company_groups()
    print(f"Загружено {len(groups_rows)} записей из company_groups.csv")
    
    # Создаем backup
    if GROUPS_CSV.exists():
        import shutil
        shutil.copy2(GROUPS_CSV, BACKUP_CSV)
        print(f"\nСоздан backup: {BACKUP_CSV}")
    
    # Обновляем ИНН
    updated_count = 0
    not_found_count = 0
    
    print("\nСопоставление компаний и обновление ИНН...")
    for row in groups_rows:
        company_name = row.get("Юр_Лицо", "").strip()
        old_inn = row.get("ИНН", "").strip()
        
        if not company_name:
            continue
        
        new_inn, match_type = find_matching_inn(company_name, quota_map)
        
        if new_inn:
            if old_inn != new_inn:
                print(f"  {company_name}")
                print(f"    Старый ИНН: {old_inn}")
                print(f"    Новый ИНН: {new_inn} ({match_type})")
                row["ИНН"] = new_inn
                updated_count += 1
            else:
                print(f"  {company_name}: ИНН уже правильный ({old_inn})")
        else:
            print(f"  {company_name}: ИНН не найден в quota_summary.csv (текущий: {old_inn})")
            not_found_count += 1
    
    # Сохраняем обновленный файл
    if groups_rows:
        fieldnames = list(groups_rows[0].keys())
        with GROUPS_CSV.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(groups_rows)
        
        print(f"\n✓ Обновлено ИНН: {updated_count}")
        print(f"✗ Не найдено в quota_summary.csv: {not_found_count}")
        print(f"\nФайл сохранен: {GROUPS_CSV}")
        print(f"Backup сохранен: {BACKUP_CSV}")
    else:
        print("\nОшибка: не удалось загрузить данные из company_groups.csv")


if __name__ == "__main__":
    main()
