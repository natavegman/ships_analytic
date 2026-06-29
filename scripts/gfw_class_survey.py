"""
Классовые освидетельствования РС (RMRS) — прогноз обязательного ремонта/докования.

Источники:
  1) RMRS surveys (если публичный доступ) — точные даты следующих осмотров.
  2) Regbook RMRS + история докований GFW — оценка по регламенту РС:
       Special (S)     — каждые 5 лет, с докованием
       Intermediate (IN) — 2–3-й год цикла, часто с донным осмотром
       Annual (A)      — ежегодно, обычно на плаву
       Bottom (D)      — донный осмотр, с докованием

Прогноз объединяется с операционным циклом докования (GFW) в единое окно
обслуживания для продаж оборудования.
"""

from __future__ import annotations

import glob
import json
import os
import re
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_RMRS_DIR = os.path.join(ROOT, "output")
DEFAULT_IMO_REGISTRY = os.path.join(ROOT, "data", "gfw_our_vessels.json")

# Коды освидетельствований, требующих докования (приоритет для прогноза).
DOCKING_SURVEY_CODES: dict[str, tuple[str, float]] = {
  # code_prefix -> (label_ru, priority 0..1)
    "CLC.S": ("Специальный периодический (докование)", 1.0),
    "CLC.D": ("Донный осмотр (докование)", 0.95),
    "CLC.IN": ("Промежуточный (возможно докование)", 0.75),
    "CLC.PSSP": ("Валопровод (докование)", 0.9),
    "CLC.PSSS": ("Валопровод (докование)", 0.9),
    "SC.D": ("Донный (статутарный)", 0.9),
    "SC.S": ("Специальный (статутарный)", 0.85),
    "SC.IN": ("Промежуточный (статутарный)", 0.7),
}

# Регламентные интервалы (лет) для fallback-оценки по Rules RS.
REGULATORY_INTERVALS_YEARS = {
    "special": 5.0,
    "intermediate": 2.5,
    "annual": 1.0,
    "bottom": 5.0,
}


def _norm_vessel_name(name: str) -> str:
    s = re.sub(r"\(RUS\)$", "", name or "", flags=re.I).strip().upper()
    s = re.sub(r"\s+", " ", s)
    return s


def load_imo_registry(path: str | None = None) -> dict[str, str]:
    """gfw_name (нормализованный) / name -> IMO."""
    path = path or DEFAULT_IMO_REGISTRY
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        rows = json.load(f)
    out: dict[str, str] = {}
    for r in rows:
        imo = r.get("imo")
        if not imo:
            continue
        imo_s = str(imo).strip()
        for key in ("gfw_name", "name"):
            v = r.get(key)
            if v:
                out[_norm_vessel_name(str(v))] = imo_s
    return out


def resolve_imo(vessel: str, registry: dict[str, str] | None = None) -> str | None:
    reg = registry if registry is not None else load_imo_registry()
    n = _norm_vessel_name(vessel)
    if n in reg:
        return reg[n]
    # частичное совпадение (ALEXANDR BELYAKOV в длинном имени)
    for k, imo in reg.items():
        if n in k or k in n:
            return imo
    return None


def load_rmrs_payload(imo: str, rmrs_dir: str | None = None) -> dict[str, Any] | None:
    """Загрузить сохранённый JSON RMRS по IMO."""
    d = rmrs_dir or DEFAULT_RMRS_DIR
    for pattern in (f"rmrs_events_{imo}.json", os.path.join(d, f"rmrs_events_{imo}.json")):
        if os.path.isfile(pattern):
            with open(pattern, encoding="utf-8") as f:
                return json.load(f)
    hits = glob.glob(os.path.join(d, f"*_{imo}.json"))
    for p in hits:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return None


def parse_rmrs_date_window(text: str) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
    """'28.02.2026 31.08.2026' -> (early, late). Одна дата -> (d, d)."""
    if not text or not str(text).strip():
        return None, None
    parts = re.findall(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", str(text))
    dates: list[pd.Timestamp] = []
    for d, m, y in parts:
        try:
            dates.append(pd.Timestamp(date(int(y), int(m), int(d)), tz="UTC"))
        except ValueError:
            continue
    if not dates:
        return None, None
    dates.sort()
    return dates[0], dates[-1]


def _parse_build_date(vessel_data: dict) -> pd.Timestamp | None:
    for key in ("Date of build", "Дата постройки"):
        raw = vessel_data.get(key, "")
        if not raw:
            continue
        m = re.search(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", str(raw))
        if m:
            d, mo, y = m.groups()
            try:
                return pd.Timestamp(date(int(y), int(mo), int(d)), tz="UTC")
            except ValueError:
                pass
    return None


def _survey_docking_info(code: str, name: str) -> tuple[bool, float, str]:
    code = (code or "").strip().upper()
    name_u = (name or "").upper()
    for prefix, (label, prio) in DOCKING_SURVEY_CODES.items():
        if code.startswith(prefix) or code == prefix:
            return True, prio, label
    if re.search(r"\bSPECIAL\b", name_u) and "PERIODICAL" in name_u:
        return True, 0.95, "Специальный периодический"
    if re.search(r"\bBOTTOM\b", name_u) or re.search(r"\bДОНН", name_u):
        return True, 0.9, "Донный осмотр"
    if re.search(r"\bINTERMEDIATE\b", name_u):
        return False, 0.6, "Промежуточный"
    if re.search(r"\bANNUAL\b", name_u):
        return False, 0.3, "Ежегодный"
    return False, 0.2, name or code or "прочее"


def surveys_from_payload(payload: dict[str, Any]) -> pd.DataFrame:
    rows = []
    for s in payload.get("surveys") or []:
        if not isinstance(s, dict):
            continue
        code = str(s.get("Code", "")).strip()
        name = str(s.get("Survey", "")).strip()
        date_next_raw = str(s.get("Date / time the next survey", "")).strip()
        date_last_raw = str(s.get("Date of last survey", "")).strip()
        status = str(s.get("Status", s.get("row_css_class", ""))).strip().upper()
        early, late = parse_rmrs_date_window(date_next_raw)
        dock_req, prio, label = _survey_docking_info(code, name)
        rows.append({
            "survey_type": str(s.get("Type", "")).strip(),
            "survey_code": code,
            "survey_name": name,
            "survey_label": label,
            "date_last_raw": date_last_raw,
            "date_next_raw": date_next_raw,
            "date_next_early": early,
            "date_next_late": late,
            "status": status,
            "docking_required": dock_req,
            "priority": prio,
            "is_due": status == "DUE" or "DUE" in status,
        })
    return pd.DataFrame(rows)


def estimate_regulatory_schedule(
    last_major_docking: pd.Timestamp | None,
    build_date: pd.Timestamp | None,
    *,
    reference: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """Оценка графика освидетельствований по регламенту РС (fallback)."""
    ref = reference or pd.Timestamp.now(tz="UTC")
    anchor = last_major_docking or build_date
    if anchor is None or pd.isna(anchor):
        return pd.DataFrame()

    rows = []
    specs = [
        ("CLC.A", "Ежегодный (оценка)", REGULATORY_INTERVALS_YEARS["annual"], False, 0.35),
        ("CLC.IN", "Промежуточный (оценка)", REGULATORY_INTERVALS_YEARS["intermediate"], False, 0.7),
        ("CLC.S", "Специальный периодический (оценка)", REGULATORY_INTERVALS_YEARS["special"], True, 1.0),
        ("CLC.D", "Донный осмотр (оценка)", REGULATORY_INTERVALS_YEARS["bottom"], True, 0.95),
    ]
    for code, label, interval_y, dock_req, prio in specs:
        margin = 90
        step = pd.Timedelta(days=interval_y * 365.25)
        # ближайшее окно освидетельствования, актуальное на дату ref
        due_date = anchor + step
        while due_date + pd.Timedelta(days=margin) < ref:
            due_date = due_date + step
        early = due_date - pd.Timedelta(days=margin)
        late = due_date + pd.Timedelta(days=margin)
        # Судно в эксплуатации → класс действителен, просрочки быть не может:
        # окно всегда прокатано вперёд (while выше), поэтому статус только
        # DUE (мы внутри окна) либо ESTIMATED (окно в будущем).
        in_window = early <= ref <= late
        rows.append({
            "survey_type": "Regulatory",
            "survey_code": code,
            "survey_name": label,
            "survey_label": label,
            "date_last_raw": anchor.strftime("%Y-%m-%d"),
            "date_next_raw": f"{early.strftime('%d.%m.%Y')} {late.strftime('%d.%m.%Y')}",
            "date_next_early": early,
            "date_next_late": late,
            "status": "DUE" if in_window else "ESTIMATED",
            "docking_required": dock_req,
            "priority": prio,
            "is_due": in_window,
            "source": "regulatory_fallback",
        })
    return pd.DataFrame(rows)


def analyze_class_surveys(
    payload: dict[str, Any] | None,
    *,
    last_major_docking: pd.Timestamp | None = None,
    reference: pd.Timestamp | None = None,
) -> dict[str, Any]:
    """Анализ классовых освидетельствований + прогноз обязательного докования."""
    ref = reference or pd.Timestamp.now(tz="UTC")
    empty = {
        "imo": None,
        "rmrs_status": None,
        "class_notation": None,
        "build_date": None,
        "surveys_source": "none",
        "surveys_total": 0,
        "surveys_due": 0,
        "next_class_survey_type": None,
        "next_class_survey_code": None,
        "next_class_survey_date": None,
        "next_class_survey_deadline": None,
        "next_mandatory_docking_class": None,
        "class_survey_status": "unknown",
        "months_to_class_survey": None,
    }
    if not payload:
        return empty

    vd = payload.get("vessel_data") or {}
    imo = str(payload.get("imo") or vd.get("IMO") or vd.get("Номер ИМО") or "").strip() or None
    notation = (
        vd.get("RS Class notation")
        or vd.get("Символ класса")
        or ""
    ).strip()
    build = _parse_build_date(vd)
    status = str(payload.get("status", "")).strip()

    df = surveys_from_payload(payload)
    source = "rmrs_surveys"
    if df.empty:
        df = estimate_regulatory_schedule(last_major_docking, build, reference=ref)
        source = "regulatory_fallback" if not df.empty else "none"

    if df.empty:
        return {**empty, "imo": imo, "rmrs_status": status, "class_notation": notation or None,
                "build_date": build.date() if build is not None and pd.notna(build) else None}

    # ближайшее освидетельствование (срочное: просрочено / в окне / ближайшее)
    df = df[df["date_next_early"].notna()].copy()
    due_df = df[df["is_due"] | (df["date_next_late"] < ref)]
    pending = df[~df.index.isin(due_df.index)] if not due_df.empty else df
    if not due_df.empty:
        due_df = due_df.sort_values(["date_next_early"])
        next_row = due_df.iloc[0]
    elif not pending.empty:
        next_row = pending.sort_values("date_next_early").iloc[0]
    else:
        next_row = df.sort_values("date_next_early").iloc[0]

    # Обязательное докование: ближайшее ВПЕРЁД. Судно в море ⇒ класс действителен
    # ⇒ просроченных докований быть не может. Если в данных РС дата докования уже
    # в прошлом, значит оно состоялось — прокатываем на следующий цикл (2.5 г.).
    dock_all = df[df["docking_required"] & df["date_next_early"].notna()].copy()
    if not dock_all.empty:
        step = pd.Timedelta(days=REGULATORY_INTERVALS_YEARS["intermediate"] * 365.25)
        def _roll(ts):
            ts = pd.Timestamp(ts)
            g = 0
            while ts < ref and g < 50:
                ts = ts + step
                g += 1
            return ts
        dock_all["dock_fwd"] = dock_all["date_next_early"].apply(_roll)
        dock_df = dock_all.sort_values("dock_fwd")
        next_dock = dock_df.iloc[0]
    else:
        dock_df = dock_all
        next_dock = None

    def _to_date(ts) -> date | None:
        if ts is None or (isinstance(ts, float) and np.isnan(ts)):
            return None
        if pd.isna(ts):
            return None
        return pd.Timestamp(ts).date()

    next_early = _to_date(next_row.get("date_next_early"))
    next_late = _to_date(next_row.get("date_next_late"))
    # для докования используем прокатанную вперёд дату
    dock_early = _to_date(next_dock["dock_fwd"]) if next_dock is not None else None

    # Окно обязательного докования — основной сигнал для продаж (forward-looking).
    months_to = None
    if dock_early:
        months_to = round((pd.Timestamp(dock_early, tz="UTC") - ref).total_seconds() / 86400 / 30.44, 1)
    elif next_early:
        months_to = round((pd.Timestamp(next_early, tz="UTC") - ref).total_seconds() / 86400 / 30.44, 1)

    # Статус без «overdue»: судно в эксплуатации ⇒ класс действителен.
    survey_status = "scheduled"
    if months_to is not None:
        if months_to <= 3:
            survey_status = "due_soon"
        elif months_to <= 12:
            survey_status = "approaching"

    return {
        "imo": imo,
        "rmrs_status": status,
        "class_notation": notation or None,
        "build_date": build.date() if build is not None and pd.notna(build) else None,
        "surveys_source": source,
        "surveys_total": len(df),
        "surveys_due": int((df["is_due"] | (df["date_next_late"] < ref)).sum()),
        "next_class_survey_type": next_row.get("survey_label"),
        "next_class_survey_code": next_row.get("survey_code"),
        "next_class_survey_date": next_early,
        "next_class_survey_deadline": next_late,
        "next_mandatory_docking_class": dock_early,
        "class_survey_status": survey_status,
        "months_to_class_survey": months_to,
        "_surveys_df": df,
    }


def merge_maintenance_forecast(
    gfw: dict[str, Any],
    cls: dict[str, Any],
) -> dict[str, Any]:
    """Объединить операционный цикл GFW и классовые требования РС."""
    ref = pd.Timestamp.now(tz="UTC")
    candidates: list[tuple[str, pd.Timestamp, str]] = []

    # Все кандидаты прокатаны вперёд в своих модулях → только будущие даты.
    op_date = gfw.get("predicted_next_docking")
    if op_date:
        d = pd.Timestamp(op_date, tz="UTC")
        if d >= ref:
            candidates.append(("operational_cycle", d, gfw.get("sales_window", "unknown")))

    # Класс: для окна продаж значимо только обязательное ДОКОВАНИЕ
    # (ежегодные осмотры на плаву окно не формируют — они идут отдельным полем).
    d = cls.get("next_mandatory_docking_class")
    if d:
        ts = pd.Timestamp(d, tz="UTC")
        if ts >= ref:
            candidates.append(("class_docking", ts, cls.get("class_survey_status", "scheduled")))

    if not candidates:
        return {
            "predicted_next_maintenance": None,
            "maintenance_driver": "unknown",
            "sales_window_combined": gfw.get("sales_window", "unknown"),
            "months_to_next_maintenance": None,
        }

    candidates.sort(key=lambda x: x[1])
    best_driver, best_date, best_status = candidates[0]
    drivers = sorted({c[0] for c in candidates if abs((c[1] - best_date).days) <= 120})
    maintenance_driver = drivers[0] if len(drivers) == 1 else "both" if len(drivers) > 1 else best_driver

    months = round((best_date - ref).total_seconds() / 86400 / 30.44, 1)

    # комбинированный статус по сроку до ближайшего окна (без «overdue»)
    if months <= 3:
        combined = "due_soon"
    elif months <= 12:
        combined = "approaching"
    else:
        combined = "scheduled"

    return {
        "predicted_next_maintenance": best_date.date(),
        "maintenance_driver": maintenance_driver,
        "sales_window_combined": combined,
        "months_to_next_maintenance": months,
    }


def enrich_vessel_profile(
    profile: dict[str, Any],
    vessel: str,
    last_major_docking: pd.Timestamp | None,
    *,
    imo: str | None = None,
    rmrs_dir: str | None = None,
    imo_registry_path: str | None = None,
) -> tuple[dict[str, Any], pd.DataFrame]:
    """Дополнить профиль судна полями классового прогноза. Возвращает (profile, surveys_df)."""
    reg = load_imo_registry(imo_registry_path)
    imo = imo or resolve_imo(vessel, reg)
    payload = load_rmrs_payload(imo, rmrs_dir) if imo else None

    cls = analyze_class_surveys(payload, last_major_docking=last_major_docking)
    surveys_df = cls.pop("_surveys_df", pd.DataFrame())
    merged = merge_maintenance_forecast(profile, cls)

    out = {**profile}
    out.update({k: v for k, v in cls.items() if not str(k).startswith("_")})
    out.update(merged)
    out["imo"] = imo
    return out, surveys_df
