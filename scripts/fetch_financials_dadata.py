"""
Fetch NBAMR financials from Дата.ру SPARK API.

Загружает финансовые показатели за последние годы для:
- ПАО НБАМР (2508007948)
- ООО Мерлион (2723194055)
- ООО Аква-инвест (2508131550)

SPARK API содержит:
- Выручку, прибыль, активы, баланс
- Судебные дела, претензии
- Контрагентов, связанные компании

Usage:
    python3 scripts/fetch_financials_dadata.py --companies nbamr,merlion,akva-invest
    python3 scripts/fetch_financials_dadata.py --companies 2508007948,2723194055,2508131550 --output data/reference/nbamr_financials_dadata.json

Env:
    DADATA_API_KEY
    DADATA_SECRET_KEY
"""

import argparse
import json
import os
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

ROOT = Path(__file__).parent.parent

COMPANIES = {
    "nbamr": "2508007948",
    "merlion": "2723194055",
    "akva-invest": "2508131550",
}


def fetch_spark(inn: str) -> Optional[Dict[str, Any]]:
    """
    Загрузить финансы через Дата.ру SPARK API.

    SPARK — это платная подписка Дата.ру, требует отдельных ключей.
    Возвращает: выручка, прибыль, активы, баланс по годам.
    """
    api_key = os.getenv("DADATA_API_KEY")
    secret_key = os.getenv("DADATA_SECRET_KEY")

    if not api_key or not secret_key:
        logger.error("Дата.ру ключи не заданы. Установите DADATA_API_KEY и DADATA_SECRET_KEY")
        return None

    try:
        from dadata import Dadata
        client = Dadata(api_key, secret_key)

        logger.info(f"Запрос финансов ИНН {inn}...")

        # SPARK API требует платную подписку
        # Базовый find_by_id не содержит финансы
        # Нужно использовать find_affiliated для связанных компаний
        # или заказать доступ к SPARK в личном кабинете Дата.ру

        result = client.find_by_id(name="party", query=inn)
        if not result:
            logger.error(f"Компания ИНН {inn} не найдена")
            return None

        data = result[0].get("data", {})

        # ⚠ Базовый API не содержит ФИНАНСОВЫЕ ПОКАЗАТЕЛИ
        # Для получения выручки, прибыли требуется:
        # 1. Платная подписка на SPARK (от 5000 ₽/мес)
        # 2. Или использовать другой источник (ЕГРЮЛ, ListOrg, BAMR.ru)

        logger.warning("""
        ⚠ Дата.ру базовый API НЕ содержит финансовые показатели.

        Для SPARK:
        1. Перейти в https://dadata.ru/api/access/
        2. Заказать платный доступ к SPARK (выручка, прибыль, баланс)
        3. Использовать отдельный endpoint для финансов

        Альтернатива: fetch_financials_egrul.py или fetch_financials_bamr_pdf.py
        """)

        return {
            "source": "dadata_basic",
            "inn": inn,
            "company_name": data.get("name", {}).get("full_with_opf"),
            "status": data.get("state", {}).get("status"),
            "fetched_at": datetime.now().isoformat(),
            "financials": None,  # Требуется SPARK платная подписка
            "note": "Базовый API. Для финансов нужна платная SPARK подписка Дата.ру."
        }

    except Exception as e:
        logger.error(f"Ошибка Дата.ру: {e}")
        return None


def fetch_spark_batch(inns: list) -> Dict[str, Any]:
    """Загрузить финансы для нескольких ИНН."""
    result = {
        "fetched_at": datetime.now().isoformat(),
        "companies": {}
    }

    for inn in inns:
        logger.info(f"\n=== ИНН {inn} ===")
        data = fetch_spark(inn)
        if data:
            result["companies"][inn] = data

    return result


def main():
    parser = argparse.ArgumentParser(description="Fetch NBAMR financials from Дата.ру SPARK")
    parser.add_argument("--companies", default="nbamr,merlion,akva-invest",
                        help="Comma-separated company names or INN codes")
    parser.add_argument("--output", default="data/reference/nbamr_financials_dadata.json")
    args = parser.parse_args()

    # Resolve company codes
    inns = []
    for comp in args.companies.split(","):
        comp = comp.strip()
        if comp in COMPANIES:
            inns.append(COMPANIES[comp])
        elif len(comp) in (10, 12) and comp.isdigit():
            inns.append(comp)
        else:
            logger.warning(f"Неизвестная компания: {comp}")

    if not inns:
        logger.error("Не указаны ИНН компаний")
        return

    logger.info(f"Загружаю финансы для: {', '.join(inns)}")
    result = fetch_spark_batch(inns)

    out_file = Path(args.output)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    logger.info(f"Сохранено: {out_file}")

    logger.info("""
    ===== РЕЗУЛЬТАТ =====

    ⚠ Дата.ру базовый API не содержит финансовые показатели.

    Решение:
    1. Заказать SPARK подписку (платно)
    2. Или использовать альтернативные источники:
       - fetch_financials_bamr_pdf.py (парсинг BAMR.ru PDF)
       - fetch_financials_egrul.py (ЕГРЮЛ выписки)
    """)


if __name__ == "__main__":
    main()
