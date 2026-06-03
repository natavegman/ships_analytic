#!/usr/bin/env python3
"""
Строит кэш: наши суда (Цербер) -> GFW vessel id.
Читает data/cerberus_export.csv (Судно=1), ищет каждое в GFW API по названию,
сохраняет в data/gfw_our_vessels.json.

Требует GFW_API_TOKEN в окружении.
По умолчанию использует существующий data/gfw_our_vessels.json: запрос к GFW только
для судов без gfw_id или с ошибкой. Флаг --full — пересобрать кэш с нуля.
Запуск: python scripts/build_gfw_vessel_cache.py [--full] [--limit N]
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import pandas as pd

# project root
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# подгрузить .env из корня (GFW_API_TOKEN)
_env = ROOT / ".env"
if _env.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_env)
    except ImportError:
        import os
        for line in _env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip().strip('"').strip("'")

from scripts.gfw_client import get_token, vessels_search


def load_our_vessels() -> list[dict]:
    """Судна из Цербера (Судно=1), с непустым Название_объекта."""
    path = ROOT / "data" / "cerberus_export.csv"
    df = pd.read_csv(path, dtype=str)
    df = df[df["Судно"] == "1"].drop_duplicates(subset=["Название_объекта", "ИНН"])
    df = df[df["Название_объекта"].notna() & (df["Название_объекта"].str.strip() != "")]
    rows = df[["ИНН", "Название_объекта", "Хоз_субъект"]].to_dict("records")
    return [{"inn": r["ИНН"], "name": r["Название_объекта"], "company": r.get("Хоз_субъект") or ""} for r in rows]


# Транслитерация RU -> EN (GFW API возвращает 422 на кириллице)
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
    "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}
for _k, _v in list(_TRANSLIT.items()):
    _TRANSLIT[_k.upper()] = _v.upper() if len(_v) == 1 else _v.capitalize()


def _to_latin(s: str) -> str:
    """Кириллица -> латиница для запроса к API."""
    return "".join(_TRANSLIT.get(c, c) for c in s)


# Формы собственности в начале строки = название компании, а не судна
_COMPANY_PREFIXES = (
    r"^ООО\b", r"^АО\b", r"^ОАО\b", r"^ЗАО\b", r"^ПАО\b", r"^НАО\b",
    r"^ИП\b", r"^ГУП\b", r"^МУП\b", r"^КФХ\b", r"^СПК\b", r"^СХПК\b",
    r"^Общество\s+с\s+ограниченной\s+ответственностью\b",
    r"^Акционерное\s+общество\b", r"^Публичное\s+акционерное\s+общество\b",
    r"^Индивидуальный\s+предприниматель\b",
)
_COMPANY_PATTERN = re.compile("|".join(f"({p})" for p in _COMPANY_PREFIXES), re.IGNORECASE)


def looks_like_company_name(name: str) -> bool:
    """True, если строка похожа на название компании (форма собственности в начале)."""
    if not name or not name.strip():
        return False
    s = name.strip()
    return _COMPANY_PATTERN.search(s) is not None


# «Форма + название в кавычках» в начале — отрезаем, остаток = судно (напр. ООО "ССВ" МРТР "Гриф" -> МРТР "Гриф")
_LEADING_COMPANY_QUOTED = re.compile(
    r"^(ООО|АО|ОАО|ЗАО|ПАО|НАО|ИП|ГУП|МУП|КФХ|СПК|СХПК)\s*[\"\"\u201c\u201d][^\"\"\u201c\u201d]*[\"\"\u201c\u201d]\s*",
    re.IGNORECASE,
)


def strip_leading_company(name: str) -> str:
    """
    Если в начале «форма собственности + название в кавычках», возвращает остаток (название судна).
    Иначе возвращает исходную строку.
    Примеры: 'ООО "ССВ" МРТР "Гриф"' -> 'МРТР "Гриф"'; 'АО"АТЛАНТРЫБФЛОТ" СРТМ К-2165 "Освейское"' -> 'СРТМ К-2165 "Освейское"'.
    """
    if not name or not name.strip():
        return name
    s = name.strip()
    rest = _LEADING_COMPANY_QUOTED.sub("", s).strip()
    return rest if rest else s


def extract_search_name(full_name: str) -> str:
    """Упрощённое имя для поиска в GFW (латиница/цифры часто в GFW)."""
    # Вытащить часть в кавычках: СКТР "Стелла Карина" -> Стелла Карина
    m = re.search(r'["""]([^"""]+)["""]', full_name)
    if m:
        return m.group(1).strip()
    # Или последнее слово/два перед запятой
    if "," in full_name:
        part = full_name.split(",")[0].strip()
        return part
    return full_name.strip()


def _cache_key(rec: dict) -> tuple[str, str]:
    """Ключ для сопоставления записи кэша с судном из Цербера (name, inn)."""
    return (str(rec.get("name") or "").strip(), str(rec.get("inn") or "").strip())


def load_existing_cache(out_path: Path) -> dict[tuple[str, str], dict]:
    """Загружает существующий кэш: ключ (name, inn) -> запись."""
    if not out_path.exists():
        return {}
    try:
        data = json.loads(out_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, list):
        return {}
    return {_cache_key(r): r for r in data if _cache_key(r) != ("", "")}


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Кэш суда Цербер → GFW id")
    ap.add_argument("--limit", type=int, default=0, help="Макс. судов обработать (0 = все). Для отладки: --limit 3")
    ap.add_argument("--full", action="store_true", help="Пересобрать кэш с нуля (игнорировать существующий gfw_our_vessels.json)")
    args = ap.parse_args()

    if not get_token():
        print("Задайте GFW_API_TOKEN в окружении. Токен: https://globalfishingwatch.org/our-apis/tokens")
        sys.exit(1)

    vessels = load_our_vessels()
    if args.limit and args.limit > 0:
        vessels = vessels[: args.limit]
        print(f"Обрабатываем первых {len(vessels)} судов (--limit {args.limit})")
    else:
        print(f"Наших судов в Цербере: {len(vessels)}")

    out_path = ROOT / "data" / "gfw_our_vessels.json"
    existing = {} if args.full else load_existing_cache(out_path)

    def use_cached(v: dict) -> dict | None:
        """Если в кэше есть запись с gfw_id или gfw_skip_reason — вернуть её (не дергать API)."""
        key = (v["name"].strip(), v["inn"].strip())
        rec = existing.get(key)
        if rec is None:
            return None
        if rec.get("gfw_id") or rec.get("gfw_skip_reason"):
            return rec
        return None  # в кэше ошибка или пусто — повторим запрос

    if existing and not args.full:
        need_api = sum(1 for v in vessels if use_cached(v) is None)
        print(f"В кэше записей: {len(existing)}. Запрос к GFW только для судов без gfw_id/пропуска: {need_api}")
    cache: list[dict] = []
    for i, v in enumerate(vessels):
        name = v["name"]
        cached = use_cached(v)
        if cached is not None:
            cache.append(cached)
            print(f"  [{i+1}/{len(vessels)}] {name!r} -> из кэша gfw_id={cached.get('gfw_id')!r}")
            continue
        # Нет в кэше или в кэше ошибка — запрос к API
        # Исключение: «ООО "ССВ" МРТР "Гриф"» — отрезаем ведущую компанию, ищем по судну
        name_for_search = strip_leading_company(name)
        search_name = extract_search_name(name_for_search)
        query = search_name if search_name else name_for_search
        if not query:
            cache.append({"name": name, "inn": v["inn"], "company": v.get("company", ""), "gfw_id": None, "gfw_name": None})
            print(f"  [{i+1}/{len(vessels)}] {name!r} -> нет имени для поиска")
            continue
        if looks_like_company_name(name_for_search) or looks_like_company_name(query):
            cache.append({
                "name": name, "inn": v["inn"], "company": v.get("company", ""),
                "gfw_id": None, "gfw_name": None, "gfw_skip_reason": "company_name",
            })
            print(f"  [{i+1}/{len(vessels)}] {name!r} -> пропуск (название компании)")
            continue
        # GFW API не принимает кириллицу (422) — ищем латиницей
        query_latin = _to_latin(query)
        try:
            entries = vessels_search(query_latin, limit=5)
        except Exception as e:
            print(f"  [{i+1}/{len(vessels)}] {name!r} -> ошибка: {e}")
            cache.append({"name": name, "inn": v["inn"], "company": v.get("company", ""), "gfw_id": None, "gfw_name": None, "error": str(e)})
            # После 503/429 даём API отдохнуть перед следующим запросом
            time.sleep(8)
            continue
        gfw_id = None
        gfw_name = None
        imo = None
        if entries:
            first = entries[0]
            gfw_id = first.get("id") or first.get("vesselId")
            gfw_name = first.get("name") or first.get("label") or (first.get("vessel", {}) or {}).get("name")
            imo = first.get("imo")  # IMO — уникальный номер судна (если есть в GFW)
        cache.append({
            "name": name,
            "inn": v["inn"],
            "company": v.get("company", ""),
            "gfw_id": gfw_id,
            "gfw_name": gfw_name,
            "imo": imo,
        })
        print(f"  [{i+1}/{len(vessels)}] {name!r} -> gfw_id={gfw_id!r}")
        time.sleep(0.6)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    print(f"Сохранено: {out_path}")
    matched = sum(1 for c in cache if c.get("gfw_id"))
    skipped_company = sum(1 for c in cache if c.get("gfw_skip_reason") == "company_name")
    print(f"Сопоставлено с GFW: {matched} из {len(cache)}")
    if skipped_company:
        print(f"Пропущено (название компании, не судно): {skipped_company}")

if __name__ == "__main__":
    main()
