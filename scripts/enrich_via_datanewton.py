#!/usr/bin/env python3
"""
Обогащение справочника компаний через DataNewton API.

Преимущества DataNewton:
  - Структурированный JSON (без парсинга HTML)
  - Граф связей до 2-го уровня (/v1/links)
  - 200 запросов/мин vs 100/день у ФНС ЕГРЮЛ
  - Финансы, контакты, скоринг

Скрипт:
  1. Загружает data/company_groups_enriched.csv (ваши ручные группы сохраняются)
  2. Дообогащает компании без данных через DataNewton:
     - Директор (ИНН ФЛ), учредители, предшественники/преемники
  3. Для компаний с ОГРН запрашивает граф связей → автоматическая группировка
  4. Распространяет группы по директору, реорганизациям, учредителям
  5. Сохраняет результат

Запуск:
    python3 scripts/enrich_via_datanewton.py                 # полный цикл
    python3 scripts/enrich_via_datanewton.py --links-only    # только граф связей
    python3 scripts/enrich_via_datanewton.py --dry-run       # показать что будет сделано
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
from datanewton_client import DataNewtonClient, CompanyData

BASE_DIR = Path(__file__).resolve().parents[1]
QUOTA_CSV = BASE_DIR / "output" / "quota_summary.csv"
ENRICHED_CSV = BASE_DIR / "data" / "company_groups_enriched.csv"
GROUPS_CSV = BASE_DIR / "data" / "company_groups.csv"

FIELDNAMES = [
    "Группа_Компаний", "Юр_Лицо", "ИНН", "ОГРН", "Комментарий", "Исключить",
    "Контакты_ListOrg", "Директор_ListOrg", "Директор_ИНН_ФЛ",
    "Учредители_JSON", "Связанные_Компании_JSON", "ListOrg_URL",
    "Финансовые_Данные_JSON",
]


@dataclass
class CompanyInfo:
    inn: str
    ogrn: str = ""
    legal_name: str = ""
    group: str = ""
    comment: str = ""
    exclude: str = ""
    contacts: str = ""
    director: str = ""
    director_inn_fl: str = ""
    founders_json: str = ""
    related_companies_json: str = ""
    list_org_url: str = ""
    financial_data_json: str = ""


def read_csv_safe(path: Path) -> list[dict]:
    for enc in ("utf-8", "utf-8-sig", "cp1251"):
        try:
            with open(path, encoding=enc, newline="") as f:
                first = f.readline()
                if ";" not in first and "," in first:
                    f.seek(0)
                    return list(csv.DictReader(f))
                elif ";" in first:
                    # Semicolons: skip label line if present
                    if first.strip().count(";") == 0:
                        reader = csv.DictReader(f, delimiter=";")
                    else:
                        f.seek(0)
                        reader = csv.DictReader(f, delimiter=";")
                    return list(reader)
                else:
                    f.seek(0)
                    return list(csv.DictReader(f))
        except (UnicodeDecodeError, UnicodeError):
            continue
    raise RuntimeError(f"Cannot read {path}")


def load_enriched() -> Dict[str, CompanyInfo]:
    """Load existing enriched data. User's manual groups are preserved."""
    mapping: Dict[str, CompanyInfo] = {}
    if not ENRICHED_CSV.exists():
        return mapping

    rows = read_csv_safe(ENRICHED_CSV)
    for row in rows:
        inn = (row.get("ИНН") or "").strip()
        if not inn or not inn.isdigit() or len(inn) < 8:
            continue
        mapping[inn] = CompanyInfo(
            inn=inn,
            ogrn=(row.get("ОГРН") or "").strip(),
            legal_name=(row.get("Юр_Лицо") or "").strip(),
            group=(row.get("Группа_Компаний") or "").strip(),
            comment=(row.get("Комментарий") or "").strip(),
            exclude=(row.get("Исключить") or "").strip(),
            contacts=(row.get("Контакты_ListOrg") or "").strip(),
            director=(row.get("Директор_ListOrg") or "").strip(),
            director_inn_fl=(row.get("Директор_ИНН_ФЛ") or "").strip(),
            founders_json=(row.get("Учредители_JSON") or "").strip(),
            related_companies_json=(row.get("Связанные_Компании_JSON") or "").strip(),
            list_org_url=(row.get("ListOrg_URL") or "").strip(),
            financial_data_json=(row.get("Финансовые_Данные_JSON") or "").strip(),
        )
    return mapping


def load_companies_from_quota() -> Dict[str, str]:
    """Load unique INN → legal_name from quota_summary."""
    result: Dict[str, str] = {}
    if not QUOTA_CSV.exists():
        return result
    for row in read_csv_safe(QUOTA_CSV):
        inn = (row.get("ИНН") or "").strip()
        if inn and inn.isdigit() and len(inn) >= 8:
            if inn not in result:
                result[inn] = (row.get("Юр_Лицо") or "").strip()
    return result


def save_enriched(enriched: Dict[str, CompanyInfo]) -> None:
    """Save enriched data to CSV."""
    active = {inn: info for inn, info in enriched.items()
              if "ликвидир" not in (info.comment + " " + info.contacts).lower()
              or info.group}

    with open(ENRICHED_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for inn, info in sorted(active.items()):
            writer.writerow({
                "Группа_Компаний": info.group,
                "Юр_Лицо": info.legal_name,
                "ИНН": info.inn,
                "ОГРН": info.ogrn,
                "Комментарий": info.comment,
                "Исключить": info.exclude,
                "Контакты_ListOrg": info.contacts,
                "Директор_ListOrg": info.director,
                "Директор_ИНН_ФЛ": info.director_inn_fl,
                "Учредители_JSON": info.founders_json,
                "Связанные_Компании_JSON": info.related_companies_json,
                "ListOrg_URL": info.list_org_url,
                "Финансовые_Данные_JSON": info.financial_data_json,
            })


def save_groups_csv(enriched: Dict[str, CompanyInfo]) -> None:
    """Update company_groups.csv with groups from enriched."""
    fields = ["Группа_Компаний", "Юр_Лицо", "ИНН", "Комментарий",
              "Контакты_ListOrg", "Директор_ListOrg", "ListOrg_URL"]
    with open(GROUPS_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for inn, info in sorted(enriched.items()):
            if not info.group or info.exclude:
                continue
            writer.writerow({
                "Группа_Компаний": info.group,
                "Юр_Лицо": info.legal_name,
                "ИНН": info.inn,
                "Комментарий": info.comment,
                "Контакты_ListOrg": info.contacts,
                "Директор_ListOrg": info.director,
                "ListOrg_URL": info.list_org_url,
            })


def apply_datanewton_data(info: CompanyInfo, data: CompanyData) -> None:
    """Merge DataNewton data into existing CompanyInfo, preserving manual edits.

    Note: free tier doesn't return managers/owners, so director/founders
    remain from FNS EGRUL enrichment. DataNewton adds OGRN, status,
    predecessors/successors.
    """
    if not data.is_active:
        info.comment = (info.comment + f"; Ликвидирована ({data.status})").strip("; ")
        return

    if data.short_name and not info.legal_name:
        info.legal_name = data.short_name
    if data.ogrn:
        info.ogrn = data.ogrn

    # Free tier: managers/owners are empty. Only apply if non-empty.
    if data.director and not info.director:
        info.director = data.director
    if data.director_inn_fl and not info.director_inn_fl:
        info.director_inn_fl = data.director_inn_fl
    if data.founders and not info.founders_json:
        info.founders_json = json.dumps(data.founders, ensure_ascii=False)

    related = data.predecessors + data.successors
    if related and not info.related_companies_json:
        info.related_companies_json = json.dumps(related, ensure_ascii=False)

    if data.address and not info.contacts:
        info.contacts = f"Адрес: {data.address}"
    if data.contacts:
        existing = info.contacts or ""
        if data.contacts not in existing:
            info.contacts = (existing + "; " + data.contacts).strip("; ")


# ---------------------------------------------------------------------------
# Group propagation (same logic as old script, optimized)
# ---------------------------------------------------------------------------

def propagate_by_director(enriched: Dict[str, CompanyInfo]) -> int:
    by_dir_inn: Dict[str, set[str]] = defaultdict(set)
    by_dir_name: Dict[str, set[str]] = defaultdict(set)
    for inn, info in enriched.items():
        if info.exclude:
            continue
        if info.director_inn_fl:
            by_dir_inn[info.director_inn_fl].add(inn)
        elif info.director:
            key = " ".join(info.director.upper().split())
            if key:
                by_dir_name[key].add(inn)

    count = 0
    for _, inns in list(by_dir_inn.items()) + list(by_dir_name.items()):
        if len(inns) < 2:
            continue
        groups = {enriched[i].group for i in inns if enriched[i].group}
        if len(groups) == 1:
            g = next(iter(groups))
            for inn in inns:
                if enriched[inn].group == "" and not enriched[inn].exclude:
                    enriched[inn].group = g
                    count += 1
    return count


def propagate_by_founders(enriched: Dict[str, CompanyInfo]) -> int:
    by_founder: Dict[str, set[str]] = defaultdict(set)
    for inn, info in enriched.items():
        if info.exclude:
            continue
        if info.founders_json:
            try:
                for f in json.loads(info.founders_json):
                    f_inn = f.get("inn", "")
                    if f_inn:
                        by_founder[f_inn].add(inn)
            except (json.JSONDecodeError, TypeError):
                pass
        if info.director_inn_fl:
            by_founder[info.director_inn_fl].add(inn)

    count = 0
    for _, inns in by_founder.items():
        if len(inns) < 2:
            continue
        groups = {enriched[i].group for i in inns if i in enriched and enriched[i].group}
        if len(groups) == 1:
            g = next(iter(groups))
            for inn in inns:
                if inn in enriched and not enriched[inn].group and not enriched[inn].exclude:
                    enriched[inn].group = g
                    count += 1
    return count


def propagate_by_reorgs(enriched: Dict[str, CompanyInfo]) -> int:
    graph: Dict[str, set[str]] = defaultdict(set)
    for inn, info in enriched.items():
        if info.exclude or not info.related_companies_json:
            continue
        try:
            for r in json.loads(info.related_companies_json):
                r_inn = r.get("inn", "")
                if r_inn:
                    graph[inn].add(r_inn)
                    graph[r_inn].add(inn)
        except (json.JSONDecodeError, TypeError):
            pass

    count = 0
    changed = True
    iters = 0
    while changed and iters < 10:
        changed = False
        iters += 1
        for inn, neighbors in graph.items():
            info = enriched.get(inn)
            if not info or not info.group:
                continue
            for n_inn in neighbors:
                n_info = enriched.get(n_inn)
                if n_info and not n_info.group and not n_info.exclude:
                    n_info.group = info.group
                    count += 1
                    changed = True
    return count


def propagate_by_links_graph(enriched: Dict[str, CompanyInfo],
                             client: DataNewtonClient,
                             limit: int = 50) -> int:
    """Use DataNewton /v1/links to discover connected companies and propagate groups."""
    companies_with_ogrn = [
        (inn, info) for inn, info in enriched.items()
        if info.ogrn and info.group and not info.exclude
    ]
    if not companies_with_ogrn:
        print("  No companies with OGRN and group for links analysis")
        return 0

    companies_with_ogrn = companies_with_ogrn[:limit]
    our_inns = set(enriched.keys())
    count = 0

    for i, (inn, info) in enumerate(companies_with_ogrn, 1):
        if i % 10 == 0:
            print(f"  Links: {i}/{len(companies_with_ogrn)}...")

        links = client.get_links(info.ogrn)
        if not links:
            continue

        for node in links.nodes:
            if node.inn and node.inn in our_inns and node.inn != inn:
                target = enriched.get(node.inn)
                if target and not target.group and not target.exclude:
                    target.group = info.group
                    count += 1

    return count


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Enrich companies via DataNewton API")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done")
    parser.add_argument("--links-only", action="store_true", help="Only run links graph analysis")
    parser.add_argument("--local", action="store_true",
                        help="Offline mode: only propagate groups using existing data (no API calls)")
    parser.add_argument("--limit", type=int, default=0, help="Max companies to enrich (0=all)")
    parser.add_argument("--links-limit", type=int, default=50, help="Max companies for links graph")
    args = parser.parse_args()

    print("=== Обогащение компаний через DataNewton API ===\n")

    # Load data
    enriched = load_enriched()
    quota_companies = load_companies_from_quota()

    # Add new companies from quota that are not yet in enriched
    for inn, name in quota_companies.items():
        if inn not in enriched:
            enriched[inn] = CompanyInfo(inn=inn, legal_name=name)

    excluded = sum(1 for i in enriched.values() if i.exclude)
    with_group = sum(1 for i in enriched.values() if i.group)
    with_director_inn = sum(1 for i in enriched.values() if i.director_inn_fl)

    print(f"Загружено компаний: {len(enriched)}")
    print(f"  С группами: {with_group}")
    print(f"  Исключено: {excluded}")
    print(f"  С ИНН директора: {with_director_inn}")
    print(f"  Без данных директора: {len(enriched) - with_director_inn - excluded}")

    if args.dry_run:
        need_enrich = [inn for inn, info in enriched.items()
                       if not info.director_inn_fl and not info.exclude and len(inn) == 10]
        print(f"\n[DRY RUN] Будет обогащено: {len(need_enrich)} компаний")
        return

    client = None
    if not args.local:
        try:
            client = DataNewtonClient()
        except ValueError as e:
            print(f"\n{e}")
            print("Добавьте в .env: DATANEWTON_API_KEY=ваш_ключ")
            print("Получить: https://datanewton.ru → Регистрация → API ключ")
            print("Или используйте --local для оффлайн-распространения групп")
            return

        print("\nПроверка DataNewton API...", end=" ", flush=True)
        ok, msg = client.test_connection()
        if ok:
            print(f"✓ ({msg})")
        else:
            print(f"✗ ({msg})")
            print("Переключаюсь в оффлайн-режим (--local)")
            client = None

    # Phase 1: Get OGRN + status + predecessors/successors for companies without OGRN
    # On free tier: managers/owners not returned, so we focus on OGRN (needed for links)
    if client and not args.links_only:
        need_ogrn = [
            inn for inn, info in enriched.items()
            if not info.ogrn
            and not info.exclude
            and len(inn) == 10  # юрлица
        ]
        # Prioritize: companies with groups first (their OGRN needed for links phase)
        need_ogrn.sort(key=lambda i: (0 if enriched[i].group else 1, i))

        if args.limit:
            need_ogrn = need_ogrn[:args.limit]

        print(f"\n--- Фаза 1: Получение ОГРН/статус через /v1/counterparty ({len(need_ogrn)} компаний) ---")
        print(f"  (бесплатный тариф: директоры/учредители не возвращаются — для них используйте ФНС ЕГРЮЛ)")

        enriched_count = 0
        liquidated_count = 0
        failed_count = 0

        for i, inn in enumerate(need_ogrn, 1):
            info = enriched[inn]
            print(f"[{i}/{len(need_ogrn)}] {inn} {info.legal_name[:40]}...", end=" ", flush=True)

            data = client.get_counterparty(inn=inn)
            if data is None:
                print("✗ (не найден)")
                failed_count += 1
                continue

            if not data.is_active:
                print(f"✗ (ликвидирована: {data.status})")
                info.comment = (info.comment + f"; Ликвидирована ({data.status})").strip("; ")
                liquidated_count += 1
            else:
                apply_datanewton_data(info, data)
                enriched_count += 1
                ogrn_info = f"ОГРН: {data.ogrn}" if data.ogrn else "no OGRN"
                succ = f", преемн: {len(data.successors)}" if data.successors else ""
                pred = f", предш: {len(data.predecessors)}" if data.predecessors else ""
                print(f"✓ ({ogrn_info}{succ}{pred})")

            if i % 25 == 0:
                save_enriched(enriched)
                print(f"  [saved, requests: {client.request_count}]")

        print(f"\nОбогащено: {enriched_count}, ликвидировано: {liquidated_count}, не найдено: {failed_count}")
        save_enriched(enriched)

    # Phase 2: Propagate groups
    print("\n--- Фаза 2: Распространение групп ---")

    n = propagate_by_director(enriched)
    print(f"  По директору: +{n} групп")

    n = propagate_by_reorgs(enriched)
    print(f"  По реорганизациям: +{n} групп")

    n = propagate_by_founders(enriched)
    print(f"  По учредителям: +{n} групп")

    # Phase 3: Links graph (DataNewton-specific, requires API)
    if client:
        print(f"\n--- Фаза 3: Граф связей DataNewton ({args.links_limit} компаний) ---")
        n = propagate_by_links_graph(enriched, client, limit=args.links_limit)
        print(f"  По графу связей: +{n} групп")
    else:
        print("\n--- Фаза 3: Граф связей DataNewton — пропущено (оффлайн-режим) ---")

    # Second round of propagation after links
    n = propagate_by_director(enriched)
    if n:
        print(f"  По директору (2-й раунд): +{n} групп")
    n = propagate_by_founders(enriched)
    if n:
        print(f"  По учредителям (2-й раунд): +{n} групп")

    # Save
    save_enriched(enriched)
    save_groups_csv(enriched)

    # Summary
    total = len(enriched)
    with_group = sum(1 for i in enriched.values() if i.group)
    with_dir = sum(1 for i in enriched.values() if i.director_inn_fl)
    excluded = sum(1 for i in enriched.values() if i.exclude)

    print(f"\n{'='*50}")
    print(f"ИТОГО:")
    print(f"  Всего компаний: {total}")
    print(f"  С группами: {with_group} ({with_group*100//max(total,1)}%)")
    print(f"  Без групп: {total - with_group - excluded}")
    print(f"  Исключено: {excluded}")
    print(f"  С данными директора: {with_dir}")
    print(f"  Запросов к DataNewton: {client.request_count if client else 0}")
    print(f"\nСохранено: {ENRICHED_CSV}")
    print(f"Группы: {GROUPS_CSV}")


if __name__ == "__main__":
    main()
