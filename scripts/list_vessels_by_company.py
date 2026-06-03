#!/usr/bin/env python3
"""
Показать суда по компаниям из data/gfw_our_vessels.json.

Запуск:
  python3 scripts/list_vessels_by_company.py                    # все компании
  python3 scripts/list_vessels_by_company.py --company Дальрыба  # только где в названии компании есть «Дальрыба»
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE_PATH = ROOT / "data" / "gfw_our_vessels.json"


def main() -> None:
    ap = argparse.ArgumentParser(description="Список судов по компаниям из кэша GFW")
    ap.add_argument("--company", "-c", type=str, default="", help="Фильтр: только компании, в названии которых есть эта строка")
    ap.add_argument("--with-gfw-only", action="store_true", help="Показывать только суда с gfw_id (сопоставленные с GFW)")
    args = ap.parse_args()

    if not CACHE_PATH.exists():
        print(f"Нет файла {CACHE_PATH}. Сначала: python3 scripts/build_gfw_vessel_cache.py", file=sys.stderr)
        sys.exit(1)

    with open(CACHE_PATH, encoding="utf-8") as f:
        vessels = json.load(f)

    # Группировка по компании (ключ — нормализованное название для объединения вариантов)
    by_company: dict[str, list[dict]] = {}
    for v in vessels:
        company = (v.get("company") or "").strip()
        if not company:
            company = "(без компании)"
        if args.company and args.company.lower() not in company.lower():
            continue
        if args.with_gfw_only and not v.get("gfw_id"):
            continue
        key = company.upper()
        if key not in by_company:
            by_company[key] = []
        by_company[key].append(v)

    for key, rows in sorted(by_company.items()):
        company = (rows[0].get("company") or "").strip() or "(без компании)"
        with_gfw = sum(1 for r in rows if r.get("gfw_id"))
        print(f"\n{company}")
        print(f"  Судов в кэше: {len(rows)}, из них с gfw_id: {with_gfw}")
        for r in rows:
            name = r.get("name") or "—"
            gfw = "✓" if r.get("gfw_id") else "—"
            print(f"    [{gfw}] {name}")
    if not by_company:
        print("Нет компаний по заданному фильтру.", file=sys.stderr)


if __name__ == "__main__":
    main()
