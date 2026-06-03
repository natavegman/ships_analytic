#!/usr/bin/env python3
"""
Подготовка CSV-файлов для импорта в Notion.

Результат: notion_import/
  - companies.csv   → база "Компании"
  - vessels.csv     → база "Суда"
  - quotas.csv      → база "Квоты"

Запуск:
    python3 scripts/prepare_notion_import.py
"""

import csv
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUTPUT = ROOT / "output"
IMPORT_DIR = ROOT / "notion_import"
IMPORT_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def read_csv(path, encoding="utf-8"):
    """Read CSV with fallback encoding."""
    for enc in (encoding, "cp1251", "utf-8-sig", "latin-1"):
        try:
            with open(path, encoding=enc, newline="") as f:
                return list(csv.DictReader(f))
        except (UnicodeDecodeError, UnicodeError):
            continue
    raise RuntimeError(f"Cannot decode {path}")


VESSEL_PREFIX_RE = re.compile(
    r'^(?:СКТР|СРТМ|СТР|БАТМ|БМРТ|РС|РТ|РТМ|РТМКС|РТМС|СТМ|ТР|ПБ|СДС|МК|АК|ПК|М)\s*[-‐]?\s*',
    re.IGNORECASE,
)
BOARD_NUM_RE = re.compile(r'[А-ЯA-Z]{1,3}[\s-]*\d{3,5}')
VESSEL_NAME_QUOTED_RE = re.compile(r'"([^"]+)"')
VESSEL_TYPE_RE = re.compile(
    r'^(СКТР|СРТМ|СТР|БАТМ|БМРТ|РС|РТ|РТМ|РТМКС|РТМС|СТМ|ТР|ПБ|СДС)',
    re.IGNORECASE,
)


COMPANY_KEYWORDS = (
    "ООО", "ОАО", "ЗАО", "ПАО", "РПК", "АКРОС", "НПО",
    "КОЛХОЗ", "РЫБОЛОВЕЦК", "ОБЩЕСТВО", "КОМПАНИЯ", "ПРЕДПРИЯТИЕ",
    "АРТЕЛЬ", "КООПЕРАТИВ", "ФАБРИК", "КОМБИНАТ", "ПРОМЫСЕЛ",
    "ГРУППА", "ОБЪЕДИНЕНИЕ", "АССОЦИАЦИЯ", "ФЛОТ",
)

COMPANY_OPF_RE = re.compile(
    r'(?:ООО|АО|ОАО|ЗАО|ПАО|СПК)\s',
    re.IGNORECASE,
)

VESSEL_TYPE_ANYWHERE_RE = re.compile(
    r'(?:СКТР|СРТМ-К|СРТМ|СТР|БАТМ|БМРТ|РТМКС|РТМС|РТМ|МРТР|РТ|СТМ|ТР|ПБ|СДС|РС|РШ|ЯМС|МФТ)\s',
    re.IGNORECASE,
)

QUOTED_WITH_POS_RE = re.compile(r'"([^"]+)"')

PAREN_COMPANY_RE = re.compile(
    r'\(\s*(?:ООО|АО|ОАО|ЗАО|ПАО|СПК)\s',
    re.IGNORECASE,
)


def _is_company_context(raw: str, match_start: int) -> bool:
    """Check if a quoted string at position match_start is in a company context."""
    before = raw[:match_start].rstrip()
    if re.search(r'(?:ООО|АО|ОАО|ЗАО|ПАО|СПК|Компания)\s*$', before, re.IGNORECASE):
        return True
    if re.search(r'\(\s*(?:ООО|АО|ОАО|ЗАО|ПАО|СПК|Рыболовецк)\b', before, re.IGNORECASE):
        return True
    if re.search(r'\(\s*$', before) and not re.search(r'Судно|судно', before):
        return True
    if re.search(r',\s+(?:ООО|АО|ОАО|ЗАО|ПАО|СПК|Новая)\b', before, re.IGNORECASE):
        return True
    return False


def _is_company_name(name: str) -> bool:
    upper = name.upper().strip()
    if upper.startswith(("ООО", "АО ", "ОАО", "ЗАО", "ПАО", "СПК")):
        return True
    return any(kw in upper for kw in COMPANY_KEYWORDS)


def _is_only_company(raw: str) -> bool:
    """Check if entire raw string is just a company name with no vessel info."""
    s = raw.strip()
    has_vessel_type = bool(VESSEL_TYPE_ANYWHERE_RE.search(s))
    has_board = bool(BOARD_NUM_RE.search(s))
    has_vessel_word = bool(re.search(r'Судно|судно|с/с\b', s, re.IGNORECASE))
    if has_vessel_type or has_board or has_vessel_word:
        return False
    upper = s.upper()
    if COMPANY_OPF_RE.match(s):
        return True
    if any(kw in upper for kw in ("ОБЩЕСТВО С ОГРАНИЧЕННОЙ", "АКЦИОНЕРНОЕ ОБЩЕСТВО")):
        return True
    if _is_company_name(s) and not QUOTED_WITH_POS_RE.search(s):
        return True
    quoted = QUOTED_WITH_POS_RE.findall(s)
    if quoted and all(_is_company_name(q) for q in quoted):
        unquoted_prefix = s.split('"')[0].strip()
        if unquoted_prefix and len(unquoted_prefix) > 2:
            return False
        return True
    return False


def parse_vessel_name(raw_name: str):
    """Extract clean vessel name, board number, and vessel type from Cerberus name.

    Uses positional context to distinguish vessel names from company names:
    - Quoted text after company OPF (ООО/АО/...) or inside parentheses → company
    - First non-company quoted text → vessel name
    """
    if not raw_name:
        return raw_name, "", ""

    if _is_only_company(raw_name):
        return "", "", ""

    vessel_type = ""
    m_type = VESSEL_TYPE_RE.match(raw_name.strip())
    if m_type:
        vessel_type = m_type.group(1).upper()
    if not vessel_type:
        m_any = VESSEL_TYPE_ANYWHERE_RE.search(raw_name)
        if m_any:
            vessel_type = m_any.group(0).strip().upper()

    board_match = BOARD_NUM_RE.search(raw_name)
    board_num = board_match.group(0).strip() if board_match else ""

    vessel_candidates = []
    for m in QUOTED_WITH_POS_RE.finditer(raw_name):
        txt = m.group(1).strip()
        if _is_company_context(raw_name, m.start()):
            continue
        if _is_company_name(txt):
            company_split = re.split(r',\s*(?:ООО|АО|ОАО|ЗАО)', txt, flags=re.IGNORECASE)
            if len(company_split) > 1 and company_split[0].strip():
                vessel_candidates.append(company_split[0].strip())
            continue
        vessel_candidates.append(txt)

    if vessel_candidates:
        clean_name = vessel_candidates[0]
    else:
        unquoted = raw_name.split('"')[0].strip().rstrip(',')
        unquoted = VESSEL_PREFIX_RE.sub("", unquoted).strip()
        type_m = VESSEL_TYPE_ANYWHERE_RE.match(unquoted)
        if type_m:
            unquoted = unquoted[type_m.end():].strip()

        for sep in [" ООО ", " АО ", " ОАО ", " ЗАО "]:
            if sep in unquoted.upper():
                idx = unquoted.upper().index(sep)
                unquoted = unquoted[:idx].strip()
                break

        unquoted = unquoted.strip(' "\',')
        unquoted = re.sub(r'\(.*$', '', unquoted).strip()

        if unquoted and len(unquoted) > 1 and not _is_company_name(unquoted):
            clean_name = unquoted
        else:
            clean_name = ""

    clean_name = re.sub(r'\s*,\s*$', '', clean_name)
    clean_name = re.sub(r'\s+', ' ', clean_name).strip()

    return clean_name, board_num, vessel_type


BASIN_SHORT = {
    "Северный": "Северный",
    "Дальневосточный": "Дальневосточный",
}


def shorten_basin(basin: str) -> str:
    for key, short in BASIN_SHORT.items():
        if key in basin:
            return short
    if "Норвег" in basin:
        return "Норвежский"
    return basin[:60]


def clean_company_name(name: str) -> str:
    """Shorten verbose OPF prefixes."""
    replacements = [
        ('ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ', 'ООО'),
        ('АКЦИОНЕРНОЕ ОБЩЕСТВО', 'АО'),
        ('ОТКРЫТОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО', 'ОАО'),
        ('ЗАКРЫТОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО', 'ЗАО'),
        ('ПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО', 'ПАО'),
    ]
    result = name.strip()
    upper = result.upper()
    for full, short in replacements:
        if upper.startswith(full):
            result = short + result[len(full):]
            break
    result = result.replace('""', '"')
    return result.strip()


# ---------------------------------------------------------------------------
# 1. Companies
# ---------------------------------------------------------------------------

def build_companies():
    """Merge company_groups_enriched + companies_with_export → companies.csv"""
    print("Building companies.csv ...")

    enriched = read_csv(DATA / "company_groups_enriched.csv")
    export_rows = read_csv(OUTPUT / "companies_with_export.csv")

    by_inn = {}

    for r in enriched:
        inn = r.get("ИНН", "").strip()
        if not inn or len(inn) < 8 or not inn.isdigit():
            continue
        name = r.get("Юр_Лицо", "").strip()
        if not name or len(name) < 3:
            continue
        exclude = (r.get("Исключить", "") or "").strip()
        if exclude:
            continue

        comment = (r.get("Комментарий", "") or "").strip()
        status = "Ликвидирована" if "Ликвидирована" in comment else "Действует"

        by_inn[inn] = {
            "Название": clean_company_name(r.get("Юр_Лицо", "")),
            "ИНН": inn,
            "ОГРН": r.get("ОГРН", "").strip(),
            "Группа_компаний": r.get("Группа_Компаний", "").strip(),
            "Директор": r.get("Директор_ListOrg", "").strip(),
            "Контакты": "",
            "Регион": "",
            "Статус": status,
            "Цербер_страны_экспорта": "",
            "Цербер_судов": "0",
        }

    for r in export_rows:
        inn = r.get("ИНН", "").strip()
        if not inn or len(inn) < 8 or not inn.isdigit():
            continue
        name = r.get("Юр_Лицо", "").strip()
        if not name or len(name) < 3:
            continue
        if inn in by_inn:
            entry = by_inn[inn]
            if r.get("Группа_Компаний", "").strip():
                entry["Группа_компаний"] = r["Группа_Компаний"].strip()
            if r.get("Контакты_ListOrg", "").strip():
                entry["Контакты"] = r["Контакты_ListOrg"].strip()
            if r.get("Директор_ListOrg", "").strip() and not entry["Директор"]:
                entry["Директор"] = r["Директор_ListOrg"].strip()
            entry["Цербер_страны_экспорта"] = r.get("Цербер_страны", "").strip()
            entry["Цербер_судов"] = r.get("Цербер_судов", "0").strip()
            if r.get("ОГРН", "").strip() and not entry.get("ОГРН"):
                entry["ОГРН"] = r["ОГРН"].strip()
        else:
            by_inn[inn] = {
                "Название": clean_company_name(r.get("Юр_Лицо", "")),
                "ИНН": inn,
                "ОГРН": "",
                "Группа_компаний": r.get("Группа_Компаний", "").strip(),
                "Директор": r.get("Директор_ListOrg", "").strip(),
                "Контакты": r.get("Контакты_ListOrg", "").strip(),
                "Регион": "",
                "Статус": "",
                "Цербер_страны_экспорта": r.get("Цербер_страны", "").strip(),
                "Цербер_судов": r.get("Цербер_судов", "0").strip(),
            }

    companies = sorted(by_inn.values(), key=lambda c: c["Название"])

    out_path = IMPORT_DIR / "companies.csv"
    fields = [
        "Название", "ИНН", "ОГРН", "Группа_компаний", "Директор",
        "Контакты", "Регион", "Статус", "Цербер_страны_экспорта",
        "Цербер_судов",
    ]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(companies)

    print(f"  → {out_path} ({len(companies)} companies)")
    return by_inn


# ---------------------------------------------------------------------------
# 2. Vessels
# ---------------------------------------------------------------------------

def build_vessels(companies_by_inn: dict):
    """Cerberus vessels + GFW data → vessels.csv"""
    print("Building vessels.csv ...")

    cerb = read_csv(DATA / "cerberus_export.csv")
    cerb_vessels = [r for r in cerb if r.get("Судно") == "1" and r.get("Название_объекта")]

    with open(DATA / "gfw_our_vessels.json", encoding="utf-8") as f:
        gfw_list = json.load(f)

    gfw_by_inn_name = {}
    for v in gfw_list:
        key = (v.get("inn", ""), v.get("name", ""))
        gfw_by_inn_name[key] = v

    seen = set()
    vessels = []

    for r in cerb_vessels:
        raw_name = r["Название_объекта"]
        inn = r.get("ИНН", "").strip()
        key = (inn, raw_name)
        if key in seen:
            continue
        seen.add(key)

        clean_name, board_num, vessel_type = parse_vessel_name(raw_name)

        if not clean_name:
            continue

        company_info = companies_by_inn.get(inn, {})
        company_name = company_info.get("Название", "") or clean_company_name(r.get("Хоз_субъект", ""))

        gfw = gfw_by_inn_name.get((inn, raw_name), {})
        imo = gfw.get("imo", "") or ""
        gfw_id = gfw.get("gfw_id", "") or ""
        gfw_name = gfw.get("gfw_name", "") or ""

        region = r.get("Регион", "").strip()
        work_region = ""
        if any(x in region for x in ("Мурманск", "Архангельск", "Карелия")):
            work_region = "Северный"
        elif any(x in region for x in ("Приморск", "Камчат", "Сахалин", "Магадан", "Хабаров", "Курил")):
            work_region = "Дальневосточный"
        elif "Калининград" in region:
            work_region = "Норвежский"

        vessels.append({
            "Название_судна": clean_name,
            "Бортовой_номер": board_num,
            "IMO": str(imo) if imo else "",
            "Тип_Модель": vessel_type,
            "Год_постройки": "",
            "Состояние": "Эксплуатация",
            "Регион_работы": work_region,
            "Судовладелец_ИНН": inn,
            "Судовладелец": company_name,
            "GFW_ID": gfw_id,
            "GFW_Name": gfw_name,
            "Регион_регистрации": region,
        })

    vessels.sort(key=lambda v: v["Название_судна"])

    out_path = IMPORT_DIR / "vessels.csv"
    fields = [
        "Название_судна", "Бортовой_номер", "IMO", "Тип_Модель",
        "Год_постройки", "Состояние", "Регион_работы",
        "Судовладелец_ИНН", "Судовладелец", "GFW_ID", "GFW_Name",
        "Регион_регистрации",
    ]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(vessels)

    print(f"  → {out_path} ({len(vessels)} vessels)")
    return vessels


# ---------------------------------------------------------------------------
# 3. Quotas
# ---------------------------------------------------------------------------

def build_quotas(excluded_inns: set = None):
    """quota_summary.csv → quotas.csv (cleaned for Notion)"""
    print("Building quotas.csv ...")
    excluded_inns = excluded_inns or set()

    rows = read_csv(OUTPUT / "quota_summary.csv")
    quotas = []
    skipped = 0

    for r in rows:
        inn = r.get("ИНН", "").strip()
        if inn in excluded_inns:
            skipped += 1
            continue

        company = clean_company_name(r.get("Юр_Лицо", ""))
        year = r.get("Год", "").strip()
        species = r.get("Объект_Лова", "").strip()
        basin = shorten_basin(r.get("Бассейн", ""))

        title = f"{company[:30]} — {species} — {year}"

        quotas.append({
            "Запись": title,
            "Компания": company,
            "Компания_ИНН": inn,
            "Год": year,
            "Бассейн": basin,
            "Объект_лова": species,
            "Тип_квоты": r.get("Тип_Квоты", "").strip(),
            "Доля_процент": r.get("Доля_%", "").strip(),
            "Объем_тонн": r.get("Объем_Тонн", "").strip(),
            "Дата_начала_договора": r.get("Дата_Начала_Договора", "").strip(),
            "Дата_окончания_договора": r.get("Дата_Окончания_Договора", "").strip(),
            "Причина_изменения": r.get("Причина_Изменения", "").strip(),
            "Группа_компаний": r.get("Группа_Компаний", "").strip(),
        })

    out_path = IMPORT_DIR / "quotas.csv"
    fields = [
        "Запись", "Компания", "Компания_ИНН", "Год", "Бассейн",
        "Объект_лова", "Тип_квоты", "Доля_процент", "Объем_тонн",
        "Дата_начала_договора", "Дата_окончания_договора",
        "Причина_изменения", "Группа_компаний",
    ]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(quotas)

    if skipped:
        print(f"  (пропущено {skipped} квот исключённых компаний)")
    print(f"  → {out_path} ({len(quotas)} quotas)")
    return quotas


# ---------------------------------------------------------------------------
# 4. Equipment templates (empty CSVs with headers)
# ---------------------------------------------------------------------------

def build_equipment_templates():
    """Generate empty CSV templates for equipment databases (to fill manually)."""
    print("Building equipment templates ...")

    templates = {
        "winches_template.csv": [
            "Название_Маркировка", "Судно", "Тип_лебедки",
            "Производитель", "Модель", "Серийный_номер",
            "Состояние", "Дата_последнего_ТО", "Примечания",
        ],
        "satellite_systems_template.csv": [
            "Название", "Судно", "Тип_системы", "Вид_оборудования",
            "Производитель", "Модель", "Серийный_номер",
            "Состояние", "Провайдер", "Дата_установки",
            "Срок_контракта_до", "Примечания",
        ],
        "trawl_control_template.csv": [
            "Название_SN", "Судно", "Система", "Тип_компонента",
            "Серийный_номер", "Состояние", "Расположение",
            "Дата_последней_проверки", "Примечания",
        ],
        "additional_equipment_template.csv": [
            "Название", "Судно", "Категория",
            "Производитель", "Модель", "Серийный_номер",
            "Состояние", "Дата_последнего_ТО", "Примечания",
        ],
        "spare_parts_orders_template.csv": [
            "Заявка", "Судно", "Категория_оборудования",
            "Тип_заявки", "Статус", "Приоритет",
            "Описание", "Наименования_ЗИП", "Поставщик",
            "Стоимость", "Валюта", "Дата_заявки",
            "Дата_ожидаемая", "Дата_исполнения",
            "Ответственный", "Документы",
        ],
        "catch_production_template.csv": [
            "Запись", "Судно", "Тип_данных", "Вид_продукции",
            "Период", "Дата_начала", "Дата_окончания",
            "Объем_тонн", "Источник_данных", "Примечания",
        ],
    }

    for filename, fields in templates.items():
        out_path = IMPORT_DIR / filename
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(fields)
        print(f"  → {out_path}")


# ---------------------------------------------------------------------------
# 5. Summary report
# ---------------------------------------------------------------------------

def print_summary(companies_by_inn, vessels, quotas):
    print("\n" + "=" * 60)
    print("NOTION IMPORT — СВОДКА")
    print("=" * 60)
    print(f"  Компании:  {len(companies_by_inn)}")
    print(f"  Суда:      {len(vessels)}")
    print(f"  Квоты:     {len(quotas)}")

    groups = set(c.get("Группа_компаний", "") for c in companies_by_inn.values() if c.get("Группа_компаний"))
    print(f"  Групп компаний: {len(groups)}")

    with_imo = sum(1 for v in vessels if v.get("IMO"))
    with_gfw = sum(1 for v in vessels if v.get("GFW_ID"))
    print(f"  Судов с IMO: {with_imo}")
    print(f"  Судов в GFW: {with_gfw}")

    regions = {}
    for v in vessels:
        r = v.get("Регион_работы", "") or "Не определен"
        regions[r] = regions.get(r, 0) + 1
    print(f"  Суда по регионам:")
    for r, cnt in sorted(regions.items(), key=lambda x: -x[1]):
        print(f"    {r}: {cnt}")

    print(f"\nФайлы в {IMPORT_DIR}/:")
    for p in sorted(IMPORT_DIR.iterdir()):
        size = p.stat().st_size
        print(f"  {p.name:40s} {size:>10,} bytes")

    print("\n--- Порядок импорта в Notion ---")
    print("  1. companies.csv  → база «Компании»")
    print("  2. vessels.csv    → база «Суда» (после создания relation → Компании)")
    print("  3. quotas.csv     → база «Квоты» (после создания relation → Компании)")
    print("  4. Шаблоны оборудования — заполнить данными, затем импортировать")
    print("  5. Настроить Relations между базами вручную в Notion")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_excluded_inns() -> set:
    """Load INNs that should be excluded from all Notion exports."""
    excluded = set()
    enriched_path = DATA / "company_groups_enriched.csv"
    if enriched_path.exists():
        rows = read_csv(enriched_path)
        for r in rows:
            if (r.get("Исключить", "") or "").strip():
                inn = (r.get("ИНН", "") or "").strip()
                if inn:
                    excluded.add(inn)
    return excluded


def main():
    excluded_inns = load_excluded_inns()
    print(f"Исключённых компаний (не загружать): {len(excluded_inns)}\n")

    companies_by_inn = build_companies()
    vessels = build_vessels(companies_by_inn)
    quotas = build_quotas(excluded_inns)
    build_equipment_templates()
    print_summary(companies_by_inn, vessels, quotas)


if __name__ == "__main__":
    main()
