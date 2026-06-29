"""
Набор груза → перегруз: связка официального вылова (АДМ / сводки) с GFW.

Зачем
-----
GFW показывает АКТИВНОСТЬ (сколько судно тралит / стоит / перегружает), но не
показывает СКОЛЬКО оно поймало. Официальная сводка вылова (напр. «Вылов минтая
по судам» АДМ) даёт тоннаж. Связав их, получаем то, чего нет ни в одном
источнике по отдельности:

  • вылов на промысловый день (т/сут промысла)   — реальная продуктивность;
  • вылов на сутки в море (т/сут)                 — для прогноза набора груза;
  • набор груза → через сколько суток после начала набора судно идёт на перегруз
        days_to_fill = вместимость_трюма / (вылов на сутки в море);
  • верификация по GFW: фактическая каденция перегрузов (медиана календарных
        дней между encounter-перегрузами) — независимая проверка модели.

Почему это важно для типов судов
--------------------------------
БМРТ морозят Б/Г и сдают на перегруз → каденция короткая, набор груза = трюм.
Плавзавод/РТМКС (Царица) работает иначе (выпуск НР/иной продукции, иная
логистика), поэтому сравнивать набор груза БМРТ и завода напрямую нельзя —
тип судна берётся из справочника и используется для сегментации.

Если фактическая вместимость трюма (hold_capacity_t) в справочнике не задана,
берётся эмпирическая оценка «типового набора за рейс»:
        implied_load = вылов_на_сутки_в_море × фактическая_каденция_перегрузов.

Справочник: data/reference/nbamr_vessel_catch.csv
  gfw_name, display_name, vessel_type, hold_capacity_t,
  catch_total_t, catch_pollock_t, catch_as_of, season_start
"""

from __future__ import annotations

import os
import re

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CATCH_REF = os.path.join(ROOT, "data", "reference", "nbamr_vessel_catch.csv")

# Тип судна → класс сравнения (нельзя смешивать БМРТ и плавзавод).
TRAWLER_TYPES = {"БМРТ", "БАТМ", "РТМКС", "СРТМ", "МРКТ", "СТР"}
FACTORY_HINTS = ("ЗАВОД", "ПЛАВЗАВОД", "FACTORY")


def _norm(name: str) -> str:
    s = re.sub(r"\(RUS\)$", "", str(name or ""), flags=re.I).strip().upper()
    return re.sub(r"\s+", " ", s)


def load_catch_reference(path: str | None = None) -> dict[str, dict]:
    """Загрузить справочник вылова. Ключ — нормализованное gfw_name."""
    path = path or DEFAULT_CATCH_REF
    if not os.path.exists(path):
        return {}
    df = pd.read_csv(path)
    out: dict[str, dict] = {}
    for _, r in df.iterrows():
        rec = r.to_dict()
        out[_norm(r.get("gfw_name", ""))] = rec
    return out


def lookup_catch(vessel: str, ref: dict[str, dict]) -> dict | None:
    n = _norm(vessel)
    if n in ref:
        return ref[n]
    for k, rec in ref.items():
        if k and (k in n or n in k):
            return rec
    return None


def peer_group(vessel_type: str | None) -> str:
    """Группа сравнения: trawler | factory | other."""
    if not vessel_type:
        return "trawler"
    t = str(vessel_type).upper()
    if any(h in t for h in FACTORY_HINTS):
        return "factory"
    code = t.split()[0]
    if "СТР" in t and "РТМКС" in t:  # Царица: СТР/РТМКС-завод
        return "factory"
    if code in TRAWLER_TYPES:
        return "trawler"
    return "other"


def _season_window(rec: dict) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
    def _ts(x):
        if not x or (isinstance(x, float) and np.isnan(x)):
            return None
        try:
            return pd.Timestamp(str(x), tz="UTC")
        except Exception:
            return None
    return _ts(rec.get("season_start")), _ts(rec.get("catch_as_of"))


def _season_days_metric(yearly: pd.DataFrame, col: str, year: int) -> float:
    if yearly is None or yearly.empty:
        return float("nan")
    row = yearly[yearly["year"] == year]
    if row.empty:
        return float("nan")
    return float(row[col].iloc[0])


# Межсезонные паузы (зимний простой, ремонт) не относятся к циклу набора груза.
INSEASON_MAX_GAP_DAYS = 90.0


def transship_cadence_days(enc: pd.DataFrame) -> tuple[float, pd.Timestamp | None]:
    """Каденция набора груза = медиана внутрисезонных интервалов между перегрузами.

    Берётся вся история (обычно 150+ перегрузов → устойчивая оценка), межсезонные
    паузы (> INSEASON_MAX_GAP_DAYS) отбрасываются. Возвращает (каденция, дата
    последнего перегруза)."""
    if enc is None or enc.empty or "kind" not in enc.columns:
        return float("nan"), None
    ts = enc[enc["kind"] == "transshipment"].copy()
    if ts.empty:
        return float("nan"), None
    ts["start"] = pd.to_datetime(ts["start"], utc=True)
    ts = ts.sort_values("start")
    last = ts["start"].max()
    gaps = ts["start"].diff().dt.total_seconds().dropna() / 86400
    gaps = gaps[(gaps > 1.0) & (gaps <= INSEASON_MAX_GAP_DAYS)]
    return (float(gaps.median()) if not gaps.empty else float("nan")), last


def analyze_cargo(
    vessel: str,
    enc: pd.DataFrame,
    yearly: pd.DataFrame,
    ref: dict[str, dict],
) -> dict:
    """Метрики продуктивности и набора груза для судна.

    enc    — встречи с колонкой kind (из classify_encounters).
    yearly — судно×год из compute_fleet_yearly (fishing_days, at_sea_days).
    ref    — справочник вылова.
    """
    empty = {
        "vessel_type": None,
        "peer_group": "trawler",
        "catch_total_t": None,
        "catch_pollock_t": None,
        "catch_as_of": None,
        "season_fishing_days": None,
        "season_at_sea_days": None,
        "catch_per_fishing_day_t": None,
        "catch_per_seaday_t": None,
        "hold_capacity_t": None,
        "offload_cadence_days": None,
        "implied_load_per_offload_t": None,
        "effective_hold_t": None,
        "days_to_fill_hold": None,
        "last_transshipment": None,
        "next_offload_eta": None,
        "trips_per_season": None,
    }
    rec = lookup_catch(vessel, ref)
    cadence, last_ts = transship_cadence_days(enc)
    if rec is None:
        empty["offload_cadence_days"] = round(cadence, 1) if not np.isnan(cadence) else None
        empty["last_transshipment"] = last_ts.date() if last_ts is not None else None
        return empty

    vessel_type = rec.get("vessel_type")
    grp = peer_group(vessel_type)
    s0, s1 = _season_window(rec)
    season_year = s1.year if s1 is not None else (s0.year if s0 is not None else None)

    catch_total = rec.get("catch_total_t")
    catch_total = float(catch_total) if pd.notna(catch_total) else None
    catch_pollock = rec.get("catch_pollock_t")
    catch_pollock = float(catch_pollock) if pd.notna(catch_pollock) else None
    hold = rec.get("hold_capacity_t")
    hold = float(hold) if pd.notna(hold) and str(hold).strip() != "" else None

    season_fish = _season_days_metric(yearly, "fishing_days", season_year) if season_year else float("nan")
    season_sea = _season_days_metric(yearly, "at_sea_days", season_year) if season_year else float("nan")

    cpfd = catch_total / season_fish if (catch_total and season_fish and season_fish > 0) else float("nan")
    cpsd = catch_total / season_sea if (catch_total and season_sea and season_sea > 0) else float("nan")

    implied = cpsd * cadence if (not np.isnan(cpsd) and not np.isnan(cadence)) else float("nan")
    effective_hold = hold if hold else (implied if not np.isnan(implied) else float("nan"))
    days_to_fill = effective_hold / cpsd if (not np.isnan(effective_hold) and not np.isnan(cpsd) and cpsd > 0) else float("nan")

    next_eta = None
    if last_ts is not None and not np.isnan(days_to_fill):
        next_eta = (last_ts + pd.Timedelta(days=days_to_fill)).date()
    elif last_ts is not None and not np.isnan(cadence):
        next_eta = (last_ts + pd.Timedelta(days=cadence)).date()

    trips = None
    if not np.isnan(cadence) and s0 is not None and s1 is not None and cadence > 0:
        trips = round((s1 - s0).days / cadence, 1)

    return {
        "vessel_type": vessel_type,
        "peer_group": grp,
        "catch_total_t": round(catch_total, 1) if catch_total else None,
        "catch_pollock_t": round(catch_pollock, 1) if catch_pollock else None,
        "catch_as_of": s1.date() if s1 is not None else None,
        "season_fishing_days": round(season_fish, 1) if not np.isnan(season_fish) else None,
        "season_at_sea_days": round(season_sea, 1) if not np.isnan(season_sea) else None,
        "catch_per_fishing_day_t": round(cpfd, 1) if not np.isnan(cpfd) else None,
        "catch_per_seaday_t": round(cpsd, 1) if not np.isnan(cpsd) else None,
        "hold_capacity_t": round(hold, 1) if hold else None,
        "offload_cadence_days": round(cadence, 1) if not np.isnan(cadence) else None,
        "implied_load_per_offload_t": round(implied, 1) if not np.isnan(implied) else None,
        "effective_hold_t": round(effective_hold, 1) if not np.isnan(effective_hold) else None,
        "days_to_fill_hold": round(days_to_fill, 1) if not np.isnan(days_to_fill) else None,
        "last_transshipment": last_ts.date() if last_ts is not None else None,
        "next_offload_eta": next_eta,
        "trips_per_season": trips,
    }
