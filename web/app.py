#!/usr/bin/env python3
"""
Веб-интерфейс: карта наших судов (данные Цербер + опционально GFW позиции).

Переменные:
  GFW_API_TOKEN — для подгрузки позиций с GFW (необязательно).
  PORT — порт (по умолчанию 5000).

Запуск: из корня проекта
  pip install -r requirements-gfw.txt
  export GFW_API_TOKEN=...   # опционально
  python web/app.py
  Открыть http://localhost:5000
"""

from __future__ import annotations

import json
import os
import sys
import warnings
from pathlib import Path

# Убрать из лога предупреждение urllib3 про LibreSSL/OpenSSL на macOS
warnings.filterwarnings("ignore", message=".*OpenSSL.*LibreSSL.*")
warnings.filterwarnings("ignore", message=".*urllib3.*only supports OpenSSL.*")

from flask import Flask, jsonify, send_from_directory

def _find_project_root() -> Path:
    """Корень проекта: каталог, в котором есть папка data/."""
    start = Path(__file__).resolve().parent
    candidate = start
    for _ in range(5):
        if (candidate / "data").is_dir():
            return candidate
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    return start.parent

ROOT = _find_project_root()
sys.path.insert(0, str(ROOT))

# Загрузить .env из корня проекта (GFW_API_TOKEN и т.д.)
_env = ROOT / ".env"
if _env.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_env)
    except ImportError:
        pass

DATA = ROOT / "data"
OUTPUT = ROOT / "output"
CACHE_FILE = DATA / "gfw_our_vessels.json"
ENRICHED_FILE = DATA / "gfw_enriched_vessels.json"
CERBERUS_CSV = DATA / "cerberus_export.csv"
QUOTA_CSV = OUTPUT / "quota_summary.csv"
COMPANY_GROUPS_CSV = DATA / "company_groups.csv"
COMPANY_GROUPS_ENRICHED_CSV = DATA / "company_groups_enriched.csv"

app = Flask(__name__, static_folder="static", static_url_path="")


def _shorten_cerberus_kind(kind: str) -> str:
    """Сокращение Вид_объекта для отображения (тип судна)."""
    if not kind:
        return ""
    k = (kind or "").strip()
    if "суда" in k.lower() and "добыч" in k.lower():
        return "Судно (добыча/переработка)"
    if "суда" in k.lower():
        return "Судно"
    return k[:50] + ("…" if len(k) > 50 else "")


def _shorten_basin(basin: str) -> str:
    """Сокращение длинных названий бассейнов/районов промысла для отображения."""
    b = (basin or "").strip()
    if not b:
        return ""
    if "Норвегии" in b or "Норвегия" in b:
        return "Район РФ–Норвегия (Баренцево море)"
    if "Соглашения" in b and len(b) > 60:
        return b[:55] + "…"
    return b[:70] + ("…" if len(b) > 70 else "")


def _read_csv_safe(path: Path) -> list[dict]:
    """Читает CSV с поддержкой BOM (utf-8-sig). При ошибке возвращает []."""
    import csv
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _load_quota_basins_by_inn() -> dict[str, list[str]]:
    """ИНН -> список уникальных бассейнов/районов промысла из quota_summary (квоты)."""
    by_inn: dict[str, set[str]] = {}
    for row in _read_csv_safe(QUOTA_CSV):
        inn = (row.get("ИНН") or "").strip()
        basin = (row.get("Бассейн") or "").strip()
        if inn and basin:
            by_inn.setdefault(inn, set()).add(basin)
    return {inn: sorted(basins) for inn, basins in by_inn.items()}


def _load_excluded_inns() -> set[str]:
    """Множество ИНН, помеченных маркером «Исключить» в enriched CSV."""
    excluded: set[str] = set()
    for row in _read_csv_safe(COMPANY_GROUPS_ENRICHED_CSV):
        inn = (row.get("ИНН") or "").strip()
        if inn and (row.get("Исключить") or "").strip():
            excluded.add(inn)
    return excluded


def _load_company_groups_by_inn() -> dict[str, list[str]]:
    """ИНН -> список названий групп компаний (Группа_Компаний) из company_groups.csv."""
    by_inn: dict[str, list[str]] = {}
    for row in _read_csv_safe(COMPANY_GROUPS_CSV):
        inn = (row.get("ИНН") or "").strip()
        group = (row.get("Группа_Компаний") or "").strip()
        if inn and group:
            if inn not in by_inn:
                by_inn[inn] = []
            if group not in by_inn[inn]:
                by_inn[inn].append(group)
    return by_inn


def _load_cerberus_kind_map() -> dict[tuple[str, str], str]:
    """(ИНН, Название_объекта) -> Вид_объекта (сокращённый). Регион регистрации не используем."""
    kind_map: dict[tuple[str, str], str] = {}
    for row in _read_csv_safe(CERBERUS_CSV):
        inn = (row.get("ИНН") or "").strip()
        name = (row.get("Название_объекта") or "").strip()
        kind = (row.get("Вид_объекта") or "").strip()
        if inn and name and kind:
            kind_map[(inn, name)] = _shorten_cerberus_kind(kind)
    return kind_map


def _load_vessels_from_cerberus_fallback() -> list[dict]:
    """Запасной источник: суда из cerberus_export.csv (Судно=1), если JSON-кэши пусты."""
    seen: set[tuple[str, str]] = set()
    vessels = []
    for row in _read_csv_safe(CERBERUS_CSV):
        if (row.get("Судно") or "").strip() != "1":
            continue
        inn = (row.get("ИНН") or "").strip()
        name = (row.get("Название_объекта") or "").strip()
        company = (row.get("Хоз_субъект") or "").strip()
        if not inn or not name:
            continue
        key = (inn, name)
        if key in seen:
            continue
        seen.add(key)
        vessels.append({
            "name": name,
            "inn": inn,
            "company": company or name,
            "gfw_id": None,
            "source": "cerberus",
        })
    return vessels


def _shorten_company_name(s: str) -> str:
    """Сокращение организационно-правовой формы: ООО, АО, ПАО, ЗАО, ИП и т.д."""
    if s is None:
        return ""
    s = str(s).strip()
    if not s:
        return ""
    import re
    t = s.strip()
    # Порядок важен: более длинные формы первыми
    replacements = [
        (r"\bПУБЛИЧНОЕ\s+АКЦИОНЕРНОЕ\s+ОБЩЕСТВО\b", "ПАО", re.IGNORECASE),
        (r"\bЗАКРЫТОЕ\s+АКЦИОНЕРНОЕ\s+ОБЩЕСТВО\b", "ЗАО", re.IGNORECASE),
        (r"\bАКЦИОНЕРНОЕ\s+ОБЩЕСТВО\b", "АО", re.IGNORECASE),
        (r"\bОБЩЕСТВО\s+С\s+ОГРАНИЧЕННОЙ\s+ОТВЕТСТВЕННОСТЬЮ\b", "ООО", re.IGNORECASE),
        (r"\bИНДИВИДУАЛЬНЫЙ\s+ПРЕДПРИНИМАТЕЛЬ\b", "ИП", re.IGNORECASE),
        (r"\bКРЕСТЬЯНСКОЕ\s+\(ФЕРМЕРСКОЕ\)\s+ХОЗЯЙСТВО\b", "КФХ", re.IGNORECASE),
        (r"\bКРЕСТЬЯНСКОЕ\s+ХОЗЯЙСТВО\b", "КФХ", re.IGNORECASE),
        (r"\bАВТОНОМНАЯ\s+НЕКОММЕРЧЕСКАЯ\s+ОРГАНИЗАЦИЯ\b", "АНО", re.IGNORECASE),
        (r"\bНЕКОММЕРЧЕСКАЯ\s+ОРГАНИЗАЦИЯ\b", "НКО", re.IGNORECASE),
        (r"\bПРОИЗВОДСТВЕННЫЙ\s+КООПЕРАТИВ\b", "ПК", re.IGNORECASE),
        (r"\bСЕЛЬСКОХОЗЯЙСТВЕННЫЙ\s+ПРОИЗВОДСТВЕННЫЙ\s+КООПЕРАТИВ\b", "СПК", re.IGNORECASE),
        (r"\bГОСУДАРСТВЕННОЕ\s+УНИТАРНОЕ\s+ПРЕДПРИЯТИЕ\b", "ГУП", re.IGNORECASE),
        (r"\bОТКРЫТОЕ\s+АКЦИОНЕРНОЕ\s+ОБЩЕСТВО\b", "ОАО", re.IGNORECASE),
    ]
    for pattern, repl, flags in replacements:
        t = re.sub(pattern, repl, t, flags=flags)
    return t.strip()


def _display_name(v: dict) -> str:
    """Краткое читаемое название судна (не проект FleetPhoto)."""
    name = (v.get("name") or "").strip()
    gfw_name = (v.get("gfw_name") or "").strip()
    if not name:
        return gfw_name or "—"
    # Не показывать как название судна строку, похожую на проект FleetPhoto (например "1328, тип Балтика")
    import re
    if re.search(r"\d{3,5}\s*,\s*тип\s+", name, re.I) or re.search(r"проект\s+\d+", name, re.I):
        return gfw_name or name or "—"
    # Вытащить часть в кавычках: СКТР "Стелла Карина" -> Стелла Карина
    m = re.search(r'"([^"]+)"', name)
    if m:
        quoted = m.group(1).strip()
        prefix = name.split('"')[0].strip()
        if prefix and len(prefix) <= 12:
            return f"{prefix} «{quoted}»"
        return quoted
    # Короткие префиксы типа СКТР, СРТМ — оставить как есть
    if len(name) <= 45:
        return name
    # Длинное (часто название компании) — обрезать или gfw_name
    if gfw_name:
        return f"{name[:30]}… ({gfw_name})"
    return name[:45] + "…"


def _enrich_vessel(v: dict, basins_by_inn: dict, kind_map: dict, groups_by_inn: dict) -> None:
    """Дополняет одну запись судна: region, vessel_type, company_groups, display_name, company_short."""
    inn = (v.get("inn") or "").strip()
    name = (v.get("name") or "").strip()
    basin_list = basins_by_inn.get(inn, [])
    v["region"] = "; ".join(_shorten_basin(b) for b in basin_list) if basin_list else ""
    v["vessel_type"] = (v.get("fleetphoto_project") or "").strip() or kind_map.get((inn, name)) or ""
    v["company_groups"] = groups_by_inn.get(inn, [])
    v["company_group"] = "; ".join(v["company_groups"]) if v["company_groups"] else ""
    v["display_name"] = _display_name(v)
    v["company_short"] = _shorten_company_name(v.get("company") or "")


def load_our_vessels_cache() -> list[dict]:
    """Объединённый список: суда из кэша JSON или из Цербера (запасной источник). Район = бассейны промысла из квот."""
    basins_by_inn = _load_quota_basins_by_inn()
    kind_map = _load_cerberus_kind_map()
    groups_by_inn = _load_company_groups_by_inn()
    excluded_inns = _load_excluded_inns()
    vessels = []
    for path, source in [(CACHE_FILE, "cerberus"), (ENRICHED_FILE, "gfw_enrichment")]:
        if not path.exists():
            continue
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except Exception:
            continue
        if not isinstance(data, list):
            data = [data] if isinstance(data, dict) else []
        for v in data:
            if not isinstance(v, dict):
                continue
            inn = (v.get("inn") or "").strip()
            if inn in excluded_inns:
                continue
            v = dict(v)
            v.setdefault("source", source)
            _enrich_vessel(v, basins_by_inn, kind_map, groups_by_inn)
            vessels.append(v)
    if not vessels:
        for v in _load_vessels_from_cerberus_fallback():
            v = dict(v)
            inn = (v.get("inn") or "").strip()
            if inn in excluded_inns:
                continue
            _enrich_vessel(v, basins_by_inn, kind_map, groups_by_inn)
            vessels.append(v)
    return vessels


def get_positions_for_vessels(gfw_ids: list[str]) -> dict[str, tuple[float, float]]:
    if not gfw_ids:
        return {}
    try:
        from scripts.gfw_client import events_get_latest_positions

        return events_get_latest_positions(gfw_ids, days_back=30)
    except Exception:
        return {}


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/favicon.ico")
def favicon():
    """Убрать 404 из лога при запросе иконки вкладки."""
    return "", 204


@app.route("/api/debug")
def api_debug():
    """Диагностика: пути, файлы, число судов и позиций GFW."""
    vessels = load_our_vessels_cache()
    gfw_ids = [v["gfw_id"] for v in vessels if v.get("gfw_id")]
    positions_count = 0
    if gfw_ids and os.environ.get("GFW_API_TOKEN"):
        try:
            pos = get_positions_for_vessels(gfw_ids)
            positions_count = len(pos)
        except Exception as e:
            positions_count = str(e)
    return jsonify({
        "ROOT": str(ROOT),
        "DATA": str(DATA),
        "CACHE_FILE_exists": CACHE_FILE.exists(),
        "CERBERUS_CSV_exists": CERBERUS_CSV.exists(),
        "vessel_count": len(vessels),
        "vessels_with_gfw_id": len(gfw_ids),
        "positions_from_gfw": positions_count,
    })


@app.route("/api/vessels")
def api_vessels():
    """Список наших судов (из кэша Цербер+GFW)."""
    vessels = load_our_vessels_cache()
    r = jsonify({"vessels": vessels, "total": len(vessels)})
    r.headers["Cache-Control"] = "no-store"
    return r


@app.route("/api/vessels/geojson")
def api_vessels_geojson():
    """GeoJSON точек для отображения на карте (только суда с известной позицией)."""
    vessels = load_our_vessels_cache()
    gfw_ids = [v["gfw_id"] for v in vessels if v.get("gfw_id")]
    positions = get_positions_for_vessels(gfw_ids) if os.environ.get("GFW_API_TOKEN") else {}

    features = []
    for v in vessels:
        gfw_id = v.get("gfw_id")
        if not gfw_id or gfw_id not in positions:
            continue
        lat, lon = positions[gfw_id]
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "name": v.get("name"),
                "display_name": v.get("display_name") or v.get("name"),
                "company": v.get("company"),
                "company_short": v.get("company_short") or v.get("company"),
                "company_group": v.get("company_group") or "",
                "company_groups": v.get("company_groups") or [],
                "inn": v.get("inn"),
                "gfw_id": gfw_id,
                "source": v.get("source", "cerberus"),
                "vessel_type": v.get("vessel_type") or "",
                "region": v.get("region") or "",
                "fleetphoto_photo_url": v.get("fleetphoto_photo_url") or "",
                "gfw_fishing_events_90d": v.get("gfw_fishing_events_90d"),
                "gfw_last_fishing_date": v.get("gfw_last_fishing_date") or "",
                "gfw_port_visits_90d": v.get("gfw_port_visits_90d"),
                "gfw_last_port_visit": v.get("gfw_last_port_visit") or "",
                "gfw_encounters_90d": v.get("gfw_encounters_90d"),
                "gfw_last_encounter_date": v.get("gfw_last_encounter_date") or "",
            },
        })

    return jsonify({
        "type": "FeatureCollection",
        "features": features,
    })


def _load_quota_by_inn() -> dict[str, list[dict]]:
    """ИНН -> список строк квоты (Год, Объект_Лова, Тип_Квоты, Объем_Тонн и т.д.)."""
    by_inn: dict[str, list[dict]] = {}
    if not QUOTA_CSV.exists():
        return by_inn
    import csv
    with open(QUOTA_CSV, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            inn = (row.get("ИНН") or "").strip()
            if not inn:
                continue
            rec = {
                "year": (row.get("Год") or "").strip(),
                "basin": (row.get("Бассейн") or "").strip(),
                "object": (row.get("Объект_Лова") or "").strip(),
                "quota_type": (row.get("Тип_Квоты") or "").strip(),
                "volume_ton": (row.get("Объем_Тонн") or "").strip(),
                "share_pct": (row.get("Доля_%") or "").strip(),
            }
            by_inn.setdefault(inn, []).append(rec)
    return by_inn


_quota_by_inn_cache: dict[str, list[dict]] | None = None


@app.route("/api/quota/<inn>")
def api_quota(inn: str):
    """Квоты по ИНН судовладельца (для карточки судна при наведении)."""
    global _quota_by_inn_cache
    if _quota_by_inn_cache is None:
        _quota_by_inn_cache = _load_quota_by_inn()
    inn = (inn or "").strip()
    rows = _quota_by_inn_cache.get(inn, [])
    # Сводка по годам: год -> сумма объёма (если число) по объекту
    summary_by_year: dict[str, float] = {}
    for r in rows:
        y = r.get("year") or ""
        vol = r.get("volume_ton") or ""
        try:
            v = float(vol.replace(",", "."))
        except ValueError:
            v = 0
        if y:
            summary_by_year[y] = summary_by_year.get(y, 0) + v
    summary_list = [{"year": y, "volume_ton": round(s, 1)} for y, s in sorted(summary_by_year.items())]
    return jsonify({
        "inn": inn,
        "rows": rows[:50],
        "summary_by_year": summary_list,
        "total_rows": len(rows),
    })


def main():
    port = int(os.environ.get("PORT", "5000"))
    print(f"Карта наших судов: http://localhost:{port}")
    print(f"  DATA: {DATA}")
    print(f"  gfw_our_vessels.json: {'есть' if CACHE_FILE.exists() else 'НЕТ'}")
    print(f"  cerberus_export.csv: {'есть' if CERBERUS_CSV.exists() else 'НЕТ'}")
    n = len(load_our_vessels_cache())
    print(f"  Загружено судов: {n}")
    if n == 0:
        print("  ВНИМАНИЕ: список пуст. Проверьте пути выше или откройте http://localhost:{}/api/debug".format(port))
    if os.environ.get("GFW_API_TOKEN"):
        print("Токен GFW задан — позиции подгружаются с API.")
    else:
        print("GFW_API_TOKEN не задан — на карте только список судов из кэша.")
    app.run(host="0.0.0.0", port=port, debug=True)


if __name__ == "__main__":
    main()
