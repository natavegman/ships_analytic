#!/usr/bin/env python3
"""
Сопоставление судов из Цербера со списком на FleetPhoto (fleetphoto.ru).

Источники списка судов FleetPhoto:
1) РМРС — список судов РМРС постранично (list.php?rgid=2).
2) Раздел «Рыболовные суда» (projects/793/) и все подразделы: Траулеры, Сейнеры,
   Ярусоловы и т.д. — обход всех страниц и подпроектов, сбор vessel_id по названию.

У судна на FleetPhoto нет поля «судовладелец»/ИНН, поэтому считаем только совпадение
по названию судна.

Использование:
  python3 scripts/merge_rmrs_fleetphoto.py

Требует: data/cerberus_export.csv, output/companies_with_export.csv.
Обновляет в companies_with_export колонки РМРС_судов и FleetPhoto_судов.
"""

from __future__ import annotations

import csv
import json
import re
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = BASE_DIR / "output"
CERBERUS_CSV = DATA_DIR / "cerberus_export.csv"
COMPANIES_WITH_EXPORT = OUTPUT_DIR / "companies_with_export.csv"
FLEETPHOTO_CACHE = DATA_DIR / "fleetphoto_rmrs_vessels.txt"  # список названий по строкам
FLEETPHOTO_IDS_CACHE = DATA_DIR / "fleetphoto_rmrs_vessel_ids.json"  # нормализованное_название -> vessel_id
FLEETPHOTO_LIST_URL = "https://fleetphoto.ru/list.php?lang=ru&rgid=2&st={offset}"
# Раздел «Рыболовные суда» и все подразделы (Траулеры, Сейнеры и т.д.) — для сбора судов по типам
FLEETPHOTO_PROJECTS_793_BASE = "https://fleetphoto.ru/projects/793/"


def normalize_vessel_name(name: str) -> str:
    """Нормализация названия судна для сопоставления."""
    if not name:
        return ""
    s = name.strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r'["«»""\']', "", s)
    return s.strip()


def load_cerberus_vessels_by_inn(cerberus_path: Path) -> dict[str, list[str]]:
    """ИНН -> список названий судов (объектов с Судно=1)."""
    inn_vessels: dict[str, list[str]] = {}
    if not cerberus_path.exists():
        return inn_vessels
    with cerberus_path.open("r", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            if row.get("Судно") != "1":
                continue
            name = (row.get("Название_объекта") or "").strip()
            if not name:
                continue
            inn = (row.get("ИНН") or "").strip()
            if not inn or len(inn) not in (10, 12) or not inn.isdigit():
                continue
            inn_vessels.setdefault(inn, []).append(name)
    return inn_vessels


def _fetch_html(url: str) -> str | None:
    """Загрузить HTML страницу с FleetPhoto."""
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; QuotasAnalytic/1.0)"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None


def _parse_vessels_and_subprojects(html: str) -> tuple[list[tuple[str, str]], set[str]]:
    """Из HTML страницы проекта извлечь (vessel_id, name) и ссылки на подпроекты /projects/N/."""
    vessels: list[tuple[str, str]] = []
    subprojects: set[str] = set()
    # Ссылки на суда: [Название](/vessel/123/) или <a href="/vessel/123/">Название</a>
    for m in re.finditer(r'/vessel/(\d+)/', html):
        vessel_id = m.group(1)
        # В HTML: <a href="/vessel/123/">Название</a> — имя после > перед </a>
        after = html[m.end() : m.end() + 300]
        name_match = re.search(r'>([^<]+)</a>', after)
        if name_match:
            name = name_match.group(1).strip()
        else:
            # Иногда разметка: [Название](/vessel/123/) — имя в предыдущем [name]
            before = html[max(0, m.start() - 120) : m.start()]
            name_in_before = re.search(r'\[([^\]]+)\]\s*\(\s*$', before)
            name = name_in_before.group(1).strip() if name_in_before else ""
        if name and name not in ("(неизвестно)", "???", "««", "»»", "1", "2", "3") and len(name) > 1:
            vessels.append((vessel_id, name))
    for m in re.finditer(r'/projects/(\d+)/', html):
        subprojects.add(m.group(1))
    return vessels, subprojects


def fetch_fleetphoto_projects_793(
    max_pages_total: int = 800,
    page_size: int = 50,
) -> tuple[set[str], dict[str, str]]:
    """Обходит раздел Рыболовные суда (projects/793/) и все подразделы, собирает названия и vessel_id."""
    try:
        import urllib.request
    except ImportError:
        return set(), {}
    seen_names: set[str] = set()
    ids_map: dict[str, str] = {}
    base_url = "https://fleetphoto.ru"
    visited_projects: set[str] = set()
    to_visit: list[tuple[str, int]] = [("793", 0)]  # (project_id, page_offset)
    pages_fetched = 0

    while to_visit and pages_fetched < max_pages_total:
        project_id, offset = to_visit.pop(0)
        if offset == 0 and project_id in visited_projects:
            continue
        if offset == 0:
            visited_projects.add(project_id)
        url = f"{base_url}/projects/{project_id}/"
        if offset > 0:
            url += f"?st={offset}"
        html = _fetch_html(url)
        if not html:
            continue
        pages_fetched += 1
        if pages_fetched % 50 == 0:
            print(f"  Загружено страниц раздела 793: {pages_fetched}, судов: {len(ids_map)}")
        vessels, subprojects = _parse_vessels_and_subprojects(html)
        for vessel_id, name in vessels:
            norm = normalize_vessel_name(name)
            if norm and norm not in ids_map:
                seen_names.add(norm)
                ids_map[norm] = vessel_id
        # Добавить подпроекты в очередь (только первую страницу каждого)
        for pid in subprojects:
            if pid != project_id and pid not in visited_projects:
                to_visit.append((pid, 0))
        # Пагинация: если нашли много судов на странице, запросить следующую
        if len(vessels) >= page_size:
            to_visit.append((project_id, offset + page_size))
        time.sleep(0.4)

    return seen_names, ids_map


def fetch_fleetphoto_rmrs_list(max_pages: int = 10, page_size: int = 500) -> tuple[set[str], dict[str, str]]:
    """Скачивает страницы списка РМРС с FleetPhoto. Возвращает (множество названий, словарь norm_name -> vessel_id)."""
    try:
        import urllib.request
    except ImportError:
        return set(), {}
    seen: set[str] = set()
    ids_map: dict[str, str] = {}
    for page in range(max_pages):
        offset = page * page_size
        url = FLEETPHOTO_LIST_URL.format(offset=offset)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; QuotasAnalytic/1.0)"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                html = resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            print(f"  Ошибка загрузки {url}: {e}")
            break
        # Ищем ссылки: /vessel/123/">Название</a> — сохраняем id и нормализованное название
        for m in re.finditer(r'/vessel/(\d+)/[^>]*>([^<]+)</a>', html):
            vessel_id, name = m.group(1), m.group(2).strip()
            if name and name not in ("(неизвестно)", "???", "««", "»»") and len(name) > 1:
                norm = normalize_vessel_name(name)
                if norm:
                    seen.add(norm)
                    if norm not in ids_map:
                        ids_map[norm] = vessel_id
        time.sleep(0.5)
        if page == 0:
            print(f"  Загружена 1-я страница РМРС (FleetPhoto), найдено названий: {len(seen)}, id: {len(ids_map)}")
    return seen, ids_map


def load_cached_fleetphoto_names(cache_path: Path) -> set[str]:
    if not cache_path.exists():
        return set()
    names = set()
    with cache_path.open("r", encoding="utf-8") as f:
        for line in f:
            n = normalize_vessel_name(line.strip())
            if n:
                names.add(n)
    return names


def save_fleetphoto_cache(cache_path: Path, names: set[str]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with cache_path.open("w", encoding="utf-8") as f:
        for n in sorted(names):
            f.write(n + "\n")


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Сопоставление судов Цербера со списком РМРС на FleetPhoto")
    ap.add_argument("--refresh", action="store_true", help="Перезагрузить список РМРС с FleetPhoto (все страницы), обновить кэш названий и vessel_id")
    args = ap.parse_args()

    if not CERBERUS_CSV.exists():
        print(f"Нет файла {CERBERUS_CSV}. Сначала выполните выгрузку Цербера.")
        return
    if not COMPANIES_WITH_EXPORT.exists():
        print(f"Нет файла {COMPANIES_WITH_EXPORT}. Сначала: python3 scripts/fetch_cerberus_export.py --merge-only")
        return

    inn_vessels = load_cerberus_vessels_by_inn(CERBERUS_CSV)
    total_cerberus_vessels = sum(len(v) for v in inn_vessels.values())
    print(f"Суда в Цербере по компаниям (ИНН): {sum(len(v) for v in inn_vessels.values())} судов, {len(inn_vessels)} компаний")

    fleetphoto_names = load_cached_fleetphoto_names(FLEETPHOTO_CACHE)
    fleetphoto_ids: dict[str, str] = {}
    if FLEETPHOTO_IDS_CACHE.exists():
        try:
            fleetphoto_ids = json.loads(FLEETPHOTO_IDS_CACHE.read_text(encoding="utf-8"))
        except Exception:
            pass
    # Если нет списка названий, нет файла с vessel_id или запрошен --refresh — загружаем список
    need_fetch = not fleetphoto_names or not FLEETPHOTO_IDS_CACHE.exists() or getattr(args, "refresh", False)
    if need_fetch:
        print("Загрузка списка РМРС с FleetPhoto (все страницы, ~55)...")
        fleetphoto_names, fleetphoto_ids = fetch_fleetphoto_rmrs_list(max_pages=55)
        print("Обход раздела «Рыболовные суда» (projects/793/) и всех подразделов...")
        names_793, ids_793 = fetch_fleetphoto_projects_793(max_pages_total=800, page_size=50)
        # Объединяем: РМРС + рыболовные суда (при дубликате названия оставляем существующий id)
        for norm, vid in ids_793.items():
            if norm not in fleetphoto_ids:
                fleetphoto_ids[norm] = vid
                fleetphoto_names.add(norm)
        if fleetphoto_names:
            save_fleetphoto_cache(FLEETPHOTO_CACHE, fleetphoto_names)
            FLEETPHOTO_IDS_CACHE.parent.mkdir(parents=True, exist_ok=True)
            FLEETPHOTO_IDS_CACHE.write_text(json.dumps(fleetphoto_ids, ensure_ascii=False, indent=0), encoding="utf-8")
            print(f"Сохранён кэш: {FLEETPHOTO_CACHE}, названий: {len(fleetphoto_names)}; id: {FLEETPHOTO_IDS_CACHE}")
    else:
        print(f"Использован кэш FleetPhoto/РМРС: {FLEETPHOTO_CACHE}, названий: {len(fleetphoto_names)}")
        if fleetphoto_ids:
            print(f"  Загружены vessel_id из {FLEETPHOTO_IDS_CACHE}")

    if not fleetphoto_names:
        print("Не удалось получить список FleetPhoto/РМРС. Колонки РМРС_судов, FleetPhoto_судов останутся пустыми.")
        return

    # По каждой компании считаем, сколько её судов (из Цербера) есть в списке FleetPhoto
    inn_rmrs_count: dict[str, int] = {}
    for inn, vessels in inn_vessels.items():
        count = 0
        for v in vessels:
            if normalize_vessel_name(v) in fleetphoto_names:
                count += 1
        if count > 0:
            inn_rmrs_count[inn] = count

    print(f"Совпадений судов Цербер ↔ FleetPhoto/РМРС: {sum(inn_rmrs_count.values())} по {len(inn_rmrs_count)} компаниям")

    # Обновляем companies_with_export
    rows = []
    with COMPANIES_WITH_EXPORT.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        if "РМРС_судов" not in fieldnames:
            fieldnames.append("РМРС_судов")
        if "FleetPhoto_судов" not in fieldnames:
            fieldnames.append("FleetPhoto_судов")
        for row in reader:
            inn = (row.get("ИНН") or "").strip()
            cnt = str(inn_rmrs_count.get(inn, row.get("РМРС_судов") or row.get("FleetPhoto_судов") or ""))
            row["РМРС_судов"] = cnt
            row["FleetPhoto_судов"] = cnt
            rows.append(row)

    with COMPANIES_WITH_EXPORT.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"Обновлён: {COMPANIES_WITH_EXPORT}")


if __name__ == "__main__":
    main()
