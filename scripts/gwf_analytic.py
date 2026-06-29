"""
GFW Activity Budget — анализ сырого трека судна (выгрузка Global Fishing Watch).

Цель: по сырым AIS-точкам (timestamp, speed, lon/lat, depth, seg_id) построить
ПОЛНЫЙ бюджет времени судна за период:

    trawling  — рабочий ход трала (характерная полоса скоростей)
    transit   — переход/транзит (быстрый ход)
    maneuver  — постановка/выборка трала, маневрирование (между idle и тралом)
    idle      — простой/дрейф/якорь/порт (≈0 узлов)
    gap       — неучтённое время (дыры в треке больше MAX_GAP)

Так сумма всех режимов = полное наблюдаемое время, и видно, сколько судно реально
тралит, сколько идёт, сколько простаивает — в часах и в долях.

Дополнительно:
  - детекция отдельных тралений (хаулов) как непрерывных событий с длительностью,
    глубиной, координатами и пройденной дистанцией;
  - суточная статистика по каждому режиму;
  - три сценария порогов (strict / base / soft) для проверки устойчивости выводов.

Метод распределения времени — трапеция: интервал между двумя соседними точками
делится поровну между их режимами (дыры > MAX_GAP относятся в gap, а не в режим).

Запуск:
    .venv/bin/python scripts/gwf_analytic.py
    .venv/bin/python scripts/gwf_analytic.py --input data/track-data-...csv --tz UTC
"""

import argparse
import math
import os

import numpy as np
import pandas as pd

# =========================================================================
# СЦЕНАРИИ
# Полосы скоростей (узлы). idle < SLOW_MAX <= maneuver < TRAWL_MIN <=
# trawling <= TRAWL_MAX < transit. Меняем границы трала, остальное общее.
# =========================================================================
SCENARIOS = [
    # strict — только явное траление (узкая полоса, длинный минимум хаула)
    {"name": "strict", "SLOW_MAX": 1.5, "TRAWL_MIN": 3.0, "TRAWL_MAX": 4.5,
     "MIN_HAUL_MIN": 90, "MIN_POINTS": 4, "SMOOTH_MIN": 20},
    # base — сбалансированный
    {"name": "base", "SLOW_MAX": 1.5, "TRAWL_MIN": 2.5, "TRAWL_MAX": 5.0,
     "MIN_HAUL_MIN": 60, "MIN_POINTS": 3, "SMOOTH_MIN": 25},
    # soft — широкая полоса трала, мягкие требования
    {"name": "soft", "SLOW_MAX": 1.0, "TRAWL_MIN": 2.0, "TRAWL_MAX": 5.5,
     "MIN_HAUL_MIN": 45, "MIN_POINTS": 3, "SMOOTH_MIN": 30},
]

# Общие параметры
MAX_GAP_MIN = 60          # дыра больше — время не относим к режиму (в gap)
DEFAULT_INPUT = "data/track-data-2026-01-01,2026-04-14.csv"
DEFAULT_OUTDIR = "output/gfw_activity"


# =========================================================================
# УТИЛИТЫ
# =========================================================================
def haversine_nm(lat1, lon1, lat2, lon2):
    """Дистанция между точками в морских милях."""
    R_km = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    km = 2 * R_km * math.asin(min(1.0, math.sqrt(a)))
    return km / 1.852


def classify_state(speed, SLOW_MAX, TRAWL_MIN, TRAWL_MAX):
    if speed <= SLOW_MAX:
        return "idle"
    if speed < TRAWL_MIN:
        return "maneuver"
    if speed <= TRAWL_MAX:
        return "trawling"
    return "transit"


def load_track(path):
    df = pd.read_csv(path)
    required = {"lon", "lat", "course", "timestamp", "speed", "depth", "seg_id"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"В CSV отсутствуют обязательные колонки: {missing}")

    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    for col in ["lon", "lat", "course", "speed", "depth"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["timestamp", "speed", "lon", "lat"]).copy()
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


# =========================================================================
# ОДИН СЦЕНАРИЙ
# =========================================================================
def run_scenario(df, tz, name, SLOW_MAX, TRAWL_MIN, TRAWL_MAX,
                 MIN_HAUL_MIN, MIN_POINTS, SMOOTH_MIN):
    d = df.copy()
    d["state"] = d["speed"].apply(
        lambda s: classify_state(s, SLOW_MAX, TRAWL_MIN, TRAWL_MAX)
    )

    # интервал до следующей точки и дистанция до следующей точки
    d["gap_next_min"] = -d["timestamp"].diff(-1).dt.total_seconds() / 60
    lat = d["lat"].to_numpy()
    lon = d["lon"].to_numpy()
    dist_next = np.zeros(len(d))
    for i in range(len(d) - 1):
        dist_next[i] = haversine_nm(lat[i], lon[i], lat[i + 1], lon[i + 1])
    d["dist_next_nm"] = dist_next

    # --- Трапеция: половину каждого валидного интервала отдаём режиму точки слева,
    #     половину — режиму точки справа. Дыры > MAX_GAP относим в gap. ---
    state_minutes = {s: 0.0 for s in ["idle", "maneuver", "trawling", "transit"]}
    state_dist = {s: 0.0 for s in ["idle", "maneuver", "trawling", "transit"]}
    gap_minutes = 0.0
    states = d["state"].to_numpy()
    gaps = d["gap_next_min"].to_numpy()
    dates = d["timestamp"].dt.tz_convert(tz).dt.date.to_numpy()

    daily = {}

    def add_daily(day, key, val):
        daily.setdefault(day, {"trawling": 0.0, "transit": 0.0, "maneuver": 0.0,
                               "idle": 0.0, "gap": 0.0, "dist_nm": 0.0,
                               "trawl_dist_nm": 0.0})
        daily[day][key] += val

    for i in range(len(d) - 1):
        g = gaps[i]
        if pd.isna(g) or g <= 0:
            continue
        if g > MAX_GAP_MIN:
            gap_minutes += g
            add_daily(dates[i], "gap", g / 60.0)
            continue
        half = g / 2.0
        s_left, s_right = states[i], states[i + 1]
        state_minutes[s_left] += half
        state_minutes[s_right] += half
        dd = dist_next[i]
        state_dist[s_left] += dd / 2.0
        state_dist[s_right] += dd / 2.0
        add_daily(dates[i], s_left, half / 60.0)
        add_daily(dates[i + 1], s_right, half / 60.0)
        add_daily(dates[i], "dist_nm", dd / 2.0)
        add_daily(dates[i + 1], "dist_nm", dd / 2.0)
        if s_left == "trawling":
            add_daily(dates[i], "trawl_dist_nm", dd / 2.0)
        if s_right == "trawling":
            add_daily(dates[i + 1], "trawl_dist_nm", dd / 2.0)

    # --- Хаулы: непрерывные события траления со сглаживанием коротких разрывов ---
    is_trawl = (d["state"] == "trawling").to_numpy()
    # разрыв трека (дыра) делит события в любом случае
    track_break = (d["gap_next_min"] > MAX_GAP_MIN).fillna(True).to_numpy()

    # сглаживание: короткий не-трал между двумя тралами (< SMOOTH_MIN и без разрыва)
    # помечаем как трал, чтобы не дробить хаул на повороте/рывке
    n = len(d)
    smoothed = is_trawl.copy()
    ts = d["timestamp"].to_numpy()
    i = 1
    while i < n:
        if not smoothed[i] and smoothed[i - 1]:
            # начало «провала» после трала: ищем следующий трал
            j = i
            while j < n and not smoothed[j]:
                j += 1
            if j < n:  # провал зажат тралом с обеих сторон
                gap_dur = (pd.Timestamp(ts[j]) - pd.Timestamp(ts[i - 1])).total_seconds() / 60
                broke = track_break[i - 1:j].any()
                if gap_dur < SMOOTH_MIN and not broke:
                    smoothed[i:j] = True
            i = j
        else:
            i += 1

    hauls = []
    i = 0
    while i < n:
        if smoothed[i]:
            start = i
            while i + 1 < n and smoothed[i + 1] and not track_break[i]:
                i += 1
            end = i
            seg = d.iloc[start:end + 1]
            dur_min = (seg["timestamp"].iloc[-1] - seg["timestamp"].iloc[0]).total_seconds() / 60
            pts = len(seg)
            seg_dist = float(d["dist_next_nm"].iloc[start:end].sum()) if end > start else 0.0
            if dur_min >= MIN_HAUL_MIN and pts >= MIN_POINTS:
                start_local = seg["timestamp"].iloc[0].tz_convert(tz)
                hauls.append({
                    "haul_id": len(hauls) + 1,
                    "start_time": seg["timestamp"].iloc[0],
                    "end_time": seg["timestamp"].iloc[-1],
                    "date_local": start_local.date(),
                    "duration_hours": dur_min / 60.0,
                    "points": pts,
                    "mean_speed": seg["speed"].mean(),
                    "mean_depth": seg["depth"].mean(),
                    "min_depth": seg["depth"].min(),
                    "max_depth": seg["depth"].max(),
                    "distance_nm": seg_dist,
                    "lat_start": seg["lat"].iloc[0],
                    "lon_start": seg["lon"].iloc[0],
                    "lat_end": seg["lat"].iloc[-1],
                    "lon_end": seg["lon"].iloc[-1],
                })
        i += 1

    hauls_df = pd.DataFrame(hauls)

    # --- Суточная таблица ---
    daily_rows = []
    for day in sorted(daily.keys()):
        rec = daily[day]
        n_hauls = int((hauls_df["date_local"] == day).sum()) if not hauls_df.empty else 0
        daily_rows.append({
            "date_local": day,
            "trawl_hours": rec["trawling"],
            "transit_hours": rec["transit"],
            "maneuver_hours": rec["maneuver"],
            "idle_hours": rec["idle"],
            "gap_hours": rec["gap"],
            "trawl_events": n_hauls,
            "distance_nm": rec["dist_nm"],
            "trawl_distance_nm": rec["trawl_dist_nm"],
        })
    daily_df = pd.DataFrame(daily_rows)

    # --- Сводка по сценарию ---
    obs_min = sum(state_minutes.values())  # наблюдаемое (без gap)
    total_min = obs_min + gap_minutes
    n_days = len(daily_df) if not daily_df.empty else 1

    def hh(x):
        return x / 60.0

    summary = {
        "scenario": name,
        "trawl_min": TRAWL_MIN, "trawl_max": TRAWL_MAX, "slow_max": SLOW_MAX,
        "min_haul_min": MIN_HAUL_MIN,
        "days_with_data": n_days,
        # часы по режимам
        "trawl_hours": hh(state_minutes["trawling"]),
        "transit_hours": hh(state_minutes["transit"]),
        "maneuver_hours": hh(state_minutes["maneuver"]),
        "idle_hours": hh(state_minutes["idle"]),
        "gap_hours": hh(gap_minutes),
        # доли от наблюдаемого времени
        "trawl_share_%": 100 * state_minutes["trawling"] / obs_min if obs_min else 0,
        "transit_share_%": 100 * state_minutes["transit"] / obs_min if obs_min else 0,
        "maneuver_share_%": 100 * state_minutes["maneuver"] / obs_min if obs_min else 0,
        "idle_share_%": 100 * state_minutes["idle"] / obs_min if obs_min else 0,
        # средние в сутки
        "avg_trawl_hours_per_day": hh(state_minutes["trawling"]) / n_days,
        "avg_transit_hours_per_day": hh(state_minutes["transit"]) / n_days,
        "avg_idle_hours_per_day": hh(state_minutes["idle"]) / n_days,
        # хаулы
        "total_hauls": len(hauls_df),
        "avg_hauls_per_day": (len(hauls_df) / n_days),
        "avg_haul_hours": hauls_df["duration_hours"].mean() if not hauls_df.empty else 0,
        "median_haul_hours": hauls_df["duration_hours"].median() if not hauls_df.empty else 0,
        # дистанции и глубины
        "total_distance_nm": sum(state_dist.values()),
        "trawl_distance_nm": state_dist["trawling"],
        "transit_distance_nm": state_dist["transit"],
        "mean_trawl_depth_m": hauls_df["mean_depth"].mean() if not hauls_df.empty else np.nan,
        "data_coverage_%": 100 * obs_min / total_min if total_min else 0,
    }

    return hauls_df, daily_df, summary


# =========================================================================
# MAIN
# =========================================================================
def main():
    ap = argparse.ArgumentParser(description="GFW activity budget analyzer")
    ap.add_argument("--input", default=DEFAULT_INPUT, help="CSV трека GFW")
    ap.add_argument("--tz", default="UTC", help="часовой пояс для суточной агрегации")
    ap.add_argument("--outdir", default=DEFAULT_OUTDIR, help="папка для результатов")
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    df = load_track(args.input)

    span_days = (df["timestamp"].max() - df["timestamp"].min()).total_seconds() / 86400
    print(f"Загружено точек: {len(df)} | период: "
          f"{df['timestamp'].min().date()} → {df['timestamp'].max().date()} "
          f"({span_days:.0f} дн., ~{len(df)/span_days:.0f} точек/сутки)")

    all_summaries = []
    for sc in SCENARIOS:
        name = sc["name"]
        print(f"\n=== Сценарий: {name} ===")
        hauls_df, daily_df, summary = run_scenario(df, args.tz, **sc)

        hauls_df.to_csv(os.path.join(args.outdir, f"hauls_{name}.csv"), index=False)
        daily_df.to_csv(os.path.join(args.outdir, f"daily_{name}.csv"), index=False)
        all_summaries.append(summary)

        print(f"  траление: {summary['trawl_hours']:.0f} ч "
              f"({summary['trawl_share_%']:.0f}%), "
              f"переход: {summary['transit_hours']:.0f} ч "
              f"({summary['transit_share_%']:.0f}%), "
              f"простой: {summary['idle_hours']:.0f} ч "
              f"({summary['idle_share_%']:.0f}%)")
        print(f"  хаулов: {summary['total_hauls']} "
              f"(~{summary['avg_hauls_per_day']:.1f}/сут, "
              f"ср. {summary['avg_haul_hours']:.1f} ч), "
              f"ср. глубина трала: {summary['mean_trawl_depth_m']:.0f} м")

    summary_df = pd.DataFrame(all_summaries)
    summary_path = os.path.join(args.outdir, "scenarios_summary.csv")
    summary_df.to_csv(summary_path, index=False)

    print("\n=== Сводка по сценариям (бюджет времени) ===")
    cols = ["scenario", "trawl_hours", "trawl_share_%", "transit_hours",
            "idle_hours", "total_hauls", "avg_trawl_hours_per_day",
            "mean_trawl_depth_m", "data_coverage_%"]
    with pd.option_context("display.width", 160, "display.max_columns", None):
        print(summary_df[cols].round(1).to_string(index=False))
    print(f"\nРезультаты сохранены в: {args.outdir}/")
    print("  hauls_*.csv, daily_*.csv, scenarios_summary.csv")


if __name__ == "__main__":
    main()
