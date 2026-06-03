#!/usr/bin/env python3
"""
Обход папки 2026 на calculations.fish.gov.ru и все вложенные каталоги,
загрузка XLSX с перечнями/расчётами квот, парсинг в строки для обогащения quota_summary.

Вход: только URL (год 2026 фиксирован).
Выход: data/calculations_2026_quota_rows.csv — те же колонки, что в parsed_quota_rows.csv;
       data/calculations_2026_processed.json — список уже обработанных URL (инкремент).

Использование:
  python3 scripts/fetch_calculations_2026_quotas.py [--dry-run] [--limit N] [--full]
  --dry-run: только обход каталогов, список xlsx без загрузки.
  --limit N: загрузить не более N новых файлов (0 = все новые).
  --full: игнорировать кэш и заново скачать/обработать все файлы.

По умолчанию обрабатываются только ещё не обработанные URL; после каждого файла
данные дописываются в CSV и кэш обновляется (инкрементальное сохранение).

После загрузки перезапустите сборку квот: python3 src/etl_quota.py
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import time
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote
from typing import List, Tuple, Optional, Set

import requests
import pandas as pd

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
OUT_CSV = DATA_DIR / "calculations_2026_quota_rows.csv"
PROCESSED_JSON = DATA_DIR / "calculations_2026_processed.json"

FIELDNAMES = [
    "nd", "number", "date", "organ", "year", "basin", "species", "area",
    "quota_type", "legal_name", "inn", "share_pct", "volume_tons",
    "contract_no", "contract_date_start", "contract_date_end",
]

BASE_URL = "https://calculations.fish.gov.ru/"
START_PATH = "2026/"

# Задержка между запросами к серверу (секунды)
REQUEST_DELAY = 0.8


def list_directory(url: str, session: requests.Session) -> List[Tuple[str, bool]]:
    """
    Возвращает список (href, is_dir) для заданного URL каталога.
    href — относительный путь (имя папки или файла).
    Парсим Apache-style Index: <a href="...">...</a>
    """
    try:
        r = session.get(url, timeout=30)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"  Ошибка запроса {url}: {e}")
        return []

    links: List[Tuple[str, bool]] = []
    for m in re.finditer(r'<a\s+href=["\']([^"\']+)["\']', r.text, re.IGNORECASE):
        href = m.group(1).strip()
        if not href or href == "../" or href.startswith("?"):
            continue
        name = href.rstrip("/")
        if not name:
            continue
        is_dir = href.endswith("/")
        links.append((name, is_dir))
    return links


def crawl_xlsx_urls(start_url: str, session: requests.Session) -> List[Tuple[str, str, str]]:
    """
    Рекурсивный обход от start_url. Возвращает список (url_xlsx, basin, species).
    basin и species из пути/имени файла (species из имени файла, например 0_2_Минтай.xlsx -> Минтай).
    """
    out: List[Tuple[str, str, str]] = []
    # Очередь: (url, path_for_basin)
    # path_for_basin — накопленный путь для определения бассейна (первая папка после 2026)
    queue: List[Tuple[str, str]] = [(start_url, "")]

    while queue:
        url, basin_from_path = queue.pop(0)
        for name, is_dir in list_directory(url, session):
            name_decoded = unquote(name)
            if is_dir:
                next_url = urljoin(url, name + "/")
                # бассейн — имя первой папки после 2026 (текущая папка, если мы ещё не задали бассейн)
                next_basin = name_decoded if not basin_from_path else basin_from_path
                queue.append((next_url, next_basin))
            else:
                if name.endswith(".xlsx") or name.endswith(".XLSX"):
                    full_url = urljoin(url, name)
                    species = name_decoded
                    for suf in [".xlsx", ".XLSX"]:
                        species = species.replace(suf, "")
                    m = re.match(r"^[\d_]+\s*(.+)$", species)
                    if m:
                        species = m.group(1).strip()
                    else:
                        species = species.strip()
                    out.append((full_url, basin_from_path, species))
        time.sleep(REQUEST_DELAY)

    return out


def load_processed_urls() -> Set[str]:
    """Загружает множество уже обработанных URL из кэша."""
    if not PROCESSED_JSON.exists():
        return set()
    try:
        data = json.loads(PROCESSED_JSON.read_text(encoding="utf-8"))
        return set(data if isinstance(data, list) else data.get("urls", []))
    except Exception:
        return set()


def save_processed_urls(urls: Set[str]) -> None:
    """Сохраняет список обработанных URL в кэш."""
    PROCESSED_JSON.write_text(
        json.dumps(sorted(urls), ensure_ascii=False, indent=0),
        encoding="utf-8",
    )


def load_existing_rows() -> List[dict]:
    """Загружает уже сохранённые строки из CSV (для инкрементального дополнения)."""
    if not OUT_CSV.exists():
        return []
    try:
        with OUT_CSV.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames != FIELDNAMES:
                return []
            return list(reader)
    except Exception:
        return []


def save_all_rows(rows: List[dict]) -> None:
    """Записывает полный список строк в CSV."""
    with OUT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def normalize_basin_from_path(path_basin: str) -> str:
    """Приводим название папки к виду для quota_summary (бассейн)."""
    if not path_basin:
        return ""
    s = path_basin.strip()
    # Оставляем как есть или маппим на короткое имя
    if "Дальневосточ" in s:
        return "Дальневосточный бассейн" if "бассейн" in s or "рыбохозяйствен" in s else s
    if "Северн" in s:
        return "Северный бассейн" if "бассейн" in s or "рыбохозяйствен" in s else s
    if "Западн" in s:
        return "Западный рыбохозяйственный бассейн" if "бассейн" in s or "рыбохозяйствен" in s else s
    if "Волжск" in s or "Каспий" in s:
        return "Волжско-Каспийский рыбохозяйственный бассейн" if "бассейн" in s or "рыбохозяйствен" in s else s
    if "Международ" in s:
        return "Международное рыболовство"
    return s


def parse_xlsx_quota_table(
    content: bytes, url: str, basin: str, species: str
) -> List[dict]:
    """
    Парсит один XLSX: ищет таблицу с колонками ИНН, наименование, доля, тонн.
    Возвращает список словарей с полями как в parsed_quota_rows (nd, number, date, organ пустые).
    """
    rows_out: List[dict] = []
    try:
        xl = pd.ExcelFile(BytesIO(content), engine="openpyxl")
    except Exception as e:
        print(f"    Excel open error {url}: {e}")
        return rows_out

    for sheet_name in xl.sheet_names:
        try:
            df = pd.read_excel(xl, sheet_name=sheet_name, header=None)
        except Exception as e:
            print(f"    Sheet {sheet_name} error: {e}")
            continue
        if df.empty or len(df) < 2:
            continue

        # Ищем строку заголовка: в какой строке есть "инн" и ("наименование" или "заявитель" или "организация")
        header_row = None
        for idx in range(min(15, len(df))):
            row = df.iloc[idx]
            row_str = " ".join(str(v).lower() for v in row.astype(str))
            if "инн" in row_str and (
                "наименование" in row_str
                or "заявитель" in row_str
                or "организац" in row_str
                or "пользователь" in row_str
                or "юрлиц" in row_str
            ):
                header_row = idx
                break
        if header_row is None:
            continue

        df_header = df.iloc[header_row].astype(str).str.strip().str.lower()
        df_data = df.iloc[header_row + 1 :].reset_index(drop=True)

        # Индексы колонок
        idx_inn = None
        idx_name = None
        idx_share = None
        idx_volume = None
        for i, h in enumerate(df_header):
            if h is None or pd.isna(h):
                continue
            h = str(h).strip().lower()
            if "инн" in h and idx_inn is None:
                idx_inn = i
            # не брать колонку бассейна/субъекта как наименование организации
            if any(x in h for x in ["бассейн", "субъект", "регион", "территор"]):
                continue
            if any(x in h for x in ["наименование", "заявитель", "организац", "пользователь", "юрлиц", "название"]):
                idx_name = i
            if "дол" in h or "%" in h or "процент" in h:
                idx_share = i
            if "тонн" in h or "объем" in h or "размер части" in h or "оду" in h:
                idx_volume = i

        if idx_inn is None:
            continue
        # если название организации не нашли по заголовку — берём первую текстовую колонку (не ИНН, не число)
        if idx_name is None:
            for i in range(min(8, len(df_header))):
                if i == idx_inn or i == idx_share or i == idx_volume:
                    continue
                idx_name = i
                break
        if idx_name is None:
            continue

        for _, row in df_data.iterrows():
            inn_val = row.iloc[idx_inn] if idx_inn is not None else None
            name_val = row.iloc[idx_name] if idx_name is not None else None
            if pd.isna(inn_val) or pd.isna(name_val):
                continue
            inn_str = str(inn_val).strip().replace(" ", "").replace(".0", "")
            name_str = str(name_val).strip()
            # Пропускаем итоги и пустые
            if not inn_str or not name_str:
                continue
            if inn_str.lower() in ("итого", "всего", "inn", "инн"):
                continue
            # ИНН должен быть цифрами (10 или 12)
            if not re.match(r"^\d{10}$|^\d{12}$", inn_str):
                continue

            share_val = None
            if idx_share is not None:
                try:
                    share_val = float(str(row.iloc[idx_share]).replace(",", ".").replace(" ", ""))
                except (ValueError, TypeError):
                    pass
            volume_val = None
            if idx_volume is not None:
                try:
                    volume_val = float(str(row.iloc[idx_volume]).replace(",", ".").replace(" ", ""))
                except (ValueError, TypeError):
                    pass
            if volume_val is None and share_val is None:
                continue
            if volume_val is not None and (volume_val <= 0 or volume_val > 1e7):
                continue

            rows_out.append({
                "nd": "",
                "number": "",
                "date": "",
                "organ": "calculations.fish.gov.ru",
                "year": 2026,
                "basin": normalize_basin_from_path(basin),
                "species": species,
                "area": "",
                "quota_type": "Промышленная",
                "legal_name": name_str,
                "inn": inn_str,
                "share_pct": share_val if share_val is not None else "",
                "volume_tons": volume_val if volume_val is not None else "",
                "contract_no": "",
                "contract_date_start": "",
                "contract_date_end": "",
            })
    return rows_out


def main() -> None:
    parser = argparse.ArgumentParser(description="Загрузка квот 2026 с calculations.fish.gov.ru")
    parser.add_argument("--dry-run", action="store_true", help="Только список xlsx, без загрузки")
    parser.add_argument("--limit", type=int, default=0, help="Макс. число новых файлов (0 = все новые)")
    parser.add_argument("--full", action="store_true", help="Полная перезагрузка: игнорировать кэш, обработать все файлы заново")
    args = parser.parse_args()

    session = requests.Session()
    session.headers.update({"User-Agent": "QuotasAnalytic/1.0 (research)"})

    start_url = urljoin(BASE_URL, START_PATH)
    print(f"Обход каталога {start_url} ...")
    xlsx_list = crawl_xlsx_urls(start_url, session)
    print(f"Найдено файлов .xlsx: {len(xlsx_list)}")

    if args.dry_run:
        for url, basin, species in xlsx_list[:30]:
            print(f"  {basin} | {species} | {url}")
        if len(xlsx_list) > 30:
            print(f"  ... и ещё {len(xlsx_list) - 30}")
        return

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Инкрементальный режим: уже обработанные URL и накопленные строки
    if args.full:
        processed: Set[str] = set()
        existing_rows: List[dict] = []
        print("Режим --full: кэш игнорируется, обрабатываются все файлы.")
    else:
        processed = load_processed_urls()
        existing_rows = load_existing_rows()
        print(f"Уже обработано URL: {len(processed)}, строк в кэше CSV: {len(existing_rows)}")

    to_fetch = [(u, b, s) for u, b, s in xlsx_list if u not in processed]
    if not to_fetch:
        print("Нет новых файлов для загрузки. Используйте --full для полной перезагрузки.")
        return

    limit = args.limit or len(to_fetch)
    to_fetch = to_fetch[:limit]
    all_rows: List[dict] = list(existing_rows)
    newly_processed = 0

    for i, (url, basin, species) in enumerate(to_fetch):
        try:
            r = session.get(url, timeout=60)
            r.raise_for_status()
        except requests.RequestException as e:
            print(f"  Ошибка загрузки {url}: {e}")
            time.sleep(REQUEST_DELAY)
            continue
        rows = parse_xlsx_quota_table(r.content, url, basin, species)
        if rows:
            all_rows.extend(rows)
            newly_processed += 1
            print(f"  [{i+1}/{len(to_fetch)}] {species[:40]:40} +{len(rows)} строк")
        processed.add(url)
        save_processed_urls(processed)
        save_all_rows(all_rows)
        time.sleep(REQUEST_DELAY)

    print(f"Готово. Обработано новых файлов: {newly_processed}, всего строк в {OUT_CSV}: {len(all_rows)}")


if __name__ == "__main__":
    main()
