#!/usr/bin/env python3
"""
Клиент Global Fishing Watch API v3.
Токен: https://globalfishingwatch.org/our-apis/tokens
Переменная окружения: GFW_API_TOKEN

Базовый URL: https://gateway.api.globalfishingwatch.org/v3
"""

from __future__ import annotations

import os
import time
import warnings
from typing import Any

# Подавить предупреждение urllib3 про LibreSSL на macOS (до импорта requests)
warnings.filterwarnings("ignore", message=".*OpenSSL.*")
warnings.filterwarnings("ignore", message=".*urllib3.*OpenSSL.*")

import requests

GFW_API_BASE = "https://gateway.api.globalfishingwatch.org/v3"
DEFAULT_TIMEOUT = 30


def get_token() -> str | None:
    token = os.environ.get("GFW_API_TOKEN", "").strip()
    return token or None


def _headers() -> dict[str, str]:
    token = get_token()
    if not token:
        return {"Content-Type": "application/json"}
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


# В v3 поиск судов требует параметр dataset(s): public-global-vessel-identity
GFW_VESSEL_IDENTITY_DATASET = "public-global-vessel-identity:latest"


def vessels_search(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Поиск судов по названию. Возвращает список совпадений. При 503/429 — повтор с задержкой."""
    url = f"{GFW_API_BASE}/vessels/search"
    headers = _headers()
    params = {
        "query": query,
        "limit": limit,
        "datasets[0]": GFW_VESSEL_IDENTITY_DATASET,
    }
    max_retries = 4  # 1 попытка + 3 повтора при 503/429
    for attempt in range(max_retries):
        r = requests.get(url, headers=headers, params=params, timeout=DEFAULT_TIMEOUT)
        if r.status_code == 422:
            try:
                err_body = r.json()
            except ValueError:
                err_body = r.text[:500]
            raise RuntimeError(f"GFW API 422 Unprocessable Entity. Response: {err_body}")
        if r.status_code in (503, 429):
            if attempt < max_retries - 1:
                wait = (2 ** attempt) * 10  # 10, 20, 40 сек
                time.sleep(wait)
                continue
        r.raise_for_status()
        data = r.json()
        entries = data.get("entries", [])
        return [_normalize_search_entry(e) for e in entries]
    # Все повторы при 503/429 исчерпаны
    r.raise_for_status()
    return []


def _extract_imo(obj: dict[str, Any] | list) -> str | None:
    """Достать IMO из объекта (словарь или элемент массива). IMO — уникальный номер судна."""
    if isinstance(obj, list):
        for item in obj:
            imo = _extract_imo(item)
            if imo:
                return imo
        return None
    if not isinstance(obj, dict):
        return None
    for key in ("imo", "imoNumber", "IMO"):
        val = obj.get(key)
        if val is not None and str(val).strip():
            s = str(val).strip()
            if s.isdigit() and 6 <= len(s) <= 8:
                return s
    return None


def _normalize_search_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Из ответа v3 vessels/search извлекает id, name и IMO для совместимости."""
    gfw_id = None
    name = None
    imo = _extract_imo(entry) or entry.get("imo")
    self_reported = entry.get("selfReportedInfo") or [{}]
    combined = entry.get("combinedSourcesInfo") or [{}]
    registry = entry.get("registryInfo") or [{}]
    if self_reported and self_reported[0]:
        gfw_id = self_reported[0].get("id")
        name = name or self_reported[0].get("shipname") or self_reported[0].get("nShipname")
        if not imo:
            imo = _extract_imo(self_reported)
    if not gfw_id and combined and combined[0]:
        gfw_id = combined[0].get("vesselId")
    if not imo:
        imo = _extract_imo(combined) or _extract_imo(registry)
    if not name and registry and registry[0]:
        name = registry[0].get("shipname") or registry[0].get("nShipname")
    out = {"id": gfw_id, "vesselId": gfw_id, "name": name, **entry}
    if imo:
        out["imo"] = imo
    return out


def _entry_contains_vessel_id(entry: dict[str, Any], vessel_id: str) -> bool:
    """Проверяет, что в записи поиска фигурирует именно этот vessel_id (иначе мы могли получить чужое судно)."""
    vid = (vessel_id or "").strip()
    if not vid:
        return False
    for s in entry.get("selfReportedInfo") or []:
        if isinstance(s, dict) and str(s.get("id") or "").strip() == vid:
            return True
    for c in entry.get("combinedSourcesInfo") or []:
        if isinstance(c, dict) and str(c.get("vesselId") or "").strip() == vid:
            return True
    return False


def vessel_by_id(vessel_id: str) -> dict[str, Any] | None:
    """
    Получить судно по GFW id (в т.ч. identity: owner, operator).
    В v3 для поиска обязателен datasets[0]; для GET /vessels/{id} в документации явно не указано.
    Пробуем с dataset, при 422 — через search по id. Важно: из search берём только запись,
    в которой действительно есть наш vessel_id (иначе поиск по строке id мог вернуть другое судно).
    """
    url = f"{GFW_API_BASE}/vessels/{vessel_id}"
    params = {"datasets[0]": GFW_VESSEL_IDENTITY_DATASET}
    r = requests.get(url, headers=_headers(), params=params, timeout=DEFAULT_TIMEOUT)
    if r.status_code == 404:
        return None
    if r.status_code == 200:
        return r.json()
    if r.status_code == 422:
        # v3 часто возвращает 422 для GET /vessels/{id} — получаем детали через search по id
        entries = vessels_search(vessel_id, limit=10)
        for e in entries:
            if _entry_contains_vessel_id(e, vessel_id):
                return e
        # если ни одна запись не содержит наш id — не подставляем первую попавшуюся
        return None
    r.raise_for_status()
    return r.json()


def extract_imo_from_vessel_detail(detail: dict[str, Any]) -> str | None:
    """Из ответа vessel_by_id извлечь IMO (уникальный номер судна), если есть."""
    return _extract_imo(detail)


def vessel_identity_text(vessel_detail: dict[str, Any]) -> str:
    """Собрать в одну строку owner/operator/name из ответа vessel by id для сопоставления с компанией."""
    parts = []
    for key in ("name", "owner", "operator", "ownership", "operators"):
        val = vessel_detail.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(val.strip())
        if isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    parts.append(vessel_identity_text(item))
                elif isinstance(item, str):
                    parts.append(item.strip())
        if isinstance(val, dict):
            parts.append(vessel_identity_text(val))
    identity = vessel_detail.get("identity") or vessel_detail.get("identities")
    if isinstance(identity, dict):
        parts.append(vessel_identity_text(identity))
    if isinstance(identity, list):
        for item in identity:
            if isinstance(item, dict):
                parts.append(item.get("name") or item.get("value") or "")
    # v3 search/detail: registryOwners, selfReportedInfo
    for reg in vessel_detail.get("registryOwners") or []:
        if isinstance(reg, dict) and reg.get("name"):
            parts.append(str(reg["name"]).strip())
    for s in vessel_detail.get("selfReportedInfo") or []:
        if isinstance(s, dict) and s.get("shipname"):
            parts.append(str(s["shipname"]).strip())
    return " ".join(p for p in parts if p)


def extract_vessel_detail_fields(detail: dict[str, Any], requested_id: str | None = None) -> dict[str, Any]:
    """
    Из ответа vessel_by_id или из элемента entries поиска извлекает поля для обогащения кэша.
    requested_id: если задан, предпочитаем идентичность (selfReportedInfo/combinedSourcesInfo) с этим id,
      чтобы взять флаг и данные именно нашего судна, а не первой из нескольких идентичностей.
    """
    out: dict[str, Any] = {}
    rid = (requested_id or "").strip()

    # Выбрать блок selfReportedInfo с нашим id (в одной записи бывает несколько идентичностей с разными флагами)
    self_list = detail.get("selfReportedInfo") or []
    combined_list = detail.get("combinedSourcesInfo") or []
    self_rep = None
    combined = None
    if rid:
        for s in self_list:
            if isinstance(s, dict) and str(s.get("id") or "").strip() == rid:
                self_rep = s
                break
        for c in combined_list:
            if isinstance(c, dict) and str(c.get("vesselId") or "").strip() == rid:
                combined = c
                break
    if self_rep is None and self_list:
        self_rep = self_list[0] if isinstance(self_list[0], dict) else None
    if combined is None and combined_list:
        combined = combined_list[0] if isinstance(combined_list[0], dict) else None

    reg_info = (detail.get("registryInfo") or [{}])[0] if detail.get("registryInfo") else {}
    reg_owners = detail.get("registryOwners") or []

    if isinstance(reg_info, dict):
        for key, gfw_key in [("flag", "gfw_flag"), ("ssvid", "gfw_ssvid"), ("lengthM", "gfw_length_m"), ("tonnageGt", "gfw_tonnage_gt")]:
            v = reg_info.get(key)
            if v is not None and (not isinstance(v, str) or v.strip()):
                out[gfw_key] = v
        geartype = reg_info.get("geartype")
        if isinstance(geartype, list) and geartype:
            out["gfw_geartype"] = geartype[0] if isinstance(geartype[0], str) else str(geartype[0])
        elif isinstance(geartype, str) and geartype.strip():
            out["gfw_geartype"] = geartype.strip()
    if isinstance(self_rep, dict):
        for key, gfw_key in [("flag", "gfw_flag"), ("ssvid", "gfw_ssvid")]:
            if gfw_key not in out:
                v = self_rep.get(key)
                if v is not None and (not isinstance(v, str) or v.strip()):
                    out[gfw_key] = v
        if "gfw_geartype" not in out and self_rep.get("geartype"):
            g = self_rep["geartype"]
            out["gfw_geartype"] = g[0] if isinstance(g, list) and g else str(g)
    # Владелец: registryOwners[].name или .owner; registryOperators для оператора
    for arr_key, gfw_key in [("registryOwners", "gfw_owner"), ("registryOperators", "gfw_operator")]:
        if gfw_key in out:
            continue
        arr = detail.get(arr_key) or []
        for item in arr if isinstance(arr, list) else []:
            if not isinstance(item, dict):
                continue
            for key in ("name", "owner", "company"):
                val = item.get(key)
                if val and str(val).strip():
                    out[gfw_key] = str(val).strip()
                    break
            if gfw_key in out:
                break
    if isinstance(combined, dict):
        geartypes = combined.get("geartypes") or []
        if not out.get("gfw_geartype") and geartypes and isinstance(geartypes[0], dict):
            out["gfw_geartype"] = geartypes[0].get("name") or str(geartypes[0])
    # Плоские поля (альтернативный формат ответа)
    for flat_key, gfw_key in [("flag", "gfw_flag"), ("owner", "gfw_owner"), ("operator", "gfw_operator"), ("ssvid", "gfw_ssvid")]:
        if gfw_key not in out and detail.get(flat_key):
            out[gfw_key] = detail[flat_key]
    return out


# Датасеты событий GFW Events API
GFW_FISHING_EVENTS_DATASET = "public-global-fishing-events:latest"
GFW_PORT_VISITS_DATASET = "public-global-port-visits:latest"
GFW_ENCOUNTERS_DATASET = "public-global-encounters:latest"


def events_get(
    vessel_ids: list[str],
    start_date: str,
    end_date: str,
    event_type: str | None = None,
    limit: int = 100,
    dataset: str | None = None,
) -> list[dict[str, Any]]:
    """
    События по судам (порты, рыбалка и т.д.). В ответе есть lat/lon.
    start_date, end_date: YYYY-MM-DD.
    dataset: например GFW_FISHING_EVENTS_DATASET для событий рыбалки.
    """
    url = f"{GFW_API_BASE}/events"
    params = {
        "start-date": start_date,
        "end-date": end_date,
        "limit": limit,
    }
    if event_type:
        params["event-type"] = event_type.upper()
    if dataset:
        params["datasets[0]"] = dataset
    payload = {"vesselIds": vessel_ids}
    r = requests.post(
        url, headers=_headers(), params=params, json=payload, timeout=DEFAULT_TIMEOUT
    )
    r.raise_for_status()
    data = r.json()
    return data.get("entries", [])


def _normalize_vessel_id(vid: str) -> str:
    """Привести vessel_id к одному виду для сопоставления (нижний регистр, без лишних пробелов)."""
    if not vid:
        return ""
    return str(vid).strip().lower()


def _extract_lat_lon(e: dict[str, Any]) -> tuple[float | None, float | None]:
    """Из события извлечь lat, lon из разных возможных полей ответа GFW."""
    lat = e.get("lat") or e.get("latitude") or e.get("startLat") or e.get("endLat")
    lon = e.get("lon") or e.get("longitude") or e.get("startLon") or e.get("endLon")
    pos = e.get("position") or {}
    if isinstance(pos, dict):
        lat = lat or pos.get("lat") or pos.get("latitude")
        lon = lon or pos.get("lon") or pos.get("longitude")
    geom = e.get("geometry") or {}
    if isinstance(geom, dict) and geom.get("type") == "Point":
        coords = geom.get("coordinates")
        if isinstance(coords, (list, tuple)) and len(coords) >= 2:
            lon, lat = coords[0], coords[1]
    if lat is not None and lon is not None:
        try:
            return (float(lat), float(lon))
        except (TypeError, ValueError):
            pass
    return (None, None)


def _positions_from_events(
    vessel_ids: list[str],
    start_str: str,
    end_str: str,
    event_type: str,
    dataset: str,
) -> dict[str, tuple[float, float]]:
    """Собрать позиции из событий одного типа (fishing или port_visit)."""
    positions: dict[str, tuple[float, float]] = {}
    id_set = {_normalize_vessel_id(vid): vid for vid in vessel_ids}
    batch = 50
    for i in range(0, len(vessel_ids), batch):
        batch_ids = vessel_ids[i : i + batch]
        try:
            entries = events_get(
                batch_ids, start_str, end_str, limit=500,
                event_type=event_type, dataset=dataset,
            )
        except requests.RequestException:
            time.sleep(2)
            continue
        for e in entries:
            vid = e.get("vesselId") or (e.get("vessel") or {}).get("id")
            if not vid:
                continue
            vid_norm = _normalize_vessel_id(vid)
            lat, lon = _extract_lat_lon(e)
            if lat is not None and lon is not None:
                # Сохраняем под ключом из исходного списка для совместимости
                key = id_set.get(vid_norm) or vid
                positions[key] = (lat, lon)
        time.sleep(0.3)
    return positions


def events_get_latest_positions(
    vessel_ids: list[str], days_back: int = 14
) -> dict[str, tuple[float, float]]:
    """
    Для каждого vessel_id возвращает (lat, lon) последнего события с координатами.
    Сначала fishing events, затем для судов без позиции — port visits.
    """
    from datetime import datetime, timedelta

    end = datetime.utcnow()
    start = end - timedelta(days=days_back)
    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")

    positions = _positions_from_events(
        vessel_ids, start_str, end_str, "fishing", GFW_FISHING_EVENTS_DATASET
    )
    missing = [vid for vid in vessel_ids if vid not in positions]
    if missing:
        port_pos = _positions_from_events(
            missing, start_str, end_str, "port_visit", GFW_PORT_VISITS_DATASET
        )
        positions.update(port_pos)
    return positions


def get_fishing_events_summary(
    vessel_ids: list[str],
    days_back: int = 90,
    batch_size: int = 50,
) -> dict[str, dict[str, Any]]:
    """
    Для каждого vessel_id возвращает сводку по событиям рыбалки за последние days_back дней:
    { vessel_id: { "count": N, "last_date": "YYYY-MM-DD" } }.
    Использует датасет public-global-fishing-events. При большом числе событий limit=2000.
    """
    from datetime import datetime, timedelta

    end = datetime.utcnow()
    start = end - timedelta(days=days_back)
    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")
    result: dict[str, dict[str, Any]] = {vid: {"count": 0, "last_date": None} for vid in vessel_ids}
    total_batches = (len(vessel_ids) + batch_size - 1) // batch_size
    for i in range(0, len(vessel_ids), batch_size):
        batch_num = i // batch_size + 1
        print(f"  fishing батч {batch_num}/{total_batches}", flush=True)
        batch_ids = vessel_ids[i : i + batch_size]
        try:
            entries = events_get(
                batch_ids,
                start_str,
                end_str,
                event_type="fishing",
                limit=2000,
                dataset=GFW_FISHING_EVENTS_DATASET,
            )
        except requests.RequestException:
            time.sleep(2)
            continue
        for e in entries:
            vid = e.get("vesselId") or (e.get("vessel") or {}).get("id")
            if vid not in result:
                result[vid] = {"count": 0, "last_date": None}
            result[vid]["count"] = result[vid]["count"] + 1
            start_ts = e.get("start") or e.get("end")
            if start_ts:
                date_str = start_ts[:10] if isinstance(start_ts, str) else None
                if date_str and (result[vid]["last_date"] is None or date_str > result[vid]["last_date"]):
                    result[vid]["last_date"] = date_str
        time.sleep(0.5)
    return result


def _events_summary_generic(
    vessel_ids: list[str],
    days_back: int,
    batch_size: int,
    dataset: str,
    event_type: str,
    progress_label: str = "events",
) -> dict[str, dict[str, Any]]:
    """Общая сводка по событиям: count и last_date на vessel_id."""
    from datetime import datetime, timedelta

    end = datetime.utcnow()
    start = end - timedelta(days=days_back)
    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")
    result: dict[str, dict[str, Any]] = {vid: {"count": 0, "last_date": None} for vid in vessel_ids}
    total_batches = (len(vessel_ids) + batch_size - 1) // batch_size
    for i in range(0, len(vessel_ids), batch_size):
        batch_num = i // batch_size + 1
        print(f"  {progress_label} батч {batch_num}/{total_batches}", flush=True)
        batch_ids = vessel_ids[i : i + batch_size]
        try:
            entries = events_get(
                batch_ids, start_str, end_str,
                event_type=event_type, limit=2000, dataset=dataset,
            )
        except requests.RequestException:
            time.sleep(2)
            continue
        for e in entries:
            vid = e.get("vesselId") or (e.get("vessel") or {}).get("id")
            if vid not in result:
                result[vid] = {"count": 0, "last_date": None}
            result[vid]["count"] = result[vid]["count"] + 1
            start_ts = e.get("start") or e.get("end")
            if start_ts:
                date_str = start_ts[:10] if isinstance(start_ts, str) else None
                if date_str and (result[vid]["last_date"] is None or date_str > result[vid]["last_date"]):
                    result[vid]["last_date"] = date_str
        time.sleep(0.5)
    return result


def get_port_visits_summary(
    vessel_ids: list[str], days_back: int = 90, batch_size: int = 50
) -> dict[str, dict[str, Any]]:
    """Сводка заходов в порт за days_back дней: count, last_date по vessel_id."""
    return _events_summary_generic(
        vessel_ids, days_back, batch_size,
        GFW_PORT_VISITS_DATASET, "port_visit", progress_label="port_visits",
    )


def get_encounters_summary(
    vessel_ids: list[str], days_back: int = 90, batch_size: int = 50
) -> dict[str, dict[str, Any]]:
    """Сводка встреч/перегрузок (encounters) за days_back дней: count, last_date по vessel_id."""
    return _events_summary_generic(
        vessel_ids, days_back, batch_size,
        GFW_ENCOUNTERS_DATASET, "encounter", progress_label="encounters",
    )


# ---------------------------------------------------------------------------
# Tracks API (public-global-all-tracks) + fallback AIS Presence (hourly)
# ---------------------------------------------------------------------------
GFW_ALL_TRACKS_DATASET = "public-global-all-tracks:latest"
GFW_PRESENCE_DATASET = "public-global-presence:latest"


class GfwApiError(RuntimeError):
    """Ошибка GFW API с кодом HTTP."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def _request_with_retry(
    method: str,
    url: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    max_retries: int = 4,
) -> requests.Response:
    """GET/POST с повторами при 429/503/524."""
    for attempt in range(max_retries):
        if method.upper() == "GET":
            r = requests.get(url, headers=_headers(), params=params, timeout=timeout)
        else:
            r = requests.post(
                url, headers=_headers(), params=params, json=json_body, timeout=timeout
            )
        if r.status_code in (429, 503, 524) and attempt < max_retries - 1:
            time.sleep((2 ** attempt) * 5)
            continue
        return r
    return r


def split_date_range(start: str, end: str, max_days: int = 90) -> list[tuple[str, str]]:
    """Разбить [start, end] на интервалы не длиннее max_days (YYYY-MM-DD)."""
    from datetime import date, timedelta

    s = date.fromisoformat(start[:10])
    e = date.fromisoformat(end[:10])
    if s > e:
        raise ValueError(f"start > end: {start} > {end}")
    chunks: list[tuple[str, str]] = []
    cur = s
    while cur <= e:
        chunk_end = min(cur + timedelta(days=max_days - 1), e)
        chunks.append((cur.isoformat(), chunk_end.isoformat()))
        cur = chunk_end + timedelta(days=1)
    return chunks


def fetch_vessel_tracks_raw(
    vessel_id: str,
    start_date: str,
    end_date: str,
    *,
    dataset: str = GFW_ALL_TRACKS_DATASET,
    fmt: str = "CSV",
) -> tuple[str, bytes | dict]:
    """
    Сырой трек судна через GET /v3/vessels/{id}/tracks.
    Требует доступ к public-global-all-tracks (Advanced / map download tier).

    Returns:
        (content_type, body) — body bytes для CSV или dict для JSON.
    Raises:
        GfwApiError(403) если нет доступа к датасету треков.
    """
    start_iso = start_date if "T" in start_date else f"{start_date[:10]}T00:00:00.000Z"
    end_iso = end_date if "T" in end_date else f"{end_date[:10]}T23:59:59.999Z"
    url = f"{GFW_API_BASE}/vessels/{vessel_id}/tracks"
    params = {
        "start-date": start_iso,
        "end-date": end_iso,
        "datasets[0]": dataset,
        "format": fmt.upper(),
    }
    r = _request_with_retry("GET", url, params=params, timeout=120)
    if r.status_code == 403:
        raise GfwApiError(403, r.text)
    r.raise_for_status()
    ctype = (r.headers.get("content-type") or "").lower()
    if "json" in ctype or fmt.upper() == "JSON":
        return ctype, r.json()
    return ctype, r.content


def _normalize_track_point(row: dict[str, Any], vessel_id: str, seg_fallback: str) -> dict[str, Any]:
    """Привести точку трека к схеме CSV выгрузки с карты GFW."""
    ts = (
        row.get("timestamp")
        or row.get("date")
        or row.get("time")
        or row.get("entryTimestamp")
    )
    lon = row.get("lon") if row.get("lon") is not None else row.get("longitude")
    lat = row.get("lat") if row.get("lat") is not None else row.get("latitude")
    speed = row.get("speed") if row.get("speed") is not None else row.get("sog")
    course = row.get("course") if row.get("course") is not None else row.get("cog")
    depth = row.get("depth")
    seg = row.get("seg_id") or row.get("segId") or row.get("segmentId") or seg_fallback
    return {
        "lon": lon,
        "lat": lat,
        "course": course,
        "timestamp": ts,
        "speed": speed,
        "depth": depth,
        "seg_id": seg,
        "vessel_id": vessel_id,
    }


def parse_tracks_response(
    payload: bytes | dict,
    vessel_id: str,
    *,
    content_type: str = "",
) -> list[dict[str, Any]]:
    """Разобрать ответ tracks API (CSV bytes или JSON) в список точек."""
    import csv
    import io

    points: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        entries = payload.get("entries") or payload.get("positions") or payload.get("data") or []
        if isinstance(entries, dict):
            entries = [entries]
        for row in entries:
            if isinstance(row, dict):
                points.append(_normalize_track_point(row, vessel_id, vessel_id))
        return points

    text = payload.decode("utf-8-sig", errors="replace").strip()
    if not text:
        return points
    # иногда CSV приходит внутри zip — пока только plain CSV
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        norm = _normalize_track_point(
            {k.lower(): v for k, v in row.items()},
            vessel_id,
            vessel_id,
        )
        points.append(norm)
    return points


def fetch_vessel_presence_hourly(
    vessel_id: str,
    start_date: str,
    end_date: str,
    bbox: tuple[float, float, float, float],
) -> list[dict[str, Any]]:
    """
  Hourly AIS presence (1 позиция/час) в bbox, фильтр по vessel_id на клиенте.
  bbox: (west, south, east, north).
  """
    west, south, east, north = bbox
    url = f"{GFW_API_BASE}/4wings/report"
    params = {
        "spatial-resolution": "HIGH",
        "temporal-resolution": "HOURLY",
        "group-by": "VESSEL_ID",
        "datasets[0]": GFW_PRESENCE_DATASET,
        "date-range": f"{start_date[:10]},{end_date[:10]}",
        "format": "JSON",
    }
    body = {
        "geojson": {
            "type": "Polygon",
            "coordinates": [[
                [west, south], [east, south], [east, north], [west, north], [west, south],
            ]],
        }
    }
    r = _request_with_retry("POST", url, params=params, json_body=body, timeout=180)
    if r.status_code == 403:
        raise GfwApiError(403, r.text)
    r.raise_for_status()
    data = r.json()
    entries = data.get("entries") or []
    if not entries:
        return []
    dataset_key = next(iter(entries[0].keys()))
    rows = entries[0].get(dataset_key) or []
    vid_norm = _normalize_vessel_id(vessel_id)
    out = []
    for row in rows:
        rid = row.get("vesselId") or row.get("vessel_id")
        if _normalize_vessel_id(str(rid or "")) != vid_norm:
            continue
        out.append(row)
    return out


def presence_rows_to_track_points(
    rows: list[dict[str, Any]],
    vessel_id: str,
) -> list[dict[str, Any]]:
    """
    Конвертировать hourly presence → псевдо-трек для gwf_analytic.py.
    speed/course вычисляются по соседним точкам; depth пустой.
    """
    import math
    from datetime import datetime, timezone

    if not rows:
        return []

    def parse_ts(val: str) -> datetime:
        s = str(val).strip().replace(" ", "T")
        if s.endswith("Z"):
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        if "+" in s[10:]:
            return datetime.fromisoformat(s)
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)

    def hav_nm(lat1, lon1, lat2, lon2):
        r_km = 6371.0088
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlmb = math.radians(lon2 - lon1)
        a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
        km = 2 * r_km * math.asin(min(1.0, math.sqrt(a)))
        return km / 1.852

    def bearing(lat1, lon1, lat2, lon2):
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dlmb = math.radians(lon2 - lon1)
        x = math.sin(dlmb) * math.cos(p2)
        y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlmb)
        return (math.degrees(math.atan2(x, y)) + 360) % 360

    sorted_rows = sorted(rows, key=lambda r: r.get("date") or "")
    points: list[dict[str, Any]] = []
    for i, row in enumerate(sorted_rows):
        ts = parse_ts(row["date"])
        lat, lon = float(row["lat"]), float(row["lon"])
        speed, course = 0.0, 0.0
        if i > 0:
            prev = points[-1]
            dt_h = (ts - parse_ts(sorted_rows[i - 1]["date"])).total_seconds() / 3600
            if dt_h > 0:
                dist = hav_nm(float(prev["lat"]), float(prev["lon"]), lat, lon)
                speed = dist / dt_h
                course = bearing(float(prev["lat"]), float(prev["lon"]), lat, lon)
        points.append({
            "lon": lon,
            "lat": lat,
            "course": round(course, 1),
            "timestamp": ts.isoformat().replace("+00:00", "Z"),
            "speed": round(speed, 2),
            "depth": "",
            "seg_id": f"{vessel_id}-presence",
            "vessel_id": vessel_id,
            "source": "presence_hourly",
        })
    return points


def tracks_access_available(vessel_id: str) -> bool:
    """Проверить, есть ли у токена доступ к public-global-all-tracks."""
    try:
        fetch_vessel_tracks_raw(
            vessel_id,
            "2026-01-01",
            "2026-01-02",
            fmt="JSON",
        )
        return True
    except GfwApiError as e:
        if e.status_code == 403:
            return False
        raise
    except requests.RequestException:
        return False

