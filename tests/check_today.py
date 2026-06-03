#!/usr/bin/env python3
"""
Проверка «чего не хватает на сегодня»: ключевые файлы и структура данных.

Запуск:
  python3 tests/check_today.py          # отчёт в консоль
  pytest tests/check_today.py -v        # как тесты (каждый чек = тест)
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

# Корень проекта (каталог с data/)
def _project_root() -> Path:
    start = Path(__file__).resolve().parent.parent
    if (start / "data").is_dir():
        return start
    return start

ROOT = _project_root()
DATA = ROOT / "data"
OUTPUT = ROOT / "output"

# Ключевые файлы и минимальные требования
CHECKS = [
    {
        "id": "cerberus_export",
        "path": DATA / "cerberus_export.csv",
        "required": True,
        "description": "Выгрузка Цербера (реестр судов/экспортёров)",
        "csv_required_columns": ["ИНН", "Название_объекта", "Судно"],
        "min_rows": 1,
    },
    {
        "id": "gfw_our_vessels",
        "path": DATA / "gfw_our_vessels.json",
        "required": True,
        "description": "Кэш судов (Цербер + GFW + FleetPhoto)",
        "json_list": True,
        "json_item_keys": ["name", "inn"],
        "min_items": 0,  # может быть пустой список, если только Цербер как fallback
    },
    {
        "id": "quota_summary",
        "path": OUTPUT / "quota_summary.csv",
        "required": True,
        "description": "Квоты (район промысла, группы для карты)",
        "min_rows": 1,
    },
    {
        "id": "company_groups",
        "path": DATA / "company_groups.csv",
        "required": True,
        "description": "Группы компаний (фильтр на карте)",
        "csv_required_columns": ["ИНН", "Группа_Компаний"],
        "min_rows": 0,
    },
    {
        "id": "company_groups_enriched",
        "path": DATA / "company_groups_enriched.csv",
        "required": False,
        "description": "Обогащённые компании (ФНС/list-org)",
        "min_rows": 0,
    },
    {
        "id": "companies_with_export",
        "path": OUTPUT / "companies_with_export.csv",
        "required": False,
        "description": "Компании с экспортом (из Цербера)",
        "min_rows": 0,
    },
]


def _check_file(item: dict) -> tuple[bool, str]:
    """Возвращает (ok, message)."""
    path = item["path"]
    if not path.exists():
        return False, "файл отсутствует"
    if path.stat().st_size == 0:
        return False, "файл пустой"

    # CSV
    if "csv_required_columns" in item:
        try:
            with path.open("r", encoding="utf-8", newline="") as f:
                r = csv.DictReader(f)
                headers = r.fieldnames or []
                rows = list(r)
        except Exception as e:
            return False, f"ошибка чтения CSV: {e}"
        missing = [c for c in item["csv_required_columns"] if c not in headers]
        if missing:
            return False, f"нет колонок: {', '.join(missing)}"
        min_r = item.get("min_rows", 0)
        if len(rows) < min_r:
            return False, f"строк меньше {min_r} (сейчас {len(rows)})"
        return True, f"OK ({len(rows)} строк)"

    # JSON (list of vessels)
    if item.get("json_list"):
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except Exception as e:
            return False, f"ошибка чтения JSON: {e}"
        if not isinstance(data, list):
            return False, "ожидается список"
        keys = item.get("json_item_keys") or []
        min_items = item.get("min_items", 0)
        if len(data) < min_items:
            return False, f"записей меньше {min_items} (сейчас {len(data)})"
        if keys and data:
            first = data[0] if isinstance(data[0], dict) else {}
            missing_k = [k for k in keys if k not in first]
            if missing_k:
                return False, f"в записях нет полей: {', '.join(missing_k)}"
        return True, f"OK ({len(data)} судов)"

    return True, "OK"


def run_checks() -> list[dict]:
    """Выполнить все проверки. Возвращает список {id, path, required, description, ok, message}."""
    results = []
    for c in CHECKS:
        ok, msg = _check_file(c)
        results.append({
            "id": c["id"],
            "path": c["path"],
            "required": c["required"],
            "description": c["description"],
            "ok": ok,
            "message": msg,
        })
    return results


def print_report(results: list[dict]) -> None:
    """Вывести отчёт в консоль."""
    print("Проверка «чего не хватает на сегодня»")
    print("=" * 60)
    missing_required = []
    missing_optional = []
    for r in results:
        status = "OK" if r["ok"] else "НЕТ"
        req = "обязательный" if r["required"] else "опциональный"
        print(f"  [{status}] {r['id']}: {r['message']} ({req})")
        if not r["ok"]:
            if r["required"]:
                missing_required.append((r["id"], r["description"], r["path"]))
            else:
                missing_optional.append((r["id"], r["description"], r["path"]))
    print()
    if missing_required:
        print("Чего не хватает (обязательно):")
        for id_, desc, path in missing_required:
            print(f"  • {id_}: {desc}")
            print(f"    → {path}")
        print()
    if missing_optional:
        print("Чего не хватает (желательно):")
        for id_, desc, path in missing_optional:
            print(f"  • {id_}: {desc}")
    if not missing_required and not missing_optional:
        print("Всё на месте, можно работать.")
    elif not missing_required:
        print("Обязательные файлы есть. Опциональные можно догнать по шагам из docs/STATUS_AND_TASKS.md")


# --- pytest: каждый чек как отдельный тест ---

def pytest_generate_tests(metafunc):
    if "check_result" in metafunc.fixturenames:
        results = run_checks()
        metafunc.parametrize("check_result", results, ids=[r["id"] for r in results])


def test_file_ready(check_result):
    """Каждый ключевой файл должен проходить проверку (для обязательных — обязательно)."""
    if check_result["required"]:
        assert check_result["ok"], (
            f"{check_result['id']}: {check_result['message']} — путь: {check_result['path']}"
        )
    # Опциональные не падают, но можно смотреть отчёт
    # assert check_result["ok"] or not check_result["required"]


def test_today_summary():
    """Сводка: есть ли все обязательные артефакты для работы на сегодня."""
    results = run_checks()
    required_ok = all(r["ok"] for r in results if r["required"])
    missing = [r["id"] for r in results if r["required"] and not r["ok"]]
    assert required_ok, f"Не хватает обязательных файлов: {missing}"


if __name__ == "__main__":
    results = run_checks()
    print_report(results)
    sys.exit(0 if all(r["ok"] for r in results if r["required"]) else 1)
