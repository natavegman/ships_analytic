#!/usr/bin/env python3
"""
Обогащение судов данными с FleetPhoto: фото, статус, IMO, тип/проект и старые названия.

Читает data/gfw_our_vessels.json и data/fleetphoto_rmrs_vessel_ids.json (создаётся merge_rmrs_fleetphoto.py).
Для каждого судна, найденного в FleetPhoto, загружает карточку и извлекает:
  статус (Текущее состояние), ссылку на первое фото, IMO, тип/проект судна, старые названия.
Дописывает в запись: fleetphoto_url, fleetphoto_photo_url, fleetphoto_status,
  fleetphoto_imo, fleetphoto_project (тип/проект), fleetphoto_old_names.

Статус сохраняется как на сайте (поле «Текущее состояние»): без перевода в коды.
Траулеры: https://fleetphoto.ru/projects/34/ (подразделы по типам/проектам).

Запуск:
  python3 scripts/merge_rmrs_fleetphoto.py   # один раз или при обновлении списка РМРС
  python3 scripts/enrich_fleetphoto_vessels.py
  python3 scripts/enrich_fleetphoto_vessels.py --limit 20
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
GFW_VESSELS = DATA / "gfw_our_vessels.json"
FLEETPHOTO_IDS = DATA / "fleetphoto_rmrs_vessel_ids.json"
FLEETPHOTO_VESSEL_URL = "https://fleetphoto.ru/vessel/{id}/?lang=ru"


def normalize_vessel_name(name: str) -> str:
    """Нормализация названия для сопоставления с FleetPhoto (как в merge_rmrs_fleetphoto)."""
    if not name:
        return ""
    s = name.strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r'["«»""\']', "", s)
    return s.strip()


def strip_leading_company(name: str) -> str:
    """Отрезать ведущую «ООО "X" » / «АО "X" »."""
    if not name or not name.strip():
        return name
    s = name.strip()
    rest = re.sub(
        r"^(ООО|АО|ОАО|ЗАО|ПАО|НАО|ИП|ГУП|МУП|КФХ|СПК|СХПК)\s*[\"\"\u201c\u201d][^\"\"\u201c\u201d]*[\"\"\u201c\u201d]\s*",
        "",
        s,
        flags=re.IGNORECASE,
    ).strip()
    return rest if rest else s


def extract_search_name(full_name: str) -> str:
    """Часть в кавычках или до запятой."""
    m = re.search(r'["""]([^"""]+)["""]', full_name)
    if m:
        return m.group(1).strip()
    if "," in full_name:
        return full_name.split(",")[0].strip()
    return full_name.strip()


def _strip_vessel_type_prefix(name: str) -> str:
    """Убрать в начале тип судна (СРТМ, МК-123, РС, БМРТ и т.д.), оставить название."""
    s = name.strip()
    # Типы судов с опциональным номером: СРТМ, МК-0188, РС, РШ, БМРТ, СКТР, МРТК, СТР, АК4315
    m = re.match(
        r"^(?:(?:СРТМ|МК|РС|РШ|БМРТ|СКТР|МРТК|СТР|М|АК)\s*-?\s*\d*\s*[\"']?|Рыболовное судно\s+[\"']?)",
        s,
        re.IGNORECASE,
    )
    if m:
        s = s[m.end() :].strip()
    return s.strip(' "\'"') or name


def name_variants(name: str) -> list[str]:
    """Варианты нормализованного названия для поиска в FleetPhoto (больше вариантов = больше совпадений)."""
    variants = []
    for raw in (
        name,
        strip_leading_company(name),
        extract_search_name(name),
        _strip_vessel_type_prefix(name),
        _strip_vessel_type_prefix(strip_leading_company(name)),
    ):
        n = normalize_vessel_name(raw)
        if n and len(n) >= 2 and n not in variants:
            variants.append(n)
    # Последнее слово часто и есть название судна (АК4315 КУЛОЙ -> КУЛОЙ)
    words = re.split(r"[\s,()]+", name)
    for w in reversed(words):
        w = w.strip(' "\'"')
        if len(w) >= 2 and not w.isdigit():
            n = normalize_vessel_name(w)
            if n and n not in variants:
                variants.append(n)
            break
    return variants


def parse_status_from_html(html: str) -> str:
    """Из HTML карточки судна извлечь «Текущее состояние» как есть (без приведения к каноническому статусу)."""
    # Таблица: Текущее состояние: </td><td>Эксплуатируется</td> или «Прочее», «Продан» и т.д.
    for pattern in (
        r"(?:Текущее состояние|Current state)\s*:\s*</[^>]*>\s*<[^>]*>([^<]+)</",
        r"(?:Текущее состояние|Current state)\s*[:\|]\s*[^>]*>([^<]+)<",
    ):
        m = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
        if not m:
            continue
        raw = m.group(1).strip()
        # Убрать лишние пробелы/переносы
        raw = re.sub(r"\s+", " ", raw).strip()
        if len(raw) < 1 or len(raw) > 200:
            continue
        return raw
    return ""


def parse_first_photo_from_html(html: str) -> str | None:
    """Из HTML карточки извлечь ссылку на первое фото судна (/photo/123/...)."""
    m = re.search(r'/photo/(\d+)/\?vid=\d+', html)
    if m:
        return m.group(1)
    m = re.search(r'/photo/(\d+)/', html)
    if m:
        return m.group(1)
    return None


def parse_imo_from_html(html: str) -> str | None:
    """Из HTML карточки извлечь IMO (уникальный номер судна)."""
    # Ссылка: registry.php?book=imo&code=8722238
    m = re.search(r'registry\.php\?book=imo&amp;code=(\d{6,8})', html)
    if m:
        return m.group(1)
    m = re.search(r'(?:IMO|ИМО)\s*:\s*</[^>]*>\s*<[^>]*>\s*\[?(\d{6,8})\]?', html, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


def parse_project_from_html(html: str) -> str | None:
    """Из HTML карточки извлечь тип/проект судна (Проект 1328, тип Балтика и т.д.). Важно для траулеров и др."""
    # Ссылка на проект: [1328, тип Балтика](https://fleetphoto.ru/projects/740/) или [Проект 394, тип Маяковский](...)
    m = re.search(r"(?:Проект|Design)\s*[:\|]\s*[^>]*\[([^\]]+)\]\([^)]*projects?/\d+", html, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # Ячейка таблицы: >1430, тип Александр Грин<
    m = re.search(r"(?:Проект|Design)\s*[:\|]\s*</[^>]*>\s*<[^>]*>([^<]+)</", html, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return None


def parse_old_names_from_html(html: str) -> list[str]:
    """Из HTML карточки извлечь все названия судна (текущее + старые) из таблицы «Название | Регистрация | Приписка»."""
    names: list[str] = []
    # Блок таблицы с названиями до следующей секции (Проект / Design)
    block = re.search(
        r'\|\s*Название\s*\|\s*Регистрация\s*\|\s*Приписка\s*\|.*?(?=\|\s*Проект:|\|\s*Design:)',
        html,
        re.DOTALL | re.IGNORECASE,
    )
    if not block:
        return names
    text = block.group(0)
    # Первая колонка каждой строки таблицы: | Название |
    for m in re.finditer(r'\|\s*([^|]+?)\s*\|', text):
        cell = m.group(1).strip()
        if not cell or cell in ("Название", "---", "—", "-"):
            continue
        # убрать ссылки [текст](url)
        cell = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', cell).strip()
        if cell and cell not in names:
            names.append(cell)
    return names


def fetch_url(url: str) -> str | None:
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; QuotasAnalytic/1.0)"})
        with urllib.request.urlopen(req, timeout=25) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description="Обогащение судов фото и статусом с FleetPhoto")
    ap.add_argument("--limit", type=int, default=0, help="Макс. судов обработать (0 = все с совпадением в FleetPhoto)")
    ap.add_argument("--dry-run", action="store_true", help="Не сохранять gfw_our_vessels.json")
    args = ap.parse_args()

    if not GFW_VESSELS.exists():
        print(f"Нет {GFW_VESSELS}. Сначала: python3 scripts/build_gfw_vessel_cache.py")
        sys.exit(1)
    if not FLEETPHOTO_IDS.exists():
        print(f"Нет {FLEETPHOTO_IDS}. Сначала: python3 scripts/merge_rmrs_fleetphoto.py")
        sys.exit(1)

    vessels = json.loads(GFW_VESSELS.read_text(encoding="utf-8"))
    ids_map: dict[str, str] = json.loads(FLEETPHOTO_IDS.read_text(encoding="utf-8"))

    # Сопоставить наши суда с vessel_id FleetPhoto
    name_to_id: dict[str, str] = {}
    for v in vessels:
        name = (v.get("name") or "").strip()
        if not name:
            continue
        for variant in name_variants(name):
            if variant in ids_map:
                name_to_id[name] = ids_map[variant]
                break

    matched = len(name_to_id)
    print(f"Судов в кэше: {len(vessels)}, совпадений с FleetPhoto по названию: {matched}")

    to_fetch = [v for v in vessels if (v.get("name") or "").strip() in name_to_id]
    if args.limit and args.limit > 0:
        to_fetch = to_fetch[: args.limit]
    print(f"Будем загружать карточки: {len(to_fetch)}")

    for i, v in enumerate(to_fetch):
        name = (v.get("name") or "").strip()
        vessel_id = name_to_id.get(name)
        if not vessel_id:
            continue
        url = FLEETPHOTO_VESSEL_URL.format(id=vessel_id)
        html = fetch_url(url)
        time.sleep(0.8)
        if not html:
            print(f"  [{i+1}/{len(to_fetch)}] {name[:40]!r} — ошибка загрузки")
            continue
        status = parse_status_from_html(html)
        photo_id = parse_first_photo_from_html(html)
        fp_imo = parse_imo_from_html(html)
        fp_project = parse_project_from_html(html)
        old_names = parse_old_names_from_html(html)
        v["fleetphoto_url"] = url
        v["fleetphoto_status"] = status
        if photo_id:
            v["fleetphoto_photo_url"] = f"https://fleetphoto.ru/photo/{photo_id}/?vid={vessel_id}"
        else:
            v.setdefault("fleetphoto_photo_url", None)
        v["fleetphoto_imo"] = fp_imo
        v["fleetphoto_project"] = fp_project  # тип/проект судна (напр. «1328, тип Балтика», траулеры и др.)
        v["fleetphoto_old_names"] = old_names
        # Если в GFW IMO не было — подставляем из FleetPhoto как единый уникальный ключ
        if fp_imo and not v.get("imo"):
            v["imo"] = fp_imo
        proj = f" | {fp_project[:40]}" if fp_project else ""
        print(f"  [{i+1}/{len(to_fetch)}] {name[:38]!r} -> {status!r}" + (f" IMO={fp_imo}" if fp_imo else "") + proj + (f" | бывш.: {len(old_names)}" if old_names else ""))

    if not args.dry_run:
        GFW_VESSELS.write_text(json.dumps(vessels, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Сохранено: {GFW_VESSELS}")
    else:
        print("Dry-run: файл не сохранён.")

    # Сводка по статусам
    status_counts: dict[str, int] = {}
    for v in vessels:
        s = v.get("fleetphoto_status")
        if s:
            status_counts[s] = status_counts.get(s, 0) + 1
    if status_counts:
        print("Статусы FleetPhoto (как на сайте):", ", ".join(f"{k!r}: {v}" for k, v in sorted(status_counts.items(), key=lambda x: -x[1])))


if __name__ == "__main__":
    main()
