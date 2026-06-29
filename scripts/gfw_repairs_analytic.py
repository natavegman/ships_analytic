"""
GFW Repair & Docking Intelligence — анализ ремонтов и докований по событиям GFW.

Идея: в выгрузке событий GFW (Events: fishing / port_visit / encounter за 2012–now)
длительность port_visit напрямую выдаёт характер стоянки. Длинные заходы в порт —
это ремонты, докования, межрейсовый отстой или зимний layup. Заходы в известные
судоремонтные порты (Далянь, Пусан, Владивосток, Находка) — почти всегда верфь.

Это даёт коммерчески ценную картину:
  - когда судно становилось в ремонт/док, где и насколько;
  - на какие верфи уходит бизнес (зарубеж vs РФ), сколько судо-дней;
  - оценку цикла докования и ПРОГНОЗ следующего окна ремонта — то самое окно,
    когда судовладельцу нужно оборудование (момент для продажи);
  - годовую доступность судна (промысел / порт / ремонт / переходы).

Вход:
  --input <events.csv>        один файл выгрузки событий (как с карты GFW)
  --input-dir <dir>           папка с *events*.csv (флот)
Имя судна берётся из колонки или из имени файла ("ALEXANDR BELYAKOV(RUS)-events-...").

Выход (в --outdir, по умолчанию output/gfw_repairs/):
  repairs.csv                — каждый ремонт/докование (период, порт, верфь, тип)
  port_visits.csv            — все заходы в порт с классификацией
  yard_intelligence.csv      — верфи: сколько судов, судо-дней, РФ/зарубеж
  vessel_docking_profile.csv — по судну: последнее докование, цикл, прогноз окна
  yearly_availability.csv    — по судну и году: дни промысла/порта/ремонта/моря

Запуск:
  .venv/bin/python scripts/gfw_repairs_analytic.py --input "data/ALEXANDR ...csv"
  .venv/bin/python scripts/gfw_repairs_analytic.py --input-dir data/gfw_events
"""

from __future__ import annotations

import argparse
import glob
import math
import os
import re

import numpy as np
import pandas as pd

import gfw_class_survey as cls_survey

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# =========================================================================
# КЛАССИФИКАЦИЯ ЗАХОДОВ В ПОРТ ПО ДЛИТЕЛЬНОСТИ (дни)
# =========================================================================
# < 1д   — бункеровка/перегруз/быстрый заход
# 1–7д   — обычная стоянка (выгрузка, снабжение, смена экипажа)
# 7–30д  — обслуживание / мелкий ремонт
# 30–150д— ремонт / докование / рефит
# > 150д — длительный отстой / крупная модернизация / layup
PORT_CLASSES = [
    (0.0, 1.0, "bunker_call"),
    (1.0, 7.0, "port_call"),
    (7.0, 30.0, "maintenance"),
    (30.0, 150.0, "repair_docking"),
    (150.0, 100000.0, "major_refit_or_layup"),
]

# Порог, начиная с которого считаем заход «ремонтом/докованием»
REPAIR_MIN_DAYS = 14.0

# Известные судоремонтные порты ДВ-бассейна и не только.
# name (как в portVisitName, верхний регистр) -> (краткое описание, страна)
KNOWN_YARDS = {
    "DALIAN": ("Dalian shipyards (CHN) — крупные доки, рефиты", "CHN"),
    "BUSAN": ("Busan (KOR) — судоремонт/докование", "KOR"),
    "BUSAN NEW PORT": ("Busan New Port (KOR)", "KOR"),
    "VLADIVOSTOK": ("Владивосток — Дальзавод/Славянский СРЗ", "RUS"),
    "NAKHODKA": ("Находка — НСРЗ", "RUS"),
    "KORSAKOV": ("Корсаков СРЗ (Сахалин)", "RUS"),
    "PETROPAVLOVSK": ("Петропавловск-Камчатский СРВ", "RUS"),
    "SLAVYANKA": ("Славянский СРЗ", "RUS"),
    "QINGDAO": ("Qingdao (CHN) — судоремонт", "CHN"),
    "ZHOUSHAN": ("Zhoushan (CHN) — крупные доки", "CHN"),
}


# Очень длинная стоянка (дней) — скорее отстой/арест/простой, чем активный ремонт.
LAYUP_MIN_DAYS = 180.0
# Радиус (км) для отнесения захода к региону верфи (координаты GFW — уровень рейда).
YARD_RADIUS_KM = 30.0

# Кураторские координаты ключевых судоремонтных центров ДВ-бассейна и не только.
# GFW отдаёт координаты захода на уровне якорной стоянки, поэтому радиус большой.
# name -> (lat, lon, описание)
CURATED_YARDS = {
    "Находка (НСРЗ)": (42.79, 132.90, "Находкинский СРЗ"),
    "Владивосток (Дальзавод)": (43.11, 131.89, "Дальзавод"),
    "Владивосток (рейд/Большой Камень)": (43.05, 132.02, "Владивостокский рейд / Большой Камень"),
    "Звезда (Большой Камень)": (43.12, 132.34, "ССК Звезда"),
    "Славянский СРЗ": (42.86, 131.39, "Славянский СРЗ"),
    "Корсаков (СРЗ)": (46.63, 142.78, "Корсаковский СРЗ"),
    "Петропавловск-Камчатский (СРВ)": (53.02, 158.65, "Петропавловская судоверфь"),
    "Далянь (Dalian)": (38.93, 121.63, "Dalian shipyards"),
    "Пусан (Busan)": (35.05, 129.03, "Busan repair cluster (Yeongdo)"),
    "Пусан Новый порт": (35.08, 128.81, "Busan New Port"),
    "Циндао (Qingdao)": (36.07, 120.32, "Qingdao yards"),
    "Чжоушань (Zhoushan)": (29.78, 122.09, "Zhoushan yards"),
}


def classify_duration(days: float) -> str:
    for lo, hi, label in PORT_CLASSES:
        if lo <= days < hi:
            return label
    return "unknown"


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def load_shipyards(extra_csv: str | None = None) -> list[tuple[str, float, float]]:
    """Список верфей (name, lat, lon): кураторские + TrustedDocks (если есть)."""
    yards: list[tuple[str, float, float]] = [
        (name, lat, lon) for name, (lat, lon, _) in CURATED_YARDS.items()
    ]
    path = extra_csv or os.path.join(
        ROOT, "data", "reference", "trusteddocks_shipyards_ru_kr_cn.csv"
    )
    if os.path.exists(path):
        try:
            ref = pd.read_csv(path)
            for _, r in ref.iterrows():
                try:
                    lat, lon = float(r["lat"]), float(r["lon"])
                except (TypeError, ValueError):
                    continue
                if not (math.isnan(lat) or math.isnan(lon)):
                    yards.append((str(r.get("name", "")).strip(), lat, lon))
        except Exception:
            pass
    return yards


def nearest_yard(
    lat: float, lon: float, yards: list[tuple[str, float, float]]
) -> tuple[str, float]:
    """Ближайшая верфь и расстояние в км. ('', inf) если координат нет/список пуст."""
    if lat is None or lon is None or (isinstance(lat, float) and math.isnan(lat)):
        return "", float("inf")
    best_name, best_km = "", float("inf")
    for name, ylat, ylon in yards:
        d = _haversine_km(lat, lon, ylat, ylon)
        if d < best_km:
            best_name, best_km = name, d
    return best_name, best_km


def vessel_name_from_filename(path: str) -> str:
    base = os.path.basename(path)
    # "ALEXANDR BELYAKOV(RUS)-events-2012-...csv"
    m = re.split(r"-events-", base)
    name = m[0] if m else base
    name = re.sub(r"\.csv$", "", name, flags=re.IGNORECASE)
    return name.strip()


def load_events(path: str) -> tuple[pd.DataFrame, str]:
    df = pd.read_csv(path)
    df["start"] = pd.to_datetime(df["start"], utc=True, errors="coerce")
    df["end"] = pd.to_datetime(df["end"], utc=True, errors="coerce")
    df = df.dropna(subset=["start", "end"]).copy()
    df["dur_days"] = (df["end"] - df["start"]).dt.total_seconds() / 86400
    # имя судна
    name = ""
    for col in ("vesselName", "shipName", "vessel_name"):
        if col in df.columns and df[col].notna().any():
            name = str(df[col].dropna().iloc[0])
            break
    if not name:
        name = vessel_name_from_filename(path)
    return df, name


# =========================================================================
# АНАЛИЗ ОДНОГО СУДНА
# =========================================================================
def classify_event_kind(row) -> str:
    """
    Честная таксономия захода в порт по совокупности признаков GFW:
      dry_dock_repair — у верфи/судоремонтного порта, стоянка >= 3 дней
      extended_layup  — очень долгая стоянка НЕ у верфи (отстой/арест/простой)
      repair_docking  — длинная стоянка (>= порога) без явной верфи
      unloading_call  — короткая стоянка в порту (выгрузка/снабжение)
      bunker_call     — < 1 дня (бункеровка/быстрый заход)
    """
    d = row["dur_days"]
    at_yard = bool(row["at_repair_location"])
    if d < 1.0:
        return "bunker_call"
    # очень длинная стоянка = отстой/простой (даже у верфи это не рутинное докование)
    if d >= LAYUP_MIN_DAYS:
        return "extended_layup"
    if at_yard and d >= 3.0:
        return "dry_dock_repair"
    if d >= REPAIR_MIN_DAYS:
        return "repair_docking"
    return "unloading_call"


def analyze_vessel(
    df: pd.DataFrame,
    vessel: str,
    yards: list[tuple[str, float, float]],
    *,
    rmrs_dir: str | None = None,
) -> dict:
    pv = df[df["type"] == "port_visit"].copy().sort_values("start")
    fishing = df[df["type"] == "fishing"].copy()
    enc = df[df["type"] == "encounter"].copy()

    pv["port"] = pv["portVisitName"].fillna("").str.upper().str.strip()
    pv["port_flag"] = pv["portVisitFlag"].fillna("")
    pv["class"] = pv["dur_days"].apply(classify_duration)
    pv["is_yard"] = pv["port"].isin(KNOWN_YARDS.keys())
    pv["yard_note"] = pv["port"].map(lambda p: KNOWN_YARDS.get(p, ("", ""))[0])

    # геопривязка к верфям (координаты GFW — уровень рейда → радиус большой)
    nm, km = [], []
    for r in pv.itertuples():
        n, d = nearest_yard(getattr(r, "latitude", None), getattr(r, "longitude", None), yards)
        nm.append(n)
        km.append(round(d, 1) if d != float("inf") else None)
    pv["nearest_yard"] = nm
    pv["yard_distance_km"] = km
    # «у ремонтной локации» = известный судоремонтный порт ИЛИ близко к верфи
    pv["at_repair_location"] = pv["is_yard"] | pv["yard_distance_km"].apply(
        lambda x: x is not None and x <= YARD_RADIUS_KM
    )

    pv["event_kind"] = pv.apply(classify_event_kind, axis=1)
    # ремонт/докование = докование у верфи ИЛИ длинная стоянка (но не чистый layup)
    pv["is_repair"] = pv["event_kind"].isin(["dry_dock_repair", "repair_docking"])

    repairs = pv[pv["is_repair"]].copy()

    # --- профиль докования ---
    span_start = df["start"].min()
    span_end = df["end"].max()
    span_years = (span_end - span_start).total_seconds() / 86400 / 365.25

    # «крупные» докования для оценки цикла: докование у верфи (>=20 дн)
    # либо длинный ремонт (>=45 дн). Отсекает частые мелкие стоянки и
    # чистый отстой (extended_layup в цикл не входит — это не плановый ремонт).
    major = repairs[
        (
            (repairs["event_kind"] == "dry_dock_repair")
            & (repairs["dur_days"] >= 20.0)
        )
        | (repairs["dur_days"] >= 45.0)
    ].sort_values("start")

    cycle_days = np.nan
    if len(major) >= 2:
        gaps = major["start"].diff().dt.total_seconds().dropna() / 86400
        cycle_days = float(gaps.median())

    last_dock = major["start"].max() if not major.empty else (
        repairs["start"].max() if not repairs.empty else pd.NaT
    )
    next_window = pd.NaT
    months_to_next = np.nan
    sales_status = "unknown"
    if pd.notna(last_dock) and not np.isnan(cycle_days):
        now = pd.Timestamp.now(tz="UTC")
        # Сезонное межрейсовое обслуживание — повторяющийся цикл. Если расчётная
        # дата уже в прошлом, значит обслуживание состоялось (судно в море),
        # поэтому прокатываем окно вперёд к ближайшему будущему сроку.
        next_window = last_dock + pd.Timedelta(days=cycle_days)
        guard = 0
        while next_window < now and guard < 50:
            next_window += pd.Timedelta(days=cycle_days)
            guard += 1
        months_to_next = (next_window - now).total_seconds() / 86400 / 30.44
        if months_to_next <= 3:
            sales_status = "due_soon"          # окно в ближайшем квартале → горячий лид
        elif months_to_next <= 12:
            sales_status = "approaching"       # окно в пределах года
        else:
            sales_status = "later"

    # --- годовая доступность ---
    yearly = compute_yearly_availability(pv, fishing, vessel, span_start, span_end)

    total_port_days = pv["dur_days"].sum()
    total_repair_days = repairs["dur_days"].sum()
    total_fishing_days = fishing["dur_days"].sum()
    layup = pv[pv["event_kind"] == "extended_layup"]
    total_layup_days = layup["dur_days"].sum()
    dry_dock = pv[pv["event_kind"] == "dry_dock_repair"]

    profile = {
        "vessel": vessel,
        "period_start": span_start.date() if pd.notna(span_start) else None,
        "period_end": span_end.date() if pd.notna(span_end) else None,
        "years_observed": round(span_years, 1),
        "port_visits": len(pv),
        "repairs_detected": len(repairs),
        "dry_dock_repairs": len(dry_dock),
        "major_dockings": len(major),
        "extended_layups": len(layup),
        "total_port_days": round(total_port_days, 1),
        "total_repair_days": round(total_repair_days, 1),
        "total_layup_days": round(total_layup_days, 1),
        "total_fishing_days": round(total_fishing_days, 1),
        "fishing_events": len(fishing),
        "encounters": len(enc),
        "repair_share_%": round(100 * total_repair_days / max(1, (span_end - span_start).days), 1),
        "docking_cycle_months": round(cycle_days / 30.44, 1) if not np.isnan(cycle_days) else None,
        "last_docking": last_dock.date() if pd.notna(last_dock) else None,
        "predicted_next_docking": next_window.date() if pd.notna(next_window) else None,
        "months_to_next_docking": round(months_to_next, 1) if not np.isnan(months_to_next) else None,
        "sales_window": sales_status,
    }
    last_major_dock = major["start"].max() if not major.empty else last_dock
    profile, class_surveys = cls_survey.enrich_vessel_profile(
        profile, vessel, last_major_dock, rmrs_dir=rmrs_dir,
    )
    if not class_surveys.empty:
        class_surveys = class_surveys.assign(vessel=vessel, imo=profile.get("imo"))
    return {
        "profile": profile,
        "port_visits": pv,
        "repairs": repairs,
        "yearly": yearly,
        "class_surveys": class_surveys,
        "vessel": vessel,
    }


def compute_yearly_availability(
    pv: pd.DataFrame, fishing: pd.DataFrame, vessel: str, span_start, span_end
) -> pd.DataFrame:
    """Дни промысла / в порту / в ремонте / отстое / в море по годам (приближённо).

    pv — уже классифицированные заходы (с колонками is_repair, event_kind).
    """
    rows = []
    repair_pv = pv[pv["is_repair"]]
    layup_pv = pv[pv["event_kind"] == "extended_layup"]

    def overlap_days(s, e, y0, y1):
        a = max(s, y0)
        b = min(e, y1)
        return max(0.0, (b - a).total_seconds() / 86400)

    years = range(span_start.year, span_end.year + 1)
    for yr in years:
        y0 = pd.Timestamp(f"{yr}-01-01", tz="UTC")
        y1 = pd.Timestamp(f"{yr+1}-01-01", tz="UTC")
        year_days = (min(y1, pd.Timestamp.now(tz="UTC")) - y0).total_seconds() / 86400
        if year_days <= 0:
            continue
        port_d = sum(overlap_days(r.start, r.end, y0, y1) for r in pv.itertuples())
        repair_d = sum(overlap_days(r.start, r.end, y0, y1) for r in repair_pv.itertuples())
        layup_d = sum(overlap_days(r.start, r.end, y0, y1) for r in layup_pv.itertuples())
        fish_d = sum(overlap_days(r.start, r.end, y0, y1) for r in fishing.itertuples())
        sea_d = max(0.0, year_days - port_d)  # вне порта = в море (промысел+переходы)
        rows.append({
            "vessel": vessel,
            "year": yr,
            "days_observed": round(year_days, 1),
            "fishing_days": round(fish_d, 1),
            "at_sea_days": round(sea_d, 1),
            "in_port_days": round(port_d, 1),
            "repair_days": round(repair_d, 1),
            "layup_days": round(layup_d, 1),
            "availability_%": round(100 * (year_days - repair_d - layup_d) / year_days, 1),
            "fishing_intensity_%": round(100 * fish_d / year_days, 1),
        })
    return pd.DataFrame(rows)


# =========================================================================
# СВОДКА ПО ВЕРФЯМ (флот)
# =========================================================================
def build_yard_intelligence(all_repairs: pd.DataFrame) -> pd.DataFrame:
    if all_repairs.empty:
        return pd.DataFrame()
    g = (
        all_repairs.groupby(["port", "port_flag"])
        .agg(
            repairs=("dur_days", "size"),
            vessels=("vessel", "nunique"),
            total_days=("dur_days", "sum"),
            avg_days=("dur_days", "mean"),
            max_days=("dur_days", "max"),
            last_visit=("start", "max"),
        )
        .reset_index()
        .sort_values("total_days", ascending=False)
    )
    g["is_known_yard"] = g["port"].isin(KNOWN_YARDS.keys())
    g["yard_note"] = g["port"].map(lambda p: KNOWN_YARDS.get(p, ("", ""))[0])
    g["foreign"] = g["port_flag"].ne("RUS") & g["port_flag"].ne("")
    g["total_days"] = g["total_days"].round(1)
    g["avg_days"] = g["avg_days"].round(1)
    g["last_visit"] = pd.to_datetime(g["last_visit"]).dt.date
    return g


# =========================================================================
# MAIN
# =========================================================================
def main() -> int:
    global REPAIR_MIN_DAYS
    ap = argparse.ArgumentParser(description="GFW repair & docking intelligence")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--input", help="CSV выгрузки событий одного судна")
    src.add_argument("--input-dir", help="Папка с *events*.csv (флот)")
    ap.add_argument("--outdir", default="output/gfw_repairs")
    ap.add_argument("--repair-min-days", type=float, default=REPAIR_MIN_DAYS,
                    help=f"Порог ремонта в днях (по умолчанию {REPAIR_MIN_DAYS})")
    ap.add_argument("--shipyards", help="CSV справочника верфей (name,lat,lon)")
    ap.add_argument("--rmrs-dir", default="output",
                    help="Папка с rmrs_events_<IMO>.json для классового прогноза")
    args = ap.parse_args()

    REPAIR_MIN_DAYS = args.repair_min_days
    yards = load_shipyards(args.shipyards)
    print(f"Справочник верфей: {len(yards)} точек (радиус привязки {YARD_RADIUS_KM:.0f} км)")

    if args.input:
        files = [args.input]
    else:
        files = sorted(glob.glob(os.path.join(args.input_dir, "*events*.csv")))
        if not files:
            files = sorted(glob.glob(os.path.join(args.input_dir, "*.csv")))
    if not files:
        print("Не найдено CSV с событиями.")
        return 1

    os.makedirs(args.outdir, exist_ok=True)
    profiles, all_pv, all_repairs, all_yearly, all_class_surveys = [], [], [], [], []

    for path in files:
        df, vessel = load_events(path)
        if df.empty:
            print(f"[skip] пустой файл: {path}")
            continue
        res = analyze_vessel(df, vessel, yards, rmrs_dir=args.rmrs_dir)
        profiles.append(res["profile"])
        all_pv.append(res["port_visits"].assign(vessel=vessel))
        all_repairs.append(res["repairs"].assign(vessel=vessel))
        all_yearly.append(res["yearly"])
        if not res.get("class_surveys", pd.DataFrame()).empty:
            all_class_surveys.append(res["class_surveys"])
        p = res["profile"]
        print(f"\n=== {vessel} ===")
        print(f"  период: {p['period_start']} → {p['period_end']} ({p['years_observed']} лет)")
        if p.get("imo"):
            print(f"  IMO: {p['imo']} | класс: {p.get('class_notation', '—')}")
        print(f"  заходов в порт: {p['port_visits']}, ремонтов/докований: "
              f"{p['repairs_detected']} (докований у верфи: {p['dry_dock_repairs']}, "
              f"крупных: {p['major_dockings']}, отстой: {p['extended_layups']})")
        print(f"  суммарно в ремонте: {p['total_repair_days']} дн "
              f"({p['repair_share_%']}% времени) | в отстое: {p['total_layup_days']} дн")
        if p.get("docking_cycle_months"):
            print(f"  цикл докования (GFW): ~{p['docking_cycle_months']} мес | "
                  f"прогноз: {p['predicted_next_docking']} ({p['sales_window']})")
        if p.get("next_class_survey_type"):
            src = p.get("surveys_source", "")
            print(f"  класс РС ({src}): {p['next_class_survey_type']} | "
                  f"до {p.get('next_class_survey_deadline') or p.get('next_class_survey_date')} "
                  f"({p.get('class_survey_status')})")
        if p.get("predicted_next_maintenance"):
            print(f"  ИТОГО прогноз обслуживания: {p['predicted_next_maintenance']} "
                  f"({p.get('maintenance_driver')}, {p.get('sales_window_combined')})")

    if not profiles:
        print("Нет данных для анализа.")
        return 1

    # сборка и запись
    pv_all = pd.concat(all_pv, ignore_index=True) if all_pv else pd.DataFrame()
    rep_all = pd.concat(all_repairs, ignore_index=True) if all_repairs else pd.DataFrame()
    yearly_all = pd.concat(all_yearly, ignore_index=True) if all_yearly else pd.DataFrame()
    class_all = pd.concat(all_class_surveys, ignore_index=True) if all_class_surveys else pd.DataFrame()
    prof_df = pd.DataFrame(profiles)

    pv_cols = ["vessel", "start", "end", "dur_days", "port", "port_flag",
               "class", "event_kind", "is_yard", "is_repair", "at_repair_location",
               "nearest_yard", "yard_distance_km", "yard_note",
               "latitude", "longitude", "voyage"]
    pv_out = pv_all[[c for c in pv_cols if c in pv_all.columns]].copy()
    pv_out["start"] = pd.to_datetime(pv_out["start"]).dt.strftime("%Y-%m-%d %H:%M")
    pv_out["end"] = pd.to_datetime(pv_out["end"]).dt.strftime("%Y-%m-%d %H:%M")
    pv_out["dur_days"] = pv_out["dur_days"].round(2)
    pv_out.to_csv(os.path.join(args.outdir, "port_visits.csv"), index=False)

    rep_out = pv_out[pv_out["is_repair"]] if "is_repair" in pv_out.columns else pv_out
    rep_out.sort_values(["vessel", "start"]).to_csv(
        os.path.join(args.outdir, "repairs.csv"), index=False
    )

    yard = build_yard_intelligence(rep_all)
    yard.to_csv(os.path.join(args.outdir, "yard_intelligence.csv"), index=False)
    yearly_all.to_csv(os.path.join(args.outdir, "yearly_availability.csv"), index=False)
    prof_df.to_csv(os.path.join(args.outdir, "vessel_docking_profile.csv"), index=False)
    if not class_all.empty:
        cs_cols = ["vessel", "imo", "survey_type", "survey_code", "survey_name", "survey_label",
                   "date_last_raw", "date_next_raw", "status", "docking_required", "is_due"]
        cs_out = class_all[[c for c in cs_cols if c in class_all.columns]].copy()
        for c in ("date_next_early", "date_next_late"):
            if c in class_all.columns:
                cs_out[c] = pd.to_datetime(class_all[c]).dt.strftime("%Y-%m-%d")
        cs_out.to_csv(os.path.join(args.outdir, "class_surveys.csv"), index=False)

    # топ-сводка верфей
    if not yard.empty:
        print("\n=== Верфи по судо-дням ремонта ===")
        show = yard[["port", "port_flag", "vessels", "repairs", "total_days", "foreign"]].head(10)
        print(show.to_string(index=False))

    # горячие лиды (операционный цикл + класс РС)
    sw_col = "sales_window_combined" if "sales_window_combined" in prof_df.columns else "sales_window"
    hot = prof_df[prof_df[sw_col].isin(["approaching", "overdue", "due"])]
    if not hot.empty:
        print("\n=== Горячие лиды (обслуживание близко/просрочено) ===")
        cols = ["vessel", "imo", "last_docking", "predicted_next_docking",
                "next_class_survey_type", "next_class_survey_date",
                "predicted_next_maintenance", "maintenance_driver", sw_col]
        cols = [c for c in cols if c in hot.columns]
        print(hot[cols].to_string(index=False))

    print(f"\nРезультаты сохранены в: {args.outdir}/")
    print("  repairs.csv, port_visits.csv, yard_intelligence.csv,")
    print("  vessel_docking_profile.csv, yearly_availability.csv, class_surveys.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
