"""
Связка парсинга конкурента с реестрами проекта (группы, квоты, Цербер, БД, Notion).
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DATA = ROOT / "data"
OUTPUT = ROOT / "output"
SNAPSHOTS_DIR = OUTPUT / "competitor_snapshots"

COMPANY_GROUPS = DATA / "company_groups.csv"
COMPANY_GROUPS_ENRICHED = DATA / "company_groups_enriched.csv"
CERBERUS_EXPORT = DATA / "cerberus_export.csv"
QUOTA_SUMMARY = OUTPUT / "quota_summary.csv"
VESSELS_CSV = ROOT / "notion_import" / "vessels.csv"
GFW_VESSELS_JSON = DATA / "gfw_our_vessels.json"


def _normalize_company_key(name: str) -> str:
    s = (name or "").lower()
    s = re.sub(r"[«»\"'„”“]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    for prefix in ("ао ", "ооо ", "оао ", "зао ", "пао ", "акционерное общество ", "публичное акционерное общество "):
        if s.startswith(prefix):
            s = s[len(prefix) :]
    return s.strip()


COMPANY_STOPWORDS = frozenset(
    {
        "компания",
        "общество",
        "группа",
        "холдинг",
        "акционерное",
        "публичное",
        "ограниченной",
        "ответственностью",
        "закрытое",
        "открытое",
        "ао",
        "ооо",
        "пао",
        "зао",
        "оао",
        "гк",
        "company",
        "group",
        "holding",
        "ltd",
        "llc",
        "inc",
    }
)

# Нормализованный алиас -> группа_компаний
COMPANY_GROUP_ALIASES: dict[str, str] = {
    "ррпк": "ГК РРПК",
    "русская рыбопромышленная компания": "ГК РРПК",
    "russian fishery company": "ГК РРПК",
    "rfc": "ГК РРПК",
    "okeanrybflot": "ГК Океанрыбфлот",
    "океанрыбфлот": "ГК Океанрыбфлот",
}

URL_GROUP_HINTS: dict[str, str] = {
    "russianfishery.ru": "ГК РРПК",
    "www.russianfishery.ru": "ГК РРПК",
    "okeanrybflot.ru": "ГК Океанрыбфлот",
    "www.okeanrybflot.ru": "ГК Океанрыбфлот",
}

GROUP_PRIMARY_INNS: dict[str, list[str]] = {
    "ГК РРПК": ["7731414433", "2521015391", "2537008664", "2536306555"],
    "ГК Океанрыбфлот": ["4100000530"],
}


def _significant_tokens(key: str) -> list[str]:
    return [token for token in key.split() if len(token) >= 4 and token not in COMPANY_STOPWORDS]


def _score_company_match(query_key: str, candidate_key: str) -> int:
    if not query_key or not candidate_key:
        return 0
    if query_key == candidate_key:
        return 100
    if query_key in candidate_key or candidate_key in query_key:
        return 80

    query_tokens = _significant_tokens(query_key)
    if not query_tokens:
        return 0

    matched = sum(1 for token in query_tokens if token in candidate_key)
    if matched >= 3:
        return 95
    if matched >= 2:
        return 85
    if matched == 1 and len(query_tokens) == 1:
        return 70
    return 0


def _company_match_result(row: dict[str, Any], score: int, source: str) -> dict[str, Any]:
    return {
        "inn": row["inn"],
        "legal_name": row["legal_name"],
        "group_name": row["group_name"],
        "match_score": score,
        "match_source": source,
    }


def _resolve_by_group(group_name: str, *, source: str, score: int = 100) -> dict[str, Any] | None:
    index = _load_company_index()
    candidates = [row for row in index if row.get("group_name") == group_name]
    if not candidates:
        return None

    for inn in GROUP_PRIMARY_INNS.get(group_name, []):
        for row in candidates:
            if row["inn"] == inn:
                return _company_match_result(row, score, source)

    for row in candidates:
        legal_key = row.get("legal_key") or ""
        if any(token in legal_key for token in ("рыбопромышленная", "ррpk", "океанрыбфлот")):
            return _company_match_result(row, score, source)

    return _company_match_result(candidates[0], score, source)


def _resolve_from_url(source_url: str | None) -> dict[str, Any] | None:
    if not source_url:
        return None
    match = re.search(r"https?://([^/]+)", source_url.strip(), flags=re.I)
    if not match:
        return None
    domain = match.group(1).lower()
    if domain.startswith("www."):
        domain = domain[4:]
    group_name = URL_GROUP_HINTS.get(domain) or URL_GROUP_HINTS.get(f"www.{domain}")
    if not group_name:
        return None
    return _resolve_by_group(group_name, source=f"url_hint:{domain}")


def _resolve_from_alias(key: str) -> dict[str, Any] | None:
    group_name = COMPANY_GROUP_ALIASES.get(key)
    if not group_name:
        for alias, alias_group in COMPANY_GROUP_ALIASES.items():
            if alias in key or key in alias:
                group_name = alias_group
                break
    if not group_name:
        return None
    return _resolve_by_group(group_name, source="company_alias")


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    for encoding in ("utf-8", "utf-8-sig", "cp1251"):
        try:
            with path.open(encoding=encoding, newline="") as fh:
                return list(csv.DictReader(fh))
        except UnicodeDecodeError:
            continue
    return []


@lru_cache(maxsize=1)
def _load_company_index() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen_inns: set[str] = set()

    for source_file, rows_src in (
        (COMPANY_GROUPS_ENRICHED, _read_csv(COMPANY_GROUPS_ENRICHED)),
        (COMPANY_GROUPS, _read_csv(COMPANY_GROUPS)),
    ):
        for row in rows_src:
            inn = re.sub(r"\D", "", row.get("ИНН", "") or "")
            if len(inn) not in (10, 12):
                continue
            legal = (row.get("Юр_Лицо") or row.get("name") or "").strip()
            group = (row.get("Группа_Компаний") or row.get("group_companies") or "").strip()
            if inn in seen_inns and not legal:
                continue
            seen_inns.add(inn)
            rows.append(
                {
                    "inn": inn,
                    "legal_name": legal,
                    "group_name": group,
                    "legal_key": _normalize_company_key(legal),
                    "group_key": _normalize_company_key(group),
                    "source": source_file.name,
                }
            )
    return rows


def resolve_company(
    competitor_name: str | None,
    *,
    source_url: str | None = None,
) -> dict[str, Any] | None:
    """Сопоставить название конкурента с ИНН и группой из company_groups*.csv."""
    url_match = _resolve_from_url(source_url)
    if url_match:
        return url_match

    if not competitor_name or not competitor_name.strip():
        return None

    key = _normalize_company_key(competitor_name)
    if not key:
        return None

    alias_match = _resolve_from_alias(key)
    if alias_match:
        return alias_match

    index = _load_company_index()
    best: dict[str, Any] | None = None
    best_score = 0

    for row in index:
        legal_score = _score_company_match(key, row.get("legal_key") or "")
        group_score = _score_company_match(key, row.get("group_key") or "")
        score = max(legal_score, group_score)
        if score > best_score:
            best_score = score
            best = row

    if best and best_score >= 70:
        return _company_match_result(best, best_score, best["source"])
    return None


def get_group_companies(group_name: str) -> list[dict[str, str]]:
    if not group_name:
        return []
    group_key = _normalize_company_key(group_name)
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in _load_company_index():
        if row["group_key"] == group_key and row["inn"] not in seen:
            seen.add(row["inn"])
            result.append(
                {
                    "inn": row["inn"],
                    "legal_name": row["legal_name"],
                    "group_name": row["group_name"],
                }
            )
    return result


def get_quotas_for_inn(inn: str, *, latest_year_only: bool = True) -> list[dict[str, Any]]:
    rows = _read_csv(QUOTA_SUMMARY)
    matched = [r for r in rows if re.sub(r"\D", "", r.get("ИНН", "") or "") == inn]
    if not matched:
        return []

    if latest_year_only:
        years = [int(r["Год"]) for r in matched if str(r.get("Год", "")).isdigit()]
        if years:
            max_year = max(years)
            matched = [r for r in matched if str(r.get("Год")) == str(max_year)]

    quotas: list[dict[str, Any]] = []
    for row in matched:
        try:
            tons = float(str(row.get("Объем_Тонн", "") or "0").replace(",", "."))
        except ValueError:
            tons = 0.0
        quotas.append(
            {
                "year": row.get("Год"),
                "basin": row.get("Бассейн"),
                "species": row.get("Объект_Лова"),
                "quota_type": row.get("Тип_Квоты"),
                "share_pct": row.get("Доля_%"),
                "volume_tons": tons,
                "legal_name": row.get("Юр_Лицо"),
            }
        )
    return quotas


def get_cerber_vessels(inn: str) -> list[dict[str, str]]:
    return get_cerber_vessels_for_inns([inn])


def get_cerber_vessels_for_inns(inns: list[str]) -> list[dict[str, str]]:
    inn_set = {re.sub(r"\D", "", inn) for inn in inns if inn}
    rows = _read_csv(CERBERUS_EXPORT)
    vessels: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        row_inn = re.sub(r"\D", "", row.get("ИНН", "") or "")
        if row_inn not in inn_set:
            continue
        if row.get("Судно") != "1":
            continue
        raw_name = (row.get("Название_объекта") or "").strip()
        if not raw_name or not _is_vessel_cerber_record(raw_name):
            continue
        key = (row_inn, raw_name)
        if key in seen:
            continue
        seen.add(key)
        vessels.append(
            {
                "raw_name": raw_name,
                "owner_inn": row_inn,
                "region": row.get("Регион", ""),
                "export_countries": row.get("Страна", ""),
                "status": row.get("Статус", ""),
                "owner": row.get("Хоз_субъект", ""),
            }
        )
    return vessels


def get_registry_vessels_for_inns(inns: list[str]) -> list[dict[str, Any]]:
    inn_set = {re.sub(r"\D", "", inn) for inn in inns if inn}
    rows = _read_csv(VESSELS_CSV)
    vessels: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        owner_inn = re.sub(r"\D", "", row.get("Судовладелец_ИНН", "") or "")
        if owner_inn not in inn_set:
            continue
        name = (row.get("Название_судна") or "").strip()
        if not name:
            continue
        from vesselservice import normalize_vessel_name

        norm = normalize_vessel_name(name)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        imo_raw = row.get("IMO")
        imo: int | None = None
        if imo_raw not in (None, ""):
            try:
                imo = int(str(imo_raw).strip())
            except ValueError:
                imo = None
        vessels.append(
            {
                "name": name,
                "imo": imo,
                "imo_source": "vessels_csv" if imo else None,
                "owner_inn": owner_inn,
                "owner_name": row.get("Судовладелец", ""),
                "vessel_type": row.get("Тип_Модель", ""),
                "board_number": row.get("Бортовой_номер", ""),
                "operational_status": row.get("Состояние", ""),
                "work_region": row.get("Регион_работы", ""),
                "gfw_id": row.get("GFW_ID", ""),
                "gfw_name": row.get("GFW_Name", ""),
                "source": "group_registry",
            }
        )
    return vessels


def get_gfw_vessels_for_inns(inns: list[str]) -> list[dict[str, Any]]:
    inn_set = {re.sub(r"\D", "", inn) for inn in inns if inn}
    if not GFW_VESSELS_JSON.exists():
        return []
    items = json.loads(GFW_VESSELS_JSON.read_text(encoding="utf-8"))
    vessels: list[dict[str, Any]] = []
    seen: set[str] = set()
    from vesselservice import normalize_vessel_name, _extract_vessel_name

    for item in items:
        if not isinstance(item, dict):
            continue
        owner_inn = re.sub(r"\D", "", str(item.get("inn", "") or ""))
        if owner_inn not in inn_set:
            continue
        name = _extract_vessel_name(str(item.get("name", "")))
        norm = normalize_vessel_name(name)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        imo_raw = item.get("imo")
        imo: int | None = None
        if imo_raw not in (None, ""):
            try:
                imo = int(str(imo_raw).strip())
            except ValueError:
                imo = None
        vessels.append(
            {
                "name": name,
                "imo": imo,
                "imo_source": "gfw_cache" if imo else None,
                "owner_inn": owner_inn,
                "owner_name": item.get("company", ""),
                "gfw_id": item.get("gfw_id", ""),
                "gfw_name": item.get("gfw_name", ""),
                "source": "gfw_cache",
            }
        )
    return vessels


def build_group_fleet(
    group_inns: list[str],
    *,
    dispatcher_vessels: list[dict[str, Any]],
    lookup_owner_inn: str | None = None,
) -> list[dict[str, Any]]:
    """Собрать полный флот группы компаний и наложить данные диспетчерской."""
    from vesselservice import lookup_vessel_imo, normalize_vessel_name

    merged: dict[str, dict[str, Any]] = {}

    def upsert(name: str, **fields: Any) -> None:
        norm = normalize_vessel_name(name)
        if not norm:
            return
        row = merged.setdefault(norm, {"name": _extract_vessel_name_simple(name)})
        for key, value in fields.items():
            if value is None or value == "":
                continue
            if key == "imo" and row.get("imo"):
                continue
            if key == "sources":
                existing = set(row.get("sources") or [])
                existing.update(value if isinstance(value, list) else [value])
                row["sources"] = sorted(existing)
                continue
            row[key] = value

    for vessel in get_registry_vessels_for_inns(group_inns):
        upsert(
            vessel["name"],
            imo=vessel.get("imo"),
            imo_source=vessel.get("imo_source"),
            owner_inn=vessel.get("owner_inn"),
            owner_name=vessel.get("owner_name"),
            vessel_type=vessel.get("vessel_type"),
            board_number=vessel.get("board_number"),
            operational_status=vessel.get("operational_status"),
            work_region=vessel.get("work_region"),
            gfw_id=vessel.get("gfw_id"),
            gfw_name=vessel.get("gfw_name"),
            sources=["group_registry"],
        )

    for vessel in get_gfw_vessels_for_inns(group_inns):
        upsert(
            vessel["name"],
            imo=vessel.get("imo"),
            imo_source=vessel.get("imo_source"),
            owner_inn=vessel.get("owner_inn"),
            owner_name=vessel.get("owner_name"),
            gfw_id=vessel.get("gfw_id"),
            gfw_name=vessel.get("gfw_name"),
            sources=["gfw_cache"],
        )

    for cerber in get_cerber_vessels_for_inns(group_inns):
        quoted = re.search(r'"([^"]+)"', cerber.get("raw_name", ""))
        name = quoted.group(1) if quoted else cerber["raw_name"]
        upsert(
            name,
            owner_inn=cerber.get("owner_inn"),
            cerber_status=cerber.get("status"),
            cerber_region=cerber.get("region"),
            cerber_export=cerber.get("export_countries"),
            sources=["cerberus"],
        )

    for vessel in dispatcher_vessels:
        name = str(vessel.get("name") or "").strip()
        if not name:
            continue
        upsert(
            name,
            imo=vessel.get("imo"),
            imo_source=vessel.get("imo_source"),
            status=vessel.get("status"),
            location=vessel.get("location"),
            end_date=vessel.get("end_date"),
            on_dispatcher=True,
            sources=["site_dispatcher"],
        )

    owner_inn = lookup_owner_inn or (group_inns[0] if group_inns else None)
    for row in merged.values():
        if not row.get("imo"):
            match = lookup_vessel_imo(row["name"], use_gfw=True, owner_inn=owner_inn)
            if match:
                row["imo"] = match.imo
                row["imo_source"] = match.source
        row.setdefault("on_dispatcher", False)
        row.setdefault("sources", [])

    fleet = list(merged.values())
    fleet.sort(key=lambda v: (not v.get("on_dispatcher"), v.get("name", "")))
    return fleet


def _merge_vessel_records(
    parsed_vessels: list[dict[str, Any]],
    cerber_vessels: list[dict[str, str]],
    *,
    owner_inn: str | None = None,
) -> list[dict[str, Any]]:
    from vesselservice import enrich_analysis_with_imo, lookup_vessel_imo, normalize_vessel_name

    # Повторное сопоставление IMO с учётом ИНН владельца
    if parsed_vessels:
        re_enriched = enrich_analysis_with_imo({"vessels": parsed_vessels}, owner_inn=owner_inn)
        parsed_vessels = re_enriched.get("vessels") or parsed_vessels

    merged: dict[str, dict[str, Any]] = {}

    def upsert(name: str, **fields: Any) -> None:
        norm = normalize_vessel_name(name)
        if not norm:
            return
        row = merged.setdefault(norm, {"name": _extract_vessel_name_simple(name)})
        for key, value in fields.items():
            if value is None:
                continue
            if key == "imo" and row.get("imo"):
                continue
            row[key] = value

    for vessel in parsed_vessels:
        name = str(vessel.get("name") or "").strip()
        if not name:
            continue
        upsert(
            name,
            imo=vessel.get("imo"),
            imo_source=vessel.get("imo_source"),
            status=vessel.get("status"),
            location=vessel.get("location"),
            end_date=vessel.get("end_date"),
            source="site_dispatcher",
        )

    for cerber in cerber_vessels:
        raw = cerber.get("raw_name", "")
        if not _is_vessel_cerber_record(raw):
            continue
        quoted = re.search(r'"([^"]+)"', raw)
        name = quoted.group(1) if quoted else raw
        match = lookup_vessel_imo(name, use_gfw=True, owner_inn=owner_inn)
        upsert(
            name,
            imo=match.imo if match else None,
            imo_source=match.source if match else None,
            cerber_status=cerber.get("status"),
            cerber_region=cerber.get("region"),
            cerber_export=cerber.get("export_countries"),
            source="cerberus",
        )

    for row in merged.values():
        if not row.get("imo"):
            match = lookup_vessel_imo(row["name"], use_gfw=True, owner_inn=owner_inn)
            if match:
                row["imo"] = match.imo
                row["imo_source"] = match.source

    return list(merged.values())


def _extract_vessel_name_simple(full_name: str) -> str:
    quoted = re.search(r'"([^"]+)"', full_name)
    if quoted:
        return quoted.group(1).strip()
    return full_name.strip()


def _is_vessel_cerber_record(raw_name: str) -> bool:
    lower = (raw_name or "").lower()
    if not any(x in lower for x in ("судно", "бмрт", "батм", "траул", "сртм", "стр ", "рт ")):
        return False
    if any(x in lower for x in ("акционерное общество", "общество с ограниченной", "ооо ", "ао ")):
        if "судно" not in lower:
            return False
    return bool(re.search(r'"[^"]+"', raw_name))


def upsert_vessels_to_db(vessels: list[dict[str, Any]], owner_inn: str | None) -> dict[str, int]:
    """Записать суда с IMO в PostgreSQL (таблица vessels)."""
    stats = {"upserted": 0, "skipped_no_imo": 0, "errors": 0}
    if not owner_inn:
        return stats

    try:
        from sqlalchemy.dialects.postgresql import insert

        from database.db_session import session_scope
        from database.models import Vessel
    except Exception as exc:
        logger.warning("PostgreSQL недоступен: %s", exc)
        stats["errors"] = 1
        return stats

    rows = []
    for vessel in vessels:
        imo = vessel.get("imo")
        if not imo:
            stats["skipped_no_imo"] += 1
            continue
        try:
            imo_int = int(imo)
        except (TypeError, ValueError):
            stats["skipped_no_imo"] += 1
            continue
        rows.append(
            {
                "imo": imo_int,
                "name": str(vessel.get("name") or f"IMO {imo_int}")[:255],
                "project": None,
                "base_owner_inn": owner_inn,
                "gfw_id": None,
            }
        )

    if not rows:
        return stats

    stmt = insert(Vessel).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Vessel.imo],
        set_={
            "name": stmt.excluded.name,
            "base_owner_inn": stmt.excluded.base_owner_inn,
        },
    )
    try:
        with session_scope() as session:
            session.execute(stmt)
        stats["upserted"] = len(rows)
    except Exception as exc:
        logger.error("Ошибка upsert vessels: %s", exc)
        stats["errors"] = len(rows)
    return stats


def save_snapshot(payload: dict[str, Any]) -> str:
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    inn = payload.get("company", {}).get("inn", "unknown")
    path = SNAPSHOTS_DIR / f"competitor_{inn}_{ts}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def refresh_notion_export(*, push_to_notion: bool = False) -> dict[str, str]:
    """
    Пересобрать notion_import/*.csv из актуальных данных проекта.
    Опционально запустить notion_reimport.py (если NOTION_SYNC=1).
    """
    result: dict[str, str] = {"prepare": "skipped", "notion": "skipped"}
    prepare_script = ROOT / "scripts" / "prepare_notion_import.py"
    if prepare_script.exists():
        proc = subprocess.run(
            [sys.executable, str(prepare_script)],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        result["prepare"] = "ok" if proc.returncode == 0 else f"error: {proc.stderr[:300]}"

    if push_to_notion and os.getenv("NOTION_SYNC", "").strip() in {"1", "true", "yes"}:
        notion_script = ROOT / "scripts" / "notion_reimport.py"
        if notion_script.exists() and os.getenv("NOTION_API_TOKEN"):
            proc = subprocess.run(
                [sys.executable, str(notion_script)],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            result["notion"] = "ok" if proc.returncode == 0 else f"error: {proc.stderr[:300]}"
        else:
            result["notion"] = "skipped_no_token"
    return result


def enrich_competitor_report(
    analysis: dict[str, Any],
    *,
    source_url: str | None = None,
    persist_db: bool = True,
) -> dict[str, Any]:
    """
    Обогатить AI-отчёт данными проекта: группа, квоты, Цербер, linked vessels.
    """
    competitor_name = analysis.get("competitor_name")
    company = resolve_company(
        str(competitor_name) if competitor_name else "",
        source_url=source_url,
    )

    company_block: dict[str, Any] | None = None
    group_companies: list[dict[str, str]] = []
    quotas: list[dict[str, Any]] = []
    cerber_vessels: list[dict[str, str]] = []

    if company:
        company_block = dict(company)
        group_companies = get_group_companies(company["group_name"])
        quotas = get_quotas_for_inn(company["inn"])
        cerber_vessels = get_cerber_vessels(company["inn"])

    owner_inn = company["inn"] if company else None
    group_inns = [c["inn"] for c in group_companies if c.get("inn")]
    if owner_inn and owner_inn not in group_inns:
        group_inns.insert(0, owner_inn)

    from vesselservice import enrich_analysis_with_imo

    analysis = enrich_analysis_with_imo(analysis, owner_inn=owner_inn)
    parsed_vessels = analysis.get("vessels") or []
    dispatcher_vessels = _merge_vessel_records(parsed_vessels, cerber_vessels, owner_inn=owner_inn)

    group_fleet: list[dict[str, Any]] = []
    if group_inns:
        group_fleet = build_group_fleet(
            group_inns,
            dispatcher_vessels=dispatcher_vessels,
            lookup_owner_inn=owner_inn,
        )

    linked_vessels = group_fleet if group_fleet else dispatcher_vessels
    vessels_on_dispatcher = [v for v in linked_vessels if v.get("on_dispatcher")]

    quota_total_tons = round(sum(q.get("volume_tons") or 0 for q in quotas), 2)

    db_stats: dict[str, int] = {"upserted": 0, "skipped_no_imo": 0, "errors": 0}
    if persist_db and company:
        db_stats = upsert_vessels_to_db(linked_vessels, company["inn"])

    enriched = {
        "source_url": source_url,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "company": company_block,
        "group_companies": group_companies,
        "quotas_rosrybolovstvo": quotas,
        "quota_total_tons_latest_year": quota_total_tons if quotas else None,
        "cerber_vessels_count": len(get_cerber_vessels_for_inns(group_inns) if group_inns else cerber_vessels),
        "group_inns": group_inns,
        "group_fleet_count": len(linked_vessels),
        "vessels_on_dispatcher_count": len(vessels_on_dispatcher),
        "vessels": linked_vessels,
        "vessels_on_dispatcher": vessels_on_dispatcher,
        "vessels_with_imo": sum(1 for v in linked_vessels if v.get("imo")),
        "vessels_without_imo": [v["name"] for v in linked_vessels if not v.get("imo")],
        "analysis": analysis,
        "db_sync": db_stats,
        "data_sources": {
            "company_groups": str(COMPANY_GROUPS),
            "company_groups_enriched": str(COMPANY_GROUPS_ENRICHED),
            "quota_summary": str(QUOTA_SUMMARY),
            "cerberus_export": str(CERBERUS_EXPORT),
            "vessels_csv": str(VESSELS_CSV),
            "gfw_vessels_json": str(GFW_VESSELS_JSON),
        },
    }
    enriched["snapshot_path"] = save_snapshot(enriched)
    return enriched


def run_background_notion_pipeline() -> dict[str, str]:
    """Фоновая пересборка CSV для Notion после нового снимка конкурента."""
    return refresh_notion_export(push_to_notion=True)
