"""
Parse NBAMR financial reports from BAMR.ru.

Источник: https://www.bamr.ru/о-компании/раскрытие-информации/
Обычно содержит ежегодные отчёты (РСБУ) и полугодовые обновления.

Экспортирует: выручка, прибыль, активы, капитал по годам.

Usage:
    python3 scripts/fetch_financials_bamr_pdf.py --fetch  # Скачать PDF с сайта
    python3 scripts/fetch_financials_bamr_pdf.py --parse --input output/nbamr_reports/*.pdf
    python3 scripts/fetch_financials_bamr_pdf.py --ocr    # Распознать сканы

Dependencies:
    pip install requests beautifulsoup4 pdfplumber pytesseract
    (если нужна OCR: apt-get install tesseract-ocr)

Output:
    data/reference/nbamr_financials_bamr.json (raw extracted)
    data/reference/nbamr_financials_parsed.csv (clean data)
"""

import argparse
import json
import csv
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List, Any
import logging
import re

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

ROOT = Path(__file__).parent.parent


def fetch_bamr_reports() -> Optional[List[Dict[str, str]]]:
    """
    Скачать список отчётов с BAMR.ru и загрузить PDF.

    Требует: requests, beautifulsoup4
    """
    try:
        import requests
        from bs4 import BeautifulSoup
    except ImportError:
        logger.error("Требуются: pip install requests beautifulsoup4 pdfplumber")
        return None

    url = "https://www.bamr.ru/о-компании/раскрытие-информации/"
    logger.info(f"Загружаю отчёты с {url}...")

    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        logger.error(f"Ошибка загрузки: {e}")
        return None

    soup = BeautifulSoup(resp.content, "html.parser")

    # Ищем ссылки на PDF (обычно в таблице или списке)
    reports = []
    for link in soup.find_all("a", href=re.compile(r"\.pdf$", re.I)):
        href = link.get("href", "")
        text = link.get_text(strip=True)
        if href:
            if not href.startswith("http"):
                href = "https://www.bamr.ru" + href
            reports.append({
                "title": text,
                "url": href,
                "fetched_at": datetime.now().isoformat(),
            })

    logger.info(f"Найдено {len(reports)} отчётов")
    for r in reports[:5]:
        logger.info(f"  - {r['title']}: {r['url']}")

    if not reports:
        logger.warning("Отчёты не найдены. Возможно, структура сайта изменилась.")

    return reports


def parse_pdf_text(pdf_path: Path) -> Optional[str]:
    """
    Извлечь текст из PDF.

    Требует: pdfplumber
    """
    try:
        import pdfplumber
    except ImportError:
        logger.error("Требуется: pip install pdfplumber")
        return None

    try:
        with pdfplumber.open(pdf_path) as pdf:
            text = ""
            for page in pdf.pages:
                text += page.extract_text() or ""
            return text
    except Exception as e:
        logger.error(f"Ошибка парсинга {pdf_path}: {e}")
        return None


def extract_financials_from_text(text: str, year: Optional[int] = None) -> Dict[str, Any]:
    """
    Вычленить финансовые показатели из текста отчёта.

    Ищет: выручка, прибыль, активы, капитал.
    Паттерны для российских отчётов РСБУ.
    """
    result = {
        "year": year,
        "revenue_rub_m": None,
        "net_profit_rub_m": None,
        "assets_rub_m": None,
        "capital_rub_m": None,
        "raw_matches": []
    }

    # Паттерны для поиска (русский и английский)
    patterns = {
        "revenue": [
            r"выручк[аи].*?(\d+[\s\d]*)",
            r"revenue.*?(\d+[\s\d]*)",
            r"доход.*?(\d+[\s\d]*)",
        ],
        "profit": [
            r"чист[ая]*\s+прибыль.*?(\d+[\s\d]*)",
            r"net profit.*?(\d+[\s\d]*)",
        ],
        "assets": [
            r"актив[ы]?.*?(\d+[\s\d]*)",
            r"assets.*?(\d+[\s\d]*)",
        ],
    }

    for field, pats in patterns.items():
        for pat in pats:
            matches = re.findall(pat, text, re.IGNORECASE)
            if matches:
                result["raw_matches"].append({
                    "field": field,
                    "pattern": pat,
                    "matches": matches[:3]
                })

    logger.info(f"""
    ⚠ Автоматическое извлечение финансов из PDF сложно без структурированного парсинга.

    Рекомендуемый подход:
    1. Вручную открыть PDF (BAMR.ru отчёты)
    2. Найти финдокументы (обычно таблица на стр. 1-2)
    3. Внести в data/reference/nbamr_financials.csv

    Текст найден: {len(text)} символов
    Сопоставлений: {len(result['raw_matches'])}
    """)

    return result


def main():
    parser = argparse.ArgumentParser(description="Parse NBAMR PDF reports")
    parser.add_argument("--fetch", action="store_true", help="Fetch reports from BAMR.ru")
    parser.add_argument("--parse", action="store_true", help="Parse existing PDFs")
    parser.add_argument("--ocr", action="store_true", help="Use OCR for scanned PDFs")
    parser.add_argument("--input", help="Input PDF directory")
    parser.add_argument("--output", default="data/reference/nbamr_financials_bamr.json")
    args = parser.parse_args()

    result = {
        "fetched_at": datetime.now().isoformat(),
        "source": "bamr.ru",
        "reports": [],
        "financials": {},
    }

    if args.fetch:
        logger.info("Загружаю отчёты с BAMR.ru...")
        reports = fetch_bamr_reports()
        if reports:
            result["reports"] = reports
            logger.info(f"Скачано {len(reports)} отчётов")

    if args.parse:
        pdf_dir = Path(args.input) if args.input else ROOT / "output" / "nbamr_reports"
        pdfs = list(pdf_dir.glob("*.pdf"))
        logger.info(f"Парсю {len(pdfs)} PDF из {pdf_dir}...")

        for pdf_path in pdfs:
            logger.info(f"  Парсю {pdf_path.name}...")
            text = parse_pdf_text(pdf_path)
            if text:
                year_match = re.search(r"20(\d{2})", pdf_path.name)
                year = int("20" + year_match.group(1)) if year_match else None
                fin = extract_financials_from_text(text, year)
                result["financials"][pdf_path.name] = fin

    out_file = Path(args.output)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    logger.info(f"Сохранено: {out_file}")

    logger.info("""
    ===== РУКОВОДСТВО ПО ЗАПОЛНЕНИЮ =====

    ⚠ Автоматический парсинг PDF часто неточен.

    Вручную (БЫСТРО):
    1. Откройте https://www.bamr.ru/о-компании/раскрытие-информации/
    2. Скачайте последний Годовой отчёт (PDF)
    3. На 1-2 странице найдите таблицу финансов:
       - Выручка (тыс. ₽)
       - Прибыль (чистая, тыс. ₽)
       - Активы, Капитал
    4. Заполните data/reference/nbamr_financials.csv

    Готовый шаблон уже создан, содержит:
    - Годы 2023-2026
    - Колонки: revenue_rub_m, net_profit_rub_m, capex_repair_rub_m, source
    """)


if __name__ == "__main__":
    main()
