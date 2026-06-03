"""
Сопоставление названий судов с IMO по локальным реестрам и GFW API.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_env = ROOT / ".env"
if _env.exists():
    try:
        from dotenv import load_dotenv

        load_dotenv(_env)
    except ImportError:
        pass
VESSELS_CSV = ROOT / "notion_import" / "vessels.csv"
GFW_VESSELS_JSON = ROOT / "data" / "gfw_our_vessels.json"

CONGRESS_ROMAN = {
    "20": "xx",
    "27": "xxvii",
    "25": "xxv",
    "26": "xxvi",
}

CYRILLIC_TO_LATIN_MAP = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "h",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "shch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}


@dataclass(frozen=True)
class VesselMatch:
    name: str
    imo: int
    source: str
    registry_name: str | None = None


@dataclass(frozen=True)
class VesselRef:
    name: str
    imo: int | None = None
    gfw_id: str | None = None
    gfw_name: str | None = None
    owner_inn: str | None = None
    source: str = "registry"


def _extract_vessel_name(full_name: str) -> str:
    quoted = re.search(r'[«""]([^«""]+)[»""]', full_name)
    if quoted:
        return quoted.group(1).strip()
    if "судно" in full_name.lower():
        tail = re.sub(r".*судно\s+", "", full_name, flags=re.I).strip(" \"'«»")
        if tail:
            return tail
    return full_name.strip()


def normalize_vessel_name(name: str) -> str:
    if not name:
        return ""
    s = _extract_vessel_name(name).lower()
    s = re.sub(r"[«»\"'„”“]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(
        r"^(?:бмрт|батм|рт|рс|мк|стр|сктр|мртк|ак)\s*-?\s*\d*\s*",
        "",
        s,
        flags=re.I,
    ).strip()
    congress = re.match(r"^(\d{1,2})\s+(съезд\s+\S+)$", s)
    if congress:
        digits, rest = congress.groups()
        roman = CONGRESS_ROMAN.get(digits)
        if roman:
            s = f"{roman} {rest}"
    return s


def _latin_key(name: str) -> str:
    normalized = normalize_vessel_name(name)
    return "".join(CYRILLIC_TO_LATIN_MAP.get(ch, ch) for ch in normalized)


def _register_alias(registry: dict[str, VesselRef], alias: str, ref: VesselRef) -> None:
    alias_norm = normalize_vessel_name(alias)
    if not alias_norm:
        return
    existing = registry.get(alias_norm)
    if existing is None or (ref.imo and not existing.imo):
        registry[alias_norm] = ref

    latin = _latin_key(alias)
    if latin:
        existing_latin = registry.get(latin)
        if existing_latin is None or (ref.imo and not existing_latin.imo):
            registry[latin] = ref
@lru_cache(maxsize=1)
def load_vessel_registry() -> dict[str, VesselRef]:
    registry: dict[str, VesselRef] = {}

    def add(
        name: str,
        imo_raw: str | int | None,
        source: str,
        gfw_id: str | None = None,
        gfw_name: str | None = None,
        owner_inn: str | None = None,
    ) -> None:
        clean_name = _extract_vessel_name(name)
        norm = normalize_vessel_name(clean_name)
        if not norm:
            return

        imo: int | None = None
        if imo_raw not in (None, ""):
            try:
                imo = int(str(imo_raw).strip())
            except ValueError:
                imo = None
            if imo is not None and imo <= 0:
                imo = None

        ref = VesselRef(
            name=clean_name,
            imo=imo,
            gfw_id=gfw_id,
            gfw_name=(gfw_name or None),
            owner_inn=owner_inn,
            source=source,
        )
        _register_alias(registry, clean_name, ref)
        if gfw_name:
            _register_alias(registry, gfw_name, ref)
            _register_alias(registry, gfw_name.replace(" ", ""), ref)

        short = re.sub(r"^(?:xx|xxvii|xxvi|xxv)\s+", "", norm)
        if short:
            _register_alias(registry, short, ref)

    if VESSELS_CSV.exists():
        with VESSELS_CSV.open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                add(
                    row.get("Название_судна", ""),
                    row.get("IMO"),
                    "vessels_csv",
                    row.get("GFW_ID") or None,
                    row.get("GFW_Name") or None,
                    re.sub(r"\D", "", row.get("Судовладелец_ИНН", "") or "") or None,
                )

    if GFW_VESSELS_JSON.exists():
        items = json.loads(GFW_VESSELS_JSON.read_text(encoding="utf-8"))
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                add(
                    item.get("name", ""),
                    item.get("imo"),
                    "gfw_cache",
                    str(item.get("gfw_id")).strip() if item.get("gfw_id") else None,
                    str(item.get("gfw_name")).strip() if item.get("gfw_name") else None,
                    re.sub(r"\D", "", str(item.get("inn", "") or "")) or None,
                )

    logger.info("Загружен реестр судов: %d записей", len(registry))
    return registry


def _lookup_local(name: str, registry: dict[str, VesselRef], owner_inn: str | None = None) -> VesselRef | None:
    keys = [normalize_vessel_name(name), _latin_key(name)]
    keys = [k for k in keys if k]

    for norm in keys:
        if norm in registry:
            ref = registry[norm]
            if owner_inn and ref.owner_inn and ref.owner_inn != owner_inn:
                continue
            return ref

    candidates: list[VesselRef] = []
    for key, ref in registry.items():
        if owner_inn and ref.owner_inn and ref.owner_inn != owner_inn:
            continue
        for norm in keys:
            if norm in key or key in norm:
                candidates.append(ref)
                break

    if not candidates:
        return None
    with_imo = [c for c in candidates if c.imo]
    if len(with_imo) == 1:
        return with_imo[0]
    unique_names = {c.name for c in candidates}
    if len(unique_names) == 1:
        return candidates[0]
    return None


def _lookup_gfw_by_id(gfw_id: str, display_name: str) -> VesselMatch | None:
    try:
        from scripts.gfw_client import extract_imo_from_vessel_detail, vessel_by_id
    except ImportError:
        return None
    try:
        detail = vessel_by_id(gfw_id)
    except Exception as exc:
        logger.warning("GFW vessel_by_id failed for %s: %s", gfw_id, exc)
        return None
    if not detail:
        return None
    imo_raw = extract_imo_from_vessel_detail(detail)
    if not imo_raw:
        return None
    try:
        imo = int(str(imo_raw).strip())
    except ValueError:
        return None
    return VesselMatch(name=display_name, imo=imo, source="gfw_api", registry_name=display_name)


def _gfw_search_queries(name: str) -> list[str]:
    query = _extract_vessel_name(name)
    seen: set[str] = set()
    queries: list[str] = []

    def add(candidate: str) -> None:
        candidate = candidate.strip()
        if candidate and candidate not in seen:
            seen.add(candidate)
            queries.append(candidate)

    add(query)
    latin = _latin_key(query)
    if latin:
        add(latin)
        add(latin.upper())
        parts = latin.split()
        if len(parts) >= 2 and len(parts[-1]) >= 5:
            add(parts[-1].upper())
    return queries


def _lookup_gfw(name: str) -> VesselMatch | None:
    if os.getenv("VESSEL_IMO_USE_GFW", "1").strip() in {"0", "false", "no"}:
        return None
    try:
        from scripts.gfw_client import vessels_search
    except ImportError:
        logger.debug("gfw_client недоступен")
        return None

    query = _extract_vessel_name(name)
    entries: list[dict[str, Any]] = []
    for search_query in _gfw_search_queries(name):
        try:
            batch = vessels_search(search_query, limit=5)
        except Exception as exc:
            logger.warning("GFW lookup failed for %s: %s", search_query, exc)
            continue
        if batch:
            entries = batch
            break

    query_norm = normalize_vessel_name(query)
    query_latin = _latin_key(query)
    for entry in entries:
        imo_raw = entry.get("imo")
        if not imo_raw:
            continue
        try:
            imo = int(str(imo_raw).strip())
        except ValueError:
            continue

        candidate_name = str(entry.get("name") or entry.get("shipname") or query).strip()
        candidate_norm = normalize_vessel_name(candidate_name)
        candidate_latin = _latin_key(candidate_name)
        names_match = any(
            left == right or left in right or right in left
            for left in (query_norm, query_latin)
            for right in (candidate_norm, candidate_latin)
            if left and right
        )
        if names_match:
            return VesselMatch(name=query, imo=imo, source="gfw_api", registry_name=candidate_name)

    # Предпочитаем RUS + fishing/trawler при нескольких совпадениях
    preferred: VesselMatch | None = None
    for entry in entries:
        imo_raw = entry.get("imo")
        if not imo_raw:
            continue
        try:
            imo = int(str(imo_raw).strip())
        except ValueError:
            continue
        candidate_name = str(entry.get("name") or entry.get("shipname") or query).strip()
        candidate_latin = _latin_key(candidate_name)
        if query_latin and (
            query_latin == candidate_latin
            or query_latin.replace(" ", "") in candidate_latin.replace(" ", "")
            or candidate_latin.replace(" ", "") in query_latin.replace(" ", "")
        ):
            flag = ""
            for block in (entry.get("selfReportedInfo") or entry.get("registryInfo") or []):
                if isinstance(block, dict) and block.get("flag"):
                    flag = str(block["flag"])
                    break
            geartypes = str(entry.get("combinedSourcesInfo") or entry)
            match = VesselMatch(name=query, imo=imo, source="gfw_api", registry_name=candidate_name)
            if flag == "RUS" or "TRAWL" in geartypes.upper():
                return match
            preferred = preferred or match

    if preferred:
        return preferred

    if len(entries) == 1 and entries[0].get("imo"):
        try:
            imo = int(str(entries[0]["imo"]).strip())
            return VesselMatch(
                name=query,
                imo=imo,
                source="gfw_api",
                registry_name=str(entries[0].get("name") or query),
            )
        except ValueError:
            return None
    return None


def lookup_vessel_imo(name: str, *, use_gfw: bool = True, owner_inn: str | None = None) -> VesselMatch | None:
    registry = load_vessel_registry()
    ref = _lookup_local(name, registry, owner_inn=owner_inn)
    if ref and ref.imo:
        return VesselMatch(name=ref.name, imo=ref.imo, source=ref.source, registry_name=ref.name)
    if ref and ref.gfw_name and use_gfw:
        match = _lookup_gfw(ref.gfw_name)
        if match:
            return VesselMatch(name=ref.name, imo=match.imo, source=match.source, registry_name=match.registry_name)
    if ref and ref.gfw_id and use_gfw:
        match = _lookup_gfw_by_id(ref.gfw_id, ref.name)
        if match:
            return match
    if use_gfw:
        return _lookup_gfw(name)
    return None


def _collect_vessel_names(analysis: dict[str, Any]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()

    def add(raw: Any) -> None:
        if isinstance(raw, str):
            candidate = raw.strip()
        elif isinstance(raw, dict):
            candidate = str(raw.get("name") or "").strip()
        else:
            return
        if not candidate:
            return
        norm = normalize_vessel_name(candidate)
        if norm in seen:
            return
        seen.add(norm)
        names.append(candidate)

    for item in analysis.get("active_vessels") or []:
        add(item)

    for item in analysis.get("vessel_dislocation") or []:
        if isinstance(item, dict):
            add(item.get("name"))

    return names


def enrich_analysis_with_imo(
    analysis: dict[str, Any],
    *,
    owner_inn: str | None = None,
) -> dict[str, Any]:
    """Добавляет IMO к судам и формирует единый список vessels."""
    registry = load_vessel_registry()
    imo_cache: dict[str, VesselMatch | None] = {}

    def resolve(name: str) -> VesselMatch | None:
        norm = normalize_vessel_name(name)
        if norm not in imo_cache:
            ref = _lookup_local(name, registry, owner_inn=owner_inn)
            match: VesselMatch | None = None
            if ref and ref.imo:
                match = VesselMatch(name=ref.name, imo=ref.imo, source=ref.source, registry_name=ref.name)
            elif ref and ref.gfw_id:
                match = _lookup_gfw_by_id(ref.gfw_id, ref.name)
            if match is None:
                match = _lookup_gfw(name)
            imo_cache[norm] = match
        return imo_cache[norm]

    vessels: list[dict[str, Any]] = []
    dislocation = analysis.get("vessel_dislocation") or []
    parsed_vessels = analysis.get("vessels") or []
    dislocation_names = {
        normalize_vessel_name(str(item.get("name", ""))): item
        for item in dislocation
        if isinstance(item, dict) and item.get("name")
    }

    processed: set[str] = set()

    def append_vessel(name: str, extra: dict[str, Any] | None = None) -> None:
        norm = normalize_vessel_name(name)
        if not norm or norm in processed:
            return
        processed.add(norm)

        match = resolve(name)
        row: dict[str, Any] = {"name": _extract_vessel_name(name)}
        if extra:
            row.update({k: v for k, v in extra.items() if v is not None})
        if match:
            row["imo"] = match.imo
            row["imo_source"] = match.source
            if match.registry_name and match.registry_name.lower() != row["name"].lower():
                row["registry_name"] = match.registry_name
        elif "imo" not in row:
            row["imo"] = None
            row["imo_source"] = None
        vessels.append(row)

    for item in dislocation:
        if isinstance(item, dict) and item.get("name"):
            append_vessel(
                str(item["name"]),
                {
                    "status": item.get("status"),
                    "location": item.get("location"),
                    "end_date": item.get("end_date"),
                },
            )

    for item in parsed_vessels:
        if isinstance(item, dict) and item.get("name"):
            append_vessel(
                str(item["name"]),
                {
                    k: item.get(k)
                    for k in ("status", "location", "end_date", "imo", "imo_source")
                    if item.get(k) is not None
                },
            )

    for raw in analysis.get("active_vessels") or []:
        name = raw if isinstance(raw, str) else str(raw.get("name", "")) if isinstance(raw, dict) else ""
        if name.strip():
            append_vessel(name.strip())

    enriched_dislocation: list[dict[str, Any]] = []
    for item in dislocation:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        name = str(item["name"])
        match = resolve(name)
        row = dict(item)
        row["imo"] = match.imo if match else None
        row["imo_source"] = match.source if match else None
        enriched_dislocation.append(row)

    enriched_active: list[dict[str, Any]] = []
    for raw in analysis.get("active_vessels") or []:
        name = raw if isinstance(raw, str) else str(raw.get("name", "")) if isinstance(raw, dict) else ""
        if not name.strip():
            continue
        match = resolve(name.strip())
        enriched_active.append(
            {
                "name": _extract_vessel_name(name.strip()),
                "imo": match.imo if match else None,
                "imo_source": match.source if match else None,
            }
        )

    result = dict(analysis)
    result["vessel_dislocation"] = enriched_dislocation
    result["active_vessels"] = enriched_active
    result["vessels"] = vessels
    result["vessels_with_imo"] = sum(1 for v in vessels if v.get("imo"))
    result["vessels_without_imo"] = sum(1 for v in vessels if not v.get("imo"))
    return result
