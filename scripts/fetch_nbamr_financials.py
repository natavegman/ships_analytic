"""
Fetch NBAMR financials from multiple sources:
A) Дата.ру SPARK API (если ключи заданы)
B) Parse BAMR.ru PDF reports
C) ЕГРЮЛ/ФНС выписка (справочно)

Usage:
  python3 scripts/fetch_nbamr_financials.py --mode dadata
  python3 scripts/fetch_nbamr_financials.py --mode bamr_pdf --save-pdf
  python3 scripts/fetch_nbamr_financials.py --mode egrul

Output: data/reference/nbamr_financials_raw.json (по источникам)
Deduplicate → data/reference/nbamr_financials.csv
"""

import argparse
import json
import os
import csv
from pathlib import Path
from datetime import datetime
from typing import Optional
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

ROOT = Path(__file__).parent.parent


# ============ A) Дата.ру SPARK API ============
def fetch_dadata_spark(inn: str = "2508007948") -> Optional[dict]:
    """
    Попытка загрузить финансы через Дата.ру SPARK API.
    Требует: DADATA_API_KEY, DADATA_SECRET_KEY
    ⚠ SPARK API часто платная. Базовый API НЕ возвращает выручку.
    """
    api_key = os.getenv("DADATA_API_KEY")
    secret_key = os.getenv("DADATA_SECRET_KEY")

    if not api_key or not secret_key:
        logger.warning("Дата.ру ключи не заданы (DADATA_API_KEY, DADATA_SECRET_KEY)")
        return None

    try:
        from dadata import Dadata
        client = Dadata(api_key, secret_key)

        # Получаем базовую информацию
        result = client.find_by_id(name="party", query=inn)
        if not result:
            logger.error(f"Дата.ру: компания ИНН {inn} не найдена")
            return None

        data = result[0].get("data", {})

        # ⚠ Базовый API НЕ содержит выручку
        # Для SPARK нужна отдельная подписка или парсинг JSON-RPC
        logger.info("⚠ Базовый Дата.ру API не содержит выручку. Требуется SPARK подписка.")

        return {
            "source": "dadata_basic",
            "inn": inn,
            "fetched_at": datetime.now().isoformat(),
            "data": {
                "name_full": data.get("name", {}).get("full_with_opf"),
                "status": data.get("state", {}).get("status"),
                "capital": data.get("capital"),
            },
            "note": "Основной API не содержит финансы. Для выручки требуется SPARK подписка (платная)."
        }
    except Exception as e:
        logger.error(f"Ошибка Дата.ру: {e}")
        return None


# ============ B) Парсинг BAMR.ru PDF ============
def fetch_bamr_pdf(save_dir: Optional[Path] = None) -> Optional[dict]:
    """
    Скачивание и парсинг PDF отчётов с BAMR.ru.

    URL: https://www.bamr.ru/о-компании/раскрытие-информации/

    Выход ожидается:
    - Ежегодный отчёт (выручка, прибыль, активы, баланс)
    - Полугодовой отчёт (текущий год)

    Требует: requests, pdfplumber или PyPDF2
    """
    if save_dir is None:
        save_dir = ROOT / "output" / "nbamr_reports"
    save_dir.mkdir(parents=True, exist_ok=True)

    try:
        import requests
    except ImportError:
        logger.error("requests не установлен: pip install requests")
        return None

    bamr_url = "https://www.bamr.ru/о-компании/раскрытие-информации/"

    logger.info(f"Попытка загрузить список отчётов с {bamr_url}...")
    logger.warning("""
    ⚠ Парсинг требует:
    1. requests (для скачивания)
    2. BeautifulSoup4 (для парсинга HTML)
    3. pdfplumber или PyPDF2 (для чтения PDF)
    4. pytesseract + tesseract-ocr (если отчёты сканы)

    Установка: pip install requests beautifulsoup4 pdfplumber pytesseract
    """)

    return {
        "source": "bamr_pdf",
        "url": bamr_url,
        "fetched_at": datetime.now().isoformat(),
        "status": "pending",
        "note": "Требуется реализация парсинга HTML + PDF с сайта BAMR.ru"
    }


# ============ C) ЕГРЮЛ/ФНС ============
def fetch_egrul(inn: str = "2508007948") -> Optional[dict]:
    """
    Справочная выписка из ЕГРЮЛ.
    Обычно платно (50-150 ₽) или требует ручной работы.

    Альтернатива: открытый реестр ФНС
    https://egrul.nalog.ru/ (сканирование вручную)
    """
    logger.info(f"""
    ЕГРЮЛ Выписка для ИНН {inn}:

    Способ 1: Ручной поиск
    - Сайт: https://egrul.nalog.ru/
    - Введите ИНН {inn}
    - Скачайте выписку

    Способ 2: API (платно)
    - Сервис: https://api.kontur.ru/ (Контур)
    - Стоимость: ~100 ₽/запрос

    Способ 3: СПАРК API (платно)
    - Дата.ру SPARK
    - Стоимость: тариф
    """)

    return {
        "source": "egrul_manual",
        "inn": inn,
        "fetched_at": datetime.now().isoformat(),
        "status": "manual",
        "note": "ЕГРЮЛ требует ручной работы или платной подписки на API"
    }


# ============ Merge ============
def merge_financials(dadata: Optional[dict] = None,
                     bamr: Optional[dict] = None,
                     egrul: Optional[dict] = None) -> dict:
    """Объединить данные из всех источников."""
    return {
        "fetched_at": datetime.now().isoformat(),
        "nbamr_inn": "2508007948",
        "sources": {
            "dadata_spark": dadata,
            "bamr_pdf": bamr,
            "egrul": egrul,
        }
    }


def main():
    parser = argparse.ArgumentParser(description="Fetch NBAMR financials")
    parser.add_argument("--mode", choices=["dadata", "bamr_pdf", "egrul", "all"], default="all")
    parser.add_argument("--save-pdf", action="store_true", help="Download and save BAMR PDF")
    parser.add_argument("--output", default="data/reference/nbamr_financials_raw.json")
    args = parser.parse_args()

    result = {}

    if args.mode in ("dadata", "all"):
        logger.info("== Дата.ру SPARK ==")
        result["dadata"] = fetch_dadata_spark()

    if args.mode in ("bamr_pdf", "all"):
        logger.info("== BAMR.ru PDF ==")
        result["bamr"] = fetch_bamr_pdf()

    if args.mode in ("egrul", "all"):
        logger.info("== ЕГРЮЛ ==")
        result["egrul"] = fetch_egrul()

    merged = merge_financials(
        dadata=result.get("dadata"),
        bamr=result.get("bamr"),
        egrul=result.get("egrul")
    )

    out_file = Path(args.output)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

    logger.info(f"Сохранено: {out_file}")

    # Подсказка
    logger.info(f"""
    ===== ЧТО ДАЛЬШЕ =====

    1. Дата.ру: заполните env vars для SPARK API или используйте другой источник
    2. BAMR.ru: скачайте PDF с сайта, распознайте текст (OCR если надо)
    3. ЕГРЮЛ: заполните вручную или закажите выписку

    После заполнения данных:
    → python3 scripts/parse_nbamr_pdf.py --input output/nbamr_reports/ --output data/reference/nbamr_financials.csv
    """)


if __name__ == "__main__":
    main()
