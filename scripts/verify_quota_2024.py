"""
Verify NBAMR quota 2024 anomaly.

Квота 2024 = 5.7к т выглядит странно низко.
Возможные причины:
1. Пробел в выгрузке Росрыболовства (данные неполные)
2. 2024 — переходный год (квота пересмотрена)
3. Данные поступили поздно в CSV

Проверяем:
- Количество строк в quota_summary.csv для 2024
- Сравнение с 2023/2025
- Структура типов квот и договоров
- Наличие корректировок (Причина_Изменения)

Usage:
    python3 scripts/verify_quota_2024.py
    python3 scripts/verify_quota_2024.py --export report_2024.json
"""

import csv
import json
from pathlib import Path
from collections import defaultdict
import logging

logging.basicConfig(level=logging.INFO, format='%(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

ROOT = Path(__file__).parent.parent


def analyze_quota_2024():
    """Проанализировать квоту НБАМР за 2024."""
    csv_path = ROOT / "output" / "quota_summary.csv"
    inn = "2508007948"

    if not csv_path.exists():
        logger.error(f"Файл не найден: {csv_path}")
        return None

    logger.info(f"Анализирую квоту НБАМР (ИНН {inn}) по годам...")

    csv.field_size_limit(10 ** 7)
    all_rows = list(csv.DictReader(open(csv_path, encoding="utf-8")))

    result = {
        "company": "ПАО НБАМР",
        "inn": inn,
        "years": {}
    }

    for year in ["2023", "2024", "2025", "2026"]:
        year_rows = [r for r in all_rows if r.get("ИНН") == inn and r.get("Год") == year]

        if not year_rows:
            logger.warning(f"{year}: данных не найдено")
            continue

        by_type = defaultdict(float)
        by_species = defaultdict(float)
        by_reason = defaultdict(int)

        for r in year_rows:
            tp = r.get("Тип_Квоты") or "unknown"
            sp = r.get("Объект_Лова") or "unknown"
            reason = r.get("Причина_Изменения") or "(нет)"
            vol = float(r.get("Объем_Тонн") or 0)

            by_type[tp] += vol
            by_species[sp] += vol
            by_reason[reason] += 1

        total = sum(by_type.values())
        mintai = by_species.get("Минтай", 0)

        result["years"][year] = {
            "rows_count": len(year_rows),
            "total_t": round(total, 1),
            "mintai_t": round(mintai, 1),
            "quota_types": dict(by_type),
            "species": dict(by_species),
            "changes_reason_count": dict(by_reason),
        }

        logger.info(f"""
{year}: {len(year_rows)} строк, {total:>10.1f} т (минтай {mintai:>8.1f} т)
  По типам: {', '.join(f'{k}={v:.0f}' for k,v in sorted(by_type.items()))}
  Причины: {', '.join(f'{k}={v}' for k,v in sorted(by_reason.items()))}
        """)

    # Анализ аномалии
    y23_t = result["years"].get("2023", {}).get("total_t")
    y24_t = result["years"].get("2024", {}).get("total_t")
    y25_t = result["years"].get("2025", {}).get("total_t")

    result["anomaly"] = {
        "2024_vs_2023": f"{y24_t/y23_t*100:.1f}%" if y23_t else "N/A",
        "2024_vs_2025": f"{y24_t/y25_t*100:.1f}%" if y25_t else "N/A",
        "assessment": "СТРАННО НИЗКО" if y24_t and y24_t < y25_t * 0.1 else "OK",
        "likely_cause": """
        Вероятные причины:
        1. Пробел в выгрузке приказов Росрыболовства за 2024
        2. 2024 — переходный год (квота пересматривалась)
        3. Данные поступили неполные или поздно в CSV
        4. НБАМР сдала часть квоты другим компаниям

        Действие: Перепроверить в источнике
        - https://www.rosrybolovstvo.ru/ (приказы)
        - Запрос в Росрыболовство
        - СПАРК/Дата.ру выписка по квотам
        - Контакт в НБАМР напрямую
        """
    }

    return result


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Verify NBAMR 2024 quota anomaly")
    parser.add_argument("--export", help="Export analysis to JSON")
    args = parser.parse_args()

    result = analyze_quota_2024()

    if args.export:
        out_path = Path(args.export)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        logger.info(f"Сохранено: {out_path}")

    logger.info(f"""
    ===== ВЫВОД =====

    Квота 2024: {result['years'].get('2024', {}).get('total_t')} т
    (2023: {result['years'].get('2023', {}).get('total_t')} т)
    (2025: {result['years'].get('2025', {}).get('total_t')} т)

    {result['anomaly']['assessment']}

    {result['anomaly']['likely_cause']}
    """)


if __name__ == "__main__":
    main()
