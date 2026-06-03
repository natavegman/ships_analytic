#!/usr/bin/env python3
"""
Обогащение базы судами из GFW: по нашим компаниям (из Цербера) ищем в GFW суда,
сверяем owner/operator в ответе — добавляем суда, которых нет в Цербере.

Вход: data/cerberus_export.csv (компании = Хоз_субъект при Судно=1).
Выход: data/gfw_enriched_vessels.json — суда из GFW, привязанные к нашим ИНН/компаниям.

Требует GFW_API_TOKEN. Запуск: python scripts/enrich_vessels_from_gfw.py [--limit N]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import pandas as pd

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

from scripts.gfw_client import get_token, vessel_by_id, vessel_identity_text, vessels_search

# Транслитерация для запроса к GFW (API по кириллице возвращает пустой результат/422)
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
    "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}
for _k, _v in list(_TRANSLIT.items()):
    _TRANSLIT[_k.upper()] = _v.upper() if len(_v) == 1 else _v.capitalize()


def _to_latin(s: str) -> str:
    """Кириллица -> латиница для запроса к GFW API."""
    return "".join(_TRANSLIT.get(c, c) for c in s)


def load_our_companies() -> list[dict]:
    """Уникальные компании из Цербера (у которых есть суда Судно=1)."""
    path = ROOT / "data" / "cerberus_export.csv"
    df = pd.read_csv(path, dtype=str)
    df = df[df["Судно"] == "1"].drop_duplicates(subset=["ИНН", "Хоз_субъект"])
    df = df[df["Хоз_субъект"].notna() & (df["Хоз_субъект"].str.strip() != "")]
    return [{"inn": r["ИНН"], "company": r["Хоз_субъект"].strip()} for r in df[["ИНН", "Хоз_субъект"]].to_dict("records")]


def company_to_search_queries(company: str, max_queries: int = 2) -> list[str]:
    """Варианты строки для поиска в GFW по названию компании."""
    # Вытащить часть в кавычках: ООО "ИСТОК-АБ" -> ИСТОК-АБ
    m = re.search(r'["""]([^"""]+)["""]', company)
    if m:
        q = m.group(1).strip()
        if len(q) >= 2:
            return [q[:30]]  # один запрос по кавычковой части
    # Иначе убрать ООО/АО и взять первые слова (до запятой или 2–3 слова)
    cleaned = re.sub(r"\b(ООО|АО|ПАО|ОАО|ЗАО)\b", "", company, flags=re.I).strip()
    cleaned = re.sub(r"[,.]", " ", cleaned).strip()
    words = cleaned.split()
    if not words:
        return []
    # Один запрос: до 3 слов или до 25 символов
    first = " ".join(words[:3])[:25].strip()
    if not first:
        return []
    return [first]


def load_existing_vessel_ids_and_names() -> tuple[set[str], set[str]]:
    """gfw_id и названия судов, которые уже есть (Цербер + кэш)."""
    gfw_ids = set()
    names_norm = set()

    # Из кэша наших судов
    cache_path = ROOT / "data" / "gfw_our_vessels.json"
    if cache_path.exists():
        with open(cache_path, encoding="utf-8") as f:
            for v in json.load(f):
                if v.get("gfw_id"):
                    gfw_ids.add(str(v["gfw_id"]))
                if v.get("name"):
                    names_norm.add(_norm_name(v["name"]))

    # Из Цербера (названия судов)
    cerb_path = ROOT / "data" / "cerberus_export.csv"
    if cerb_path.exists():
        df = pd.read_csv(cerb_path, dtype=str)
        df = df[df["Судно"] == "1"]
        for name in df["Название_объекта"].dropna().unique():
            if str(name).strip():
                names_norm.add(_norm_name(name))

    return gfw_ids, names_norm


def _norm_name(s: str) -> str:
    """Нормализация названия для сравнения."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s or "").strip().upper())[:50]


def identity_matches_company(identity_text: str, company: str) -> bool:
    """Проверка: в identity есть совпадение с названием компании (ключевые слова)."""
    id_norm = _norm_name(identity_text)
    comp_norm = _norm_name(company)
    if not comp_norm or not id_norm:
        return False
    # Ключевые слова компании (без ООО и т.п.)
    comp_clean = re.sub(r"\b(ООО|АО|ПАО|ОАО|ЗАО)\b", "", comp_norm, flags=re.I).strip()
    words = [w for w in comp_clean.split() if len(w) >= 2][:5]
    if not words:
        return False
    return any(w in id_norm for w in words)


def main() -> None:
    ap = argparse.ArgumentParser(description="Обогащение списка судов из GFW по компаниям Цербера")
    ap.add_argument("--limit", type=int, default=30, help="Макс. компаний обработать (0 = все)")
    ap.add_argument("--verbose", "-v", action="store_true", help="Диагностика: по каждой компании — найдено/новых id/совпал owner/добавлено")
    args = ap.parse_args()

    if not get_token():
        print("Задайте GFW_API_TOKEN.")
        sys.exit(1)

    companies = load_our_companies()
    if args.limit and args.limit > 0:
        companies = companies[: args.limit]
    print(f"Компаний для обогащения: {len(companies)}")

    existing_gfw_ids, existing_names = load_existing_vessel_ids_and_names()
    print(f"Уже есть судов (Цербер+кэш): {len(existing_gfw_ids)} gfw_id, {len(existing_names)} названий")

    enriched: list[dict] = []
    seen_gfw_ids = set(existing_gfw_ids)

    for i, co in enumerate(companies):
        company = co["company"]
        inn = co["inn"]
        queries = company_to_search_queries(company)
        if not queries:
            print(f"  [{i+1}/{len(companies)}] {company[:45]!r} -> нет запроса, пропуск")
            continue
        print(f"  [{i+1}/{len(companies)}] {company[:45]!r} ...", end=" ", flush=True)
        added_here = 0
        diag = {"search": 0, "new_id": 0, "identity_ok": 0, "name_dup": 0} if getattr(args, "verbose", False) else None
        for query in queries[:2]:
            try:
                entries = vessels_search(_to_latin(query), limit=8)
            except Exception as e:
                print(f"search err: {e}", flush=True)
                time.sleep(0.5)
                continue
            if diag is not None:
                diag["search"] += len(entries)
            for e in entries:
                gfw_id = e.get("id") or e.get("vesselId")
                if not gfw_id or gfw_id in seen_gfw_ids:
                    continue
                if diag is not None:
                    diag["new_id"] += 1
                try:
                    detail = vessel_by_id(gfw_id)
                except Exception:
                    time.sleep(0.3)
                    continue
                if not detail:
                    continue
                time.sleep(0.35)
                identity = vessel_identity_text(detail)
                if not identity_matches_company(identity, company):
                    continue
                if diag is not None:
                    diag["identity_ok"] += 1
                name = detail.get("name") or e.get("name") or e.get("label") or ""
                if _norm_name(name) in existing_names:
                    if diag is not None:
                        diag["name_dup"] += 1
                    continue
                seen_gfw_ids.add(gfw_id)
                enriched.append({
                    "name": name,
                    "gfw_id": gfw_id,
                    "inn": inn,
                    "company": company,
                    "source": "gfw_enrichment",
                })
                added_here += 1
                print(f"\n    + {name!r}", flush=True)
            if entries:
                break
        if diag is not None:
            print(f"поиск={diag['search']} новых_id={diag['new_id']} owner_ok={diag['identity_ok']} дубль_имени={diag['name_dup']} добавлено={added_here}", flush=True)
        else:
            print(f"новых: {added_here}", flush=True)
        time.sleep(0.5)

    out_path = ROOT / "data" / "gfw_enriched_vessels.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(enriched, f, ensure_ascii=False, indent=2)
    print(f"Добавлено судов из GFW: {len(enriched)}. Сохранено: {out_path}")


if __name__ == "__main__":
    main()
