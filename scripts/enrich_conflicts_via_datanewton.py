#!/usr/bin/env python3
"""
Точечное обогащение через DataNewton — только для компаний, которые
resolve_company_groups_ai.py не смог разрешить по данным DaData (пометка
"Требует проверки (ИИ)" в Комментарии).

Почему не enrich_via_datanewton.py: тот скрипт при первом запуске тянет ОГРН
для ВСЕХ компаний без ОГРН — а после DaData-бутстрапа это все 327 строк.
На demo-тарифе DataNewton (~200 запросов) это выжжет бюджет на компаниях,
которые уже и так уверенно разрешены. Здесь — только реально спорные (обычно
до 30), это ОГРН + граф связей (get_links) на каждую: по ~2 запроса.

Из графа связей берём только: предшественники/преемники (реорганизации) —
всегда, и узлы графа, чьи ИНН совпадают с компаниями из НАШЕГО реестра
(company_groups.csv) — чтобы resolve_company_groups_ai.py получил конкретные,
проверяемые связи, а не сотни нерелевантных нод по всей стране.

Использование:
    python3 scripts/enrich_conflicts_via_datanewton.py --dry-run
    python3 scripts/enrich_conflicts_via_datanewton.py
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
from datanewton_client import DataNewtonClient
from resolve_company_groups_ai import find_targets

BASE_DIR = Path(__file__).resolve().parents[1]
ENRICHED_CSV = BASE_DIR / "data" / "company_groups_enriched.csv"
GROUPS_CSV = BASE_DIR / "data" / "company_groups.csv"

ENRICHED_FIELDS = [
    "Группа_Компаний", "Юр_Лицо", "ИНН", "ОГРН", "Комментарий", "Исключить",
    "Контакты_ListOrg", "Директор_ListOrg", "Директор_ИНН_ФЛ",
    "Учредители_JSON", "Связанные_Компании_JSON", "ListOrg_URL",
    "Финансовые_Данные_JSON",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Точечное обогащение спорных компаний через DataNewton")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    if not ENRICHED_CSV.exists():
        raise SystemExit(f"Не найден {ENRICHED_CSV} — сначала bootstrap_enriched_from_dadata.py")

    with ENRICHED_CSV.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    our_inns = {(r.get("ИНН") or "").strip() for r in rows} - {""}
    # Та же логика классификации, что и в резолвере — конфликт (маркер ещё не
    # заменён) или "Требует проверки" от предыдущего прогона без DataNewton.
    unresolved_targets = find_targets(rows)
    target_inns = {(t.row.get("ИНН") or "").strip() for t in unresolved_targets} - {""}
    targets = [r for r in rows if (r.get("ИНН") or "").strip() in target_inns]
    if args.limit:
        targets = targets[:args.limit]

    print(f"Строк всего: {len(rows)}, спорных (цель обогащения): {len(targets)}")
    if args.dry_run:
        for r in targets:
            print(f"  - {r['ИНН']} {r['Юр_Лицо']}")
        print(f"\nОжидаемо запросов к API: ~{len(targets) * 2} (get_counterparty + get_links на компанию)")
        return

    client = DataNewtonClient()
    ok, msg = client.test_connection()
    print(f"Соединение: {ok} ({msg})")
    if not ok:
        raise SystemExit("Нет соединения с DataNewton — прерываю, чтобы не тратить впустую")

    by_inn = {(r.get("ИНН") or "").strip(): r for r in rows}
    enriched_count = 0

    for i, r in enumerate(targets, 1):
        inn = r["ИНН"]
        print(f"[{i}/{len(targets)}] {inn} {r['Юр_Лицо'][:40]}...", end=" ", flush=True)

        info = client.get_counterparty(inn=inn)
        if not info:
            print("✗ (не найдено)")
            continue

        r["ОГРН"] = info.ogrn or r.get("ОГРН", "")
        related = []
        for p in info.predecessors:
            related.append({"inn": p.get("inn", ""), "name": p.get("name", ""), "relation": "предшественник (реорганизация)"})
        for s in info.successors:
            related.append({"inn": s.get("inn", ""), "name": s.get("name", ""), "relation": "преемник (реорганизация)"})

        if info.ogrn:
            links = client.get_links(info.ogrn)
            if links:
                for node in links.nodes:
                    if node.inn and node.inn != inn and node.inn in our_inns:
                        related.append({
                            "inn": node.inn, "name": node.name,
                            "relation": f"узел графа связей ({node.node_type})",
                        })

        if related:
            r["Связанные_Компании_JSON"] = json.dumps(related, ensure_ascii=False)
            enriched_count += 1
            print(f"✓ (связей в нашем реестре: {len(related)})")
        else:
            print("✓ (связей с нашими компаниями не найдено)")

    with ENRICHED_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=ENRICHED_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in ENRICHED_FIELDS})

    print(f"\nСохранено: {ENRICHED_CSV.name}. Обогащено связями: {enriched_count}/{len(targets)}.")
    print(f"Запросов к DataNewton: {client.request_count}")
    print("Дальше: python3 scripts/resolve_company_groups_ai.py")


if __name__ == "__main__":
    main()
