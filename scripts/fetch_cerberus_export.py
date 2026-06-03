#!/usr/bin/env python3
"""
Работа с выгрузкой реестра Цербер (cerberus.vetrf.ru).

Как получить XLS вручную:
1. Открыть https://cerberus.vetrf.ru/cerberus/certified/pub
2. Тип продукции: «Рыба и морепродукты»
3. Страна: выбрать Корея, Китай, США и т.д. (или не выбирать — все страны)
4. Поиск
5. «Сформировать новый отчет в формате xls» → указать поля, количество записей → Сформировать
6. Скачать файл и указать путь ниже.

Запуск:
  python3 scripts/fetch_cerberus_export.py path/to/cerberus_report.xls
  python3 scripts/fetch_cerberus_export.py path/to/cerberus_report.xlsx

Создаёт/обновляет:
  data/cerberus_export.csv   — все записи из выгрузки (ИНН, объект, вид объекта, страна, регион, продукция, статус)
  output/companies_with_export.csv — компании из company_groups_enriched с данными Цербера (по ИНН)
"""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

# Корень проекта
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = BASE_DIR / "output"

CERBERUS_CSV = DATA_DIR / "cerberus_export.csv"
COMPANIES_CSV = DATA_DIR / "company_groups_enriched.csv"
OUTPUT_WITH_EXPORT = OUTPUT_DIR / "companies_with_export.csv"


def normalize_inn(value: str | float) -> str:
    """Оставляет только цифры ИНН (10 или 12). Учитывает число из Excel (2537089631.0 или "2537089631.0")."""
    if value is None or value == "":
        return ""
    if isinstance(value, float) and value != value:  # NaN
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value == int(value):
            value = str(int(value))
        else:
            value = str(value)
    s = str(value).strip()
    # Строка из Excel: "2537089631.0" -> убрать хвост .0
    if re.match(r"^\d+\.0+$", s):
        s = s.split(".")[0]
    if not s:
        return ""
    digits = re.sub(r"\D", "", s)
    return digits if len(digits) in (10, 12) else ""


def read_cerberus_xls(xls_path: Path) -> list[dict]:
    """Читает выгрузку Цербера (xls или xlsx), возвращает список словарей."""
    try:
        import pandas as pd
    except ImportError:
        print("Установите pandas и openpyxl: pip install pandas openpyxl xlrd")
        return []

    path = Path(xls_path)
    if not path.exists():
        print(f"Файл не найден: {path}")
        return []

    engine = "openpyxl" if path.suffix.lower() == ".xlsx" else None
    try:
        xl = pd.ExcelFile(path, engine=engine)
        sheet_names = xl.sheet_names
        # Если несколько листов — берём лист со списком предприятий (не "Параметры запроса")
        sheet = None
        if len(sheet_names) > 1:
            for name in sheet_names:
                if "список" in name.lower() or "предприят" in name.lower():
                    sheet = name
                    break
            if sheet is None:
                # первый лист часто "Параметры запроса" с 5 строками — берём второй
                df_first = pd.read_excel(xl, sheet_name=sheet_names[0])
                if len(df_first) < 100 and len(sheet_names) > 1:
                    sheet = sheet_names[1]
        if sheet is not None:
            df = pd.read_excel(xl, sheet_name=sheet)
        else:
            df = pd.read_excel(path, engine=engine)
    except Exception as e:
        try:
            df = pd.read_excel(path, engine="xlrd")
        except Exception as e2:
            print(f"Ошибка чтения Excel: {e}, {e2}")
            return []

    # Приводим к списку словарей, ключи — как в файле
    rows = df.to_dict("records")
    # Нормализуем ключи (убираем лишние пробелы)
    out = []
    for r in rows:
        row = {}
        for k, v in r.items():
            if pd.isna(v):
                v = ""
            key = (k.strip() if isinstance(k, str) else str(k))
            row[key] = "" if v is None else str(v).strip()
        out.append(row)
    return out


def map_cerberus_columns(row: dict) -> dict:
    """Маппинг полей выгрузки Цербера в стандартные имена для cerberus_export.csv."""
    # Возможные варианты названий в XLS (зависит от выбранных полей при выгрузке)
    aliases_inn = ["ИНН", "Хоз. субъект, осуществляющий деятельность (инн)", "инн", "ИНН хоз. субъекта"]
    aliases_name = ["Название объекта", "Название", "Поднадзорный объект"]
    aliases_kind = ["Вид объекта", "Вид объекта объекта"]
    aliases_country = ["Страна", "Страна назначения"]
    aliases_region = ["Регион", "Регион объекта"]
    aliases_product = ["Продукция", "Тип продукции"]
    aliases_activity = ["Виды деятельности", "Виды деятельности объекта"]
    aliases_status = ["Статус", "Статус поднадзорного объекта"]
    aliases_subject = ["Хоз. субъект, осуществляющий деятельность", "Хоз. субъект"]

    def first_value(row: dict, keys: list[str]) -> str:
        for k in keys:
            for rk, rv in row.items():
                if rk and k.lower() in str(rk).lower():
                    return (rv or "").strip()
        return ""

    inn = ""
    for rk, rv in row.items():
        if not rk:
            continue
        rk_lower = str(rk).lower()
        if "инн" in rk_lower and "огрн" not in rk_lower:
            inn = normalize_inn(str(rv or ""))
            break
    if not inn:
        inn = normalize_inn(first_value(row, aliases_inn))

    return {
        "ИНН": inn,
        "Название_объекта": first_value(row, aliases_name) or row.get("Название объекта", ""),
        "Вид_объекта": first_value(row, aliases_kind) or row.get("Вид объекта", ""),
        "Страна": first_value(row, aliases_country) or row.get("Страна", ""),
        "Регион": first_value(row, aliases_region) or row.get("Регион", ""),
        "Продукция": first_value(row, aliases_product) or row.get("Продукция", ""),
        "Виды_деятельности": first_value(row, aliases_activity) or row.get("Виды деятельности", ""),
        "Статус": first_value(row, aliases_status) or row.get("Статус", ""),
        "Хоз_субъект": first_value(row, aliases_subject) or row.get("Хоз. субъект, осуществляющий деятельность", ""),
    }


def is_vessel_record(kind: str) -> bool:
    """Вид объекта = предприятия (суда) по добыче, переработке и транспортировке гидробионтов."""
    if not kind:
        return False
    k = kind.lower()
    return "суда" in k and "гидробионт" in k


def save_cerberus_csv(rows: list[dict], path: Path) -> None:
    """Сохраняет нормализованные записи в data/cerberus_export.csv."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "ИНН", "Название_объекта", "Вид_объекта", "Страна", "Регион",
        "Продукция", "Виды_деятельности", "Статус", "Хоз_субъект", "Судно"
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            r["Судно"] = "1" if is_vessel_record(r.get("Вид_объекта", "")) else "0"
            w.writerow(r)


def load_company_inns(path: Path) -> set[str]:
    """Загружает множество ИНН из company_groups_enriched.csv."""
    inns = set()
    if not path.exists():
        return inns
    with path.open("r", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            inn = normalize_inn(row.get("ИНН", ""))
            if inn:
                inns.add(inn)
    return inns


def build_companies_with_export(
    cerberus_path: Path,
    companies_path: Path,
    output_path: Path,
) -> None:
    """Строит output/companies_with_export.csv: наши компании + данные Цербера по ИНН."""
    # Загружаем записи Цербера
    if not cerberus_path.exists():
        print(f"Файл не найден: {cerberus_path}. Сначала выполните выгрузку и импорт XLS.")
        return
    cerberus_by_inn: dict[str, list[dict]] = {}
    with cerberus_path.open("r", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            inn = normalize_inn(row.get("ИНН", ""))
            if inn:
                cerberus_by_inn.setdefault(inn, []).append(row)

    # Загружаем компании
    companies_rows = []
    with companies_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or []) + [
            "Цербер_страны", "Цербер_объекты", "Цербер_судов",
            "РМРС_судов", "FleetPhoto_судов",
        ]
        for row in reader:
            inn = normalize_inn(row.get("ИНН", ""))
            recs = cerberus_by_inn.get(inn, [])
            countries = "; ".join(sorted({r.get("Страна", "") for r in recs if r.get("Страна")}))
            objects = "; ".join({r.get("Название_объекта", "") for r in recs if r.get("Название_объекта")})
            vessels = sum(1 for r in recs if r.get("Судно") == "1")
            row["Цербер_страны"] = countries
            row["Цербер_объекты"] = objects
            row["Цербер_судов"] = str(vessels)
            row["РМРС_судов"] = row.get("РМРС_судов", "") or ""
            row["FleetPhoto_судов"] = row.get("FleetPhoto_судов", "") or ""
            companies_rows.append(row)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for row in companies_rows:
            w.writerow(row)
    matched = sum(1 for row in companies_rows if row.get("Цербер_страны"))
    print(f"Совпадений по ИНН с Цербером: {matched} из {len(companies_rows)}")
    print(f"Записано: {output_path}")


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        print("\nИспользование: python3 fetch_cerberus_export.py <path_to_cerberus.xls|xlsx>")
        print("Или для сборки companies_with_export без нового XLS:")
        print("  python3 fetch_cerberus_export.py --merge-only")
        return

    if sys.argv[1] == "--merge-only":
        build_companies_with_export(CERBERUS_CSV, COMPANIES_CSV, OUTPUT_WITH_EXPORT)
        return

    xls_path = Path(sys.argv[1])
    rows = read_cerberus_xls(xls_path)
    if not rows:
        return
    normalized = [map_cerberus_columns(r) for r in rows]
    # В выгрузке Цербера ИНН заполнен только в первой строке предприятия; подстроки — пустые
    last_inn = ""
    for r in normalized:
        if r.get("ИНН"):
            last_inn = r["ИНН"]
        elif last_inn:
            r["ИНН"] = last_inn
    save_cerberus_csv(normalized, CERBERUS_CSV)
    print(f"Записано записей в {CERBERUS_CSV}: {len(normalized)}")
    vessels = sum(1 for r in normalized if is_vessel_record(r.get("Вид_объекта", "")))
    print(f"Из них суда (вид объекта с 'суда' и 'гидробионт'): {vessels}")

    if COMPANIES_CSV.exists():
        build_companies_with_export(CERBERUS_CSV, COMPANIES_CSV, OUTPUT_WITH_EXPORT)


if __name__ == "__main__":
    main()
