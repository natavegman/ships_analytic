#!/usr/bin/env python3
"""
Парсинг текстов приказов из data/order_texts/*.txt в единую таблицу квот по пользователям.

Вход:
  - ody_orders_2023_2026.json: метаданные по приказам (nd, number, date, organ, ody_years).
  - data/order_texts/{nd}.txt: HTML печатной версии документа из БПА
    (результат работы scripts/fetch_order_texts.py).

Выход:
  - data/parsed_quota_rows.csv с полями:
      nd, number, date, organ, year, basin, species, area,
      quota_type, legal_name, inn, share_pct, volume_tons

Поддерживаемые таблицы:
  - Формат как в приказе Росрыболовства от 26.12.2025 № 809
    (Таблица 20.2.1, 20.2.2, 22.4.1, 22.4.2, 26.1 и т.п.):
      * строка: "Вид водного биологического ресурса / Наименование рыбохозяйственного бассейна /
                 Район добычи (вылова) водного биологического ресурса"
      * следующая строка с конкретными значениями (вид, бассейн, район)
      * далее многострочная шапка с колонками "Наименование заявителя", "ИНН", "Размер доли в %"
        и "Размер части общего допустимого улова, тонн"
      * затем строки с данными по пользователям.

Ограничения:
  - Парсер эвристический, но заточен под шаблон Росрыболовства для распределения объёма части ОДУ.
  - При необходимости его можно дообучить/расширить на другие типы таблиц по тем же принципам.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, asdict
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime, timedelta


BASE_DIR = Path(__file__).resolve().parents[1]
JSON_PATH = BASE_DIR / "ody_orders_2023_2026.json"
TEXT_DIR = BASE_DIR / "data" / "order_texts"
OUT_CSV = BASE_DIR / "data" / "parsed_quota_rows.csv"


@dataclass
class QuotaRow:
    nd: str
    number: str
    date: str
    organ: str
    year: int
    basin: str
    species: str
    area: str
    quota_type: str  # Промышленная / Прибрежная / др. (пока Промышленная)
    legal_name: str
    inn: str
    share_pct: float
    volume_tons: float
    contract_no: str
    contract_date_start: str
    contract_date_end: str


class TableExtractor(HTMLParser):
    """
    Простой HTML-парсер, который вытаскивает все <table> как списки строк/ячеек.
    Каждая таблица — List[List[str]]: список строк, каждая строка — список ячеек.
    """

    def __init__(self) -> None:
        super().__init__()
        self.tables: List[List[List[str]]] = []

        self._in_table = False
        self._in_td = False

        self._current_table: List[List[str]] | None = None
        self._current_row: List[str] | None = None
        self._current_cell_chunks: List[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[override]
        tag = tag.lower()
        if tag == "table":
            self._in_table = True
            self._current_table = []
        elif tag == "tr" and self._in_table:
            self._current_row = []
        elif tag in ("td", "th") and self._in_table and self._current_row is not None:
            self._in_td = True
            self._current_cell_chunks = []

    def handle_endtag(self, tag: str) -> None:  # type: ignore[override]
        tag = tag.lower()
        if tag in ("td", "th") and self._in_table and self._in_td:
            text = "".join(self._current_cell_chunks or [])
            # Нормализуем пробелы
            text = " ".join(text.replace("\xa0", " ").split())
            self._current_row.append(text)
            self._current_cell_chunks = None
            self._in_td = False
        elif tag == "tr" and self._in_table:
            if self._current_row is not None:
                if any(cell.strip() for cell in self._current_row):
                    self._current_table.append(self._current_row)
            self._current_row = None
        elif tag == "table" and self._in_table:
            if self._current_table:
                self.tables.append(self._current_table)
            self._current_table = None
            self._in_table = False

    def handle_data(self, data: str) -> None:  # type: ignore[override]
        if self._in_table and self._in_td and self._current_cell_chunks is not None:
            self._current_cell_chunks.append(data)


def load_orders_meta() -> Dict[str, dict]:
    with JSON_PATH.open("r", encoding="utf-8") as f:
        docs = json.load(f)
    return {d["nd"]: d for d in docs}


def detect_year(meta: dict) -> Optional[int]:
    years = meta.get("ody_years") or []
    if years:
        return max(years)
    desc = meta.get("description", "")
    for y in (2026, 2025, 2024, 2023):
        if str(y) in desc:
            return y
    return None


CONTRACT_TERM_YEARS = 15


def compute_contract_end(date_str: str) -> str:
    """
    Эвристика: считаем, что стандартный срок договора о закреплении доли квоты — 15 лет.
    Дату окончания берём как (дата заключения + 15 лет - 1 день).
    """
    if not date_str:
        return ""
    try:
        start = datetime.strptime(date_str, "%d.%m.%Y").date()
    except ValueError:
        return ""
    try:
        end = start.replace(year=start.year + CONTRACT_TERM_YEARS) - timedelta(days=1)
    except ValueError:
        # запасной вариант для редких дат (29 февраля и т.п.)
        end = start + timedelta(days=CONTRACT_TERM_YEARS * 365)
    return end.strftime("%d.%m.%Y")


def detect_quota_type(meta: dict) -> str:
    """
    Определяем тип квоты по описанию приказа.

    Приблизительная логика:
    - если явно указаны инвестиционные цели / инвестиционные квоты -> "Инвестиционная"
    - если документ про международные договоры / районы действия международных договоров -> "Международная"
    - если речь только о прибрежном рыболовстве (без промышленного) -> "Прибрежная"
    - иначе считаем промышленной (исторической) квотой.
    """
    desc = (meta.get("description") or "").lower()

    if any(
        kw in desc
        for kw in (
            "инвестиционн",
            "инвестиц",
            "инвестквот",
            "на инвестиционные цели",
            "на инвестиционные квоты",
        )
    ):
        return "Инвестиционная"

    if "международных договоров" in desc or "районах действия международных договоров" in desc:
        return "Международная"

    if "прибрежного рыболовства" in desc and "промышленного рыболовства" not in desc:
        return "Прибрежная"

    return "Промышленная"


def _parse_float(value: str) -> Optional[float]:
    v = value.replace(" ", "").replace("\xa0", "").replace(",", ".")
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def parse_quota_table(table: List[List[str]], meta: dict) -> List[QuotaRow]:
    """
    Разбираем одну таблицу с распределением квот.
    """
    rows: List[QuotaRow] = []

    # Ищем заголовок с видом / бассейном / районом
    header_idx = None
    for i, r in enumerate(table):
        row_text = " ".join(r)
        if (
            "Вид водного биологического ресурса" in row_text
            and "рыбохозяйственного бассейна" in row_text
        ):
            header_idx = i
            break

    if header_idx is None or header_idx + 1 >= len(table):
        return rows

    values_row = table[header_idx + 1]
    if len(values_row) < 3:
        return rows

    species = values_row[0].strip()
    basin = values_row[1].strip()
    area = values_row[2].strip()

    # Строка с "Наименование заявителя" и "ИНН"
    cols_idx = None
    for j in range(header_idx + 1, len(table)):
        row_text = " ".join(table[j])
        if "Наименование заявителя" in row_text and "ИНН" in row_text:
            cols_idx = j
            break

    if cols_idx is None:
        return rows

    year = detect_year(meta)
    if year is None:
        return rows

    nd = meta.get("nd", "")
    number = meta.get("number", "")
    date = meta.get("date", "")
    organ = meta.get("organ", "")
    quota_type = detect_quota_type(meta)

    # Данные начинаются после шапки; ищем строки, где первый столбец — целое число (№ п/п)
    for r in table[cols_idx + 1 :]:
        if not r:
            continue

        first = r[0].strip()
        if not first.isdigit():
            # как только пошли нечисловые строки после данных — выходим
            continue

        legal_name = r[1].strip() if len(r) > 1 else ""
        inn = r[2].strip() if len(r) > 2 else ""

        contract_no = r[3].strip() if len(r) > 3 else ""
        contract_date_start = r[4].strip() if len(r) > 4 else ""
        contract_date_end = compute_contract_end(contract_date_start)

        # Объём (тонны) — обычно последний числовой столбец
        volume_tons: Optional[float] = None
        for cell in reversed(r):
            v = _parse_float(cell)
            if v is not None:
                volume_tons = v
                break

        # Доля, % — первый числовой столбец после реквизитов договора (после 5-ти первых)
        share_pct: Optional[float] = None
        for cell in r[5:]:
            v = _parse_float(cell)
            if v is not None and 0 < v <= 100:
                share_pct = v
                break

        if volume_tons is None:
            continue

        if share_pct is None:
            # если доля не нашлась, оставляем 0, чтобы не ломать последующий ETL
            share_pct = 0.0

        row = QuotaRow(
            nd=nd,
            number=number,
            date=date,
            organ=organ,
            year=year,
            basin=basin,
            species=species,
            area=area,
            quota_type=quota_type,
            legal_name=legal_name,
            inn=inn,
            share_pct=share_pct,
            volume_tons=volume_tons,
            contract_no=contract_no,
            contract_date_start=contract_date_start,
            contract_date_end=contract_date_end,
        )
        rows.append(row)

    return rows


def parse_text_for_quota_rows(html_text: str, meta: dict) -> List[QuotaRow]:
    parser = TableExtractor()
    parser.feed(html_text)

    all_rows: List[QuotaRow] = []
    for table in parser.tables:
        # Фильтруем только таблицы с "Вид водного биологического ресурса"
        has_header = any(
            "Вид водного биологического ресурса" in " ".join(r)
            for r in table
        )
        if not has_header:
            continue
        rows = parse_quota_table(table, meta)
        all_rows.extend(rows)

    return all_rows


def main() -> None:
    meta_by_nd = load_orders_meta()
    all_rows: List[QuotaRow] = []

    if not TEXT_DIR.exists():
        print("Каталог data/order_texts не найден. Сначала запустите fetch_order_texts.py")
        return

    txt_files = sorted(TEXT_DIR.glob("*.txt"))
    print(f"Всего файлов с текстами приказов: {len(txt_files)}")

    for path in txt_files:
        nd = path.stem
        meta = meta_by_nd.get(nd)
        if not meta:
            continue

        html_text = path.read_text(encoding="utf-8", errors="replace")
        rows = parse_text_for_quota_rows(html_text, meta)
        if not rows:
            continue
        print(f"{path.name}: найдено строк квот = {len(rows)}")
        all_rows.extend(rows)

    if not all_rows:
        print("Не удалось распарсить ни одной строки квот.")
        return

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "nd",
        "number",
        "date",
        "organ",
        "year",
        "basin",
        "species",
        "area",
        "quota_type",
        "legal_name",
        "inn",
        "share_pct",
        "volume_tons",
        "contract_no",
        "contract_date_start",
        "contract_date_end",
    ]
    with OUT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in all_rows:
            writer.writerow(asdict(r))

    print(f"Готово. Сводка строк квот записана в {OUT_CSV}")


if __name__ == "__main__":
    main()

