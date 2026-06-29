"""
GFW Fleet Efficiency Analytic — аналитика эффективности рыболовного флота
по всем событиям Global Fishing Watch (fishing / encounter / port_visit).

Зачем
-----
Один CSV выгрузки событий GFW по судну содержит ВСЮ его историю: тралёжку
(fishing), встречи в море (encounter — перегруз улова на рефрижератор,
бункеровка, снабжение) и заходы в порт (port_visit — выгрузка, ремонт,
докование, отстой). Если собрать все суда конкурента (напр. НБАМР) в одну
папку, получаем сравнимую по судам картину эффективности флота.

Идея «грамотно использовать ВСЕ события»
----------------------------------------
Бюджет времени судна за год раскладывается так:
    наблюдаемые дни = промысел + переход/поиск + море-логистика(encounter)
                      + порт + ремонт + отстой
Из этого считаются KPI эффективности:

  1) fishing_intensity_%   = промысел / наблюдаемые дни
        — насколько интенсивно судно вообще работает.
  2) deployment_eff_%      = промысел / дни в море
        — какая доля морского времени уходит в собственно тралёжку,
          а не в переходы/поиск (низко = много холостого хода).
  3) availability_%        = (год − ремонт − отстой) / год
        — техническая готовность.
  4) autonomy_days         = медианный интервал между заходами в порт
        — длина рейса; больше = судно автономнее, реже бегает в порт.
  5) at_sea_offload_share  = перегрузы в море / (перегрузы + выгрузки в порту)
        — доля улова, сдаваемого в море на рефрижератор. Высоко = судно
          остаётся на промысле, не тратит дни на рейсы в порт = эффективнее.
  6) repair_burden_%       = ремонт / наблюдаемые дни
  7) docking cycle/prognosis — цикл докования и прогноз окна (для продаж).

Встречи в море (encounter) классифицируются:
  transshipment  — перегруз улова (контрагент-рефрижератор/транспорт,
                   либо длительный контакт в типичных промрайонах);
  bunkering      — бункеровка (контрагент-танкер по реестру);
  supply_other   — короткий контакт / снабжение / прочее.
Точного типа контрагента в выгрузке GFW нет (флаг есть, тип — нет), поэтому
используется расширяемый реестр имён + длительность как устойчивые признаки.

Вход
----
  --input <events.csv>     один файл
  --input-dir <dir>        папка с *events*.csv (весь флот)
  --ids-from <dir>         то же, что --input-dir

Выход (в --outdir, по умолчанию output/gfw_fleet/)
  fleet_scorecard.csv  — по судну: все KPI эффективности + прогноз докования
  fleet_yearly.csv     — судно×год: полный бюджет времени и KPI
  encounters.csv       — все встречи в море с классификацией
  fleet_benchmark.csv  — ранги судов и сводный efficiency_score

Запуск
------
  .venv/bin/python scripts/gfw_fleet_analytic.py --input "data/ALEXANDR ...csv"
  .venv/bin/python scripts/gfw_fleet_analytic.py --input-dir data/nbamr_events
"""

from __future__ import annotations

import argparse
import glob
import os

import numpy as np
import pandas as pd

import gfw_catch_cargo as cargo
import gfw_repairs_analytic as rep

# =========================================================================
# КЛАССИФИКАЦИЯ ВСТРЕЧ В МОРЕ (encounter)
# =========================================================================
# Реестр имён транспортных рефрижераторов / транспортов (перегруз улова).
# Расширяемый: дополняйте по мере появления новых контрагентов во флоте.
# Регэксп по верхнему регистру имени encounteredVesselName.
REEFER_KEYWORDS = (
    "CRYSTAL|FRIO|FROST|REEFER|COOL|POLAR|ICE|PEVEK|SIMUSHIR|"
    "PRIBOY|MERIDIAN|PILGRIM|PIONER|TRANSIT|PROGRESS|PALLADA|"
    "PRIMORYE|SEVMORPUT|KAPITAN|MORZHOVETS|BERG"
)
# Реестр имён танкеров/бункеровщиков (бункеровка в море).
TANKER_KEYWORDS = "TANKER|BUNKER|NEFT|OIL|FUEL|PETRO|GAZ|LNG|MAZUT|SVETLY"

# Длительный контакт (часы) — устойчивый признак перегруза, а не быстрого касания.
TRANSSHIP_MIN_HOURS = 6.0


def classify_encounters(enc: pd.DataFrame) -> pd.DataFrame:
    """Размечает встречи в море: kind + is_transshipment/is_bunkering."""
    enc = enc.copy()
    if enc.empty:
        for c in ("dur_hours", "counterparty", "kind"):
            enc[c] = pd.Series(dtype="object")
        return enc
    enc["dur_hours"] = (enc["end"] - enc["start"]).dt.total_seconds() / 3600
    name = enc["encounteredVesselName"].fillna("").str.upper()
    enc["counterparty"] = name
    is_reefer = name.str.contains(REEFER_KEYWORDS, na=False, regex=True)
    is_tanker = name.str.contains(TANKER_KEYWORDS, na=False, regex=True)
    long_contact = enc["dur_hours"] >= TRANSSHIP_MIN_HOURS

    def kind(row_reefer, row_tanker, row_long):
        if row_tanker:
            return "bunkering"
        if row_reefer or row_long:
            return "transshipment"
        return "supply_other"

    enc["kind"] = [
        kind(r, t, l) for r, t, l in zip(is_reefer, is_tanker, long_contact)
    ]
    enc["is_transshipment"] = enc["kind"] == "transshipment"
    enc["is_bunkering"] = enc["kind"] == "bunkering"
    return enc


# =========================================================================
# БЮДЖЕТ ВРЕМЕНИ И KPI ПО ГОДАМ
# =========================================================================
def _overlap_days(s, e, y0, y1) -> float:
    a = max(s, y0)
    b = min(e, y1)
    return max(0.0, (b - a).total_seconds() / 86400)


def compute_fleet_yearly(
    pv: pd.DataFrame, fishing: pd.DataFrame, enc: pd.DataFrame,
    vessel: str, span_start, span_end,
) -> pd.DataFrame:
    """Полный бюджет времени судна по годам + KPI эффективности.

    pv  — заходы в порт с колонками is_repair, event_kind (из rep.analyze_vessel).
    enc — встречи с колонкой kind (из classify_encounters).
    """
    repair_pv = pv[pv["is_repair"]]
    layup_pv = pv[pv["event_kind"] == "extended_layup"]
    port_ops = pv[~pv["is_repair"] & (pv["event_kind"] != "extended_layup")]
    transship = enc[enc.get("kind") == "transshipment"] if not enc.empty else enc

    rows = []
    now = pd.Timestamp.now(tz="UTC")
    for yr in range(span_start.year, span_end.year + 1):
        y0 = pd.Timestamp(f"{yr}-01-01", tz="UTC")
        y1 = pd.Timestamp(f"{yr + 1}-01-01", tz="UTC")
        year_days = (min(y1, now) - y0).total_seconds() / 86400
        if year_days <= 0:
            continue
        port_all = sum(_overlap_days(r.start, r.end, y0, y1) for r in pv.itertuples())
        repair_d = sum(_overlap_days(r.start, r.end, y0, y1) for r in repair_pv.itertuples())
        layup_d = sum(_overlap_days(r.start, r.end, y0, y1) for r in layup_pv.itertuples())
        port_ops_d = sum(_overlap_days(r.start, r.end, y0, y1) for r in port_ops.itertuples())
        fish_d = sum(_overlap_days(r.start, r.end, y0, y1) for r in fishing.itertuples())
        enc_d = (
            sum(_overlap_days(r.start, r.end, y0, y1) for r in enc.itertuples())
            if not enc.empty else 0.0
        )
        # port_all уже включает repair_d и layup_d (это подмножества заходов в порт)
        at_sea = max(0.0, year_days - port_all)
        steaming = max(0.0, at_sea - fish_d - enc_d)
        active = max(1.0, year_days - repair_d - layup_d)  # «рабочий» фонд времени

        n_transship = int(((transship["start"] >= y0) & (transship["start"] < y1)).sum()) if not transship.empty else 0
        n_unload = int(((port_ops["start"] >= y0) & (port_ops["start"] < y1)).sum())
        offload_total = n_transship + n_unload

        rows.append({
            "vessel": vessel,
            "year": yr,
            "days_observed": round(year_days, 1),
            "fishing_days": round(fish_d, 1),
            "steaming_days": round(steaming, 1),
            "encounter_days": round(enc_d, 1),
            "port_ops_days": round(port_ops_d, 1),
            "repair_days": round(repair_d, 1),
            "layup_days": round(layup_d, 1),
            "at_sea_days": round(at_sea, 1),
            "fishing_intensity_%": round(100 * fish_d / year_days, 1),
            "deployment_eff_%": round(100 * fish_d / max(1.0, at_sea), 1),
            "availability_%": round(100 * active / year_days, 1),
            "transshipments": n_transship,
            "port_unloads": n_unload,
            "at_sea_offload_share_%": round(100 * n_transship / offload_total, 1) if offload_total else 0.0,
        })
    return pd.DataFrame(rows)


# =========================================================================
# СКОРКАРТА СУДНА
# =========================================================================
def vessel_scorecard(
    df: pd.DataFrame, vessel: str, yards, recent_years: int = 3, *,
    rmrs_dir: str | None = None, catch_ref: dict | None = None,
) -> dict:
    """Полный профиль эффективности судна (все KPI + прогноз докования + вылов)."""
    rep_res = rep.analyze_vessel(df, vessel, yards, rmrs_dir=rmrs_dir)
    prof = rep_res["profile"]
    pv = rep_res["port_visits"]

    fishing = df[df["type"] == "fishing"].copy()
    enc = classify_encounters(df[df["type"] == "encounter"].copy())

    span_start = df["start"].min()
    span_end = df["end"].max()

    yearly = compute_fleet_yearly(pv, fishing, enc, vessel, span_start, span_end)

    # автономность: медиана интервала между заходами в порт (дни)
    autonomy = np.nan
    if len(pv) >= 2:
        gaps = pv.sort_values("start")["start"].diff().dt.total_seconds().dropna() / 86400
        gaps = gaps[gaps > 0.5]
        if not gaps.empty:
            autonomy = float(gaps.median())

    # свежие годы (последние N полных/неполных лет) — текущая, а не историческая картина
    recent = yearly[yearly["year"] >= (span_end.year - recent_years + 1)] if not yearly.empty else yearly

    def wmean(col):
        if recent.empty:
            return np.nan
        w = recent["days_observed"]
        return float((recent[col] * w).sum() / max(1.0, w.sum()))

    n_transship = int(enc["is_transshipment"].sum()) if not enc.empty else 0
    n_bunker = int(enc["is_bunkering"].sum()) if not enc.empty else 0
    port_ops = pv[~pv["is_repair"] & (pv["event_kind"] != "extended_layup")]
    n_unload = len(port_ops)
    offload_total = n_transship + n_unload

    card = {
        "vessel": vessel,
        "period_start": prof["period_start"],
        "period_end": prof["period_end"],
        "years_observed": prof["years_observed"],
        # производительность (среднее по свежим годам, взвешенное по дням наблюдения)
        "fishing_intensity_%": round(wmean("fishing_intensity_%"), 1) if not recent.empty else None,
        "deployment_eff_%": round(wmean("deployment_eff_%"), 1) if not recent.empty else None,
        "availability_%": round(wmean("availability_%"), 1) if not recent.empty else None,
        "fishing_days_per_year": round(wmean("fishing_days"), 1) if not recent.empty else None,
        # логистика/автономность
        "autonomy_days": round(autonomy, 1) if not np.isnan(autonomy) else None,
        "transshipments": n_transship,
        "bunkerings": n_bunker,
        "port_unloads": n_unload,
        "at_sea_offload_share_%": round(100 * n_transship / offload_total, 1) if offload_total else 0.0,
        "distinct_counterparties": int(enc["counterparty"].nunique()) if not enc.empty else 0,
        # надёжность/ремонт
        "repair_burden_%": prof["repair_share_%"],
        "total_repair_days": prof["total_repair_days"],
        "total_layup_days": prof["total_layup_days"],
        "dry_dock_repairs": prof["dry_dock_repairs"],
        # прогноз докования (для продаж)
        "docking_cycle_months": prof["docking_cycle_months"],
        "last_docking": prof["last_docking"],
        "predicted_next_docking": prof["predicted_next_docking"],
        "months_to_next_docking": prof["months_to_next_docking"],
        "sales_window": prof["sales_window"],
        # классовые освидетельствования РС
        "imo": prof.get("imo"),
        "class_notation": prof.get("class_notation"),
        "next_class_survey_type": prof.get("next_class_survey_type"),
        "next_class_survey_date": prof.get("next_class_survey_date"),
        "next_mandatory_docking_class": prof.get("next_mandatory_docking_class"),
        "class_survey_status": prof.get("class_survey_status"),
        "surveys_source": prof.get("surveys_source"),
        "predicted_next_maintenance": prof.get("predicted_next_maintenance"),
        "maintenance_driver": prof.get("maintenance_driver"),
        "sales_window_combined": prof.get("sales_window_combined"),
        "months_to_next_maintenance": prof.get("months_to_next_maintenance"),
    }

    # --- вылов и набор груза (официальная сводка × GFW) ---
    cargo_metrics = cargo.analyze_cargo(vessel, enc, yearly, catch_ref or {})
    card.update(cargo_metrics)
    return {"card": card, "yearly": yearly, "encounters": enc.assign(vessel=vessel)}


# =========================================================================
# ФЛОТОВЫЙ БЕНЧМАРК
# =========================================================================
def build_benchmark(scorecard: pd.DataFrame) -> pd.DataFrame:
    """Ранги судов + сводный efficiency_score (0–100), СЕГМЕНТИРОВАННО по типу.

    Сравнивать БМРТ с плавзаводом (Царица) некорректно — у них разная роль,
    логистика и продукция. Поэтому нормировка и ранги считаются ВНУТРИ группы
    сравнения (peer_group: trawler / factory / other). Если в группе одно судно —
    балл не присваивается (не с чем сравнивать).

    Композит для траулеров: продуктивность по вылову (если есть официальная
    сводка), интенсивность промысла, готовность, эфф. развёртывания, автономность.
    """
    if scorecard.empty:
        return scorecard
    b = scorecard.copy()
    if "peer_group" not in b.columns:
        b["peer_group"] = "trawler"
    b["peer_group"] = b["peer_group"].fillna("trawler")

    # Продуктивность по вылову — главный приоритет, если сводка доступна.
    weights = {
        "catch_per_seaday_t": 0.30,
        "fishing_intensity_%": 0.25,
        "availability_%": 0.20,
        "deployment_eff_%": 0.15,
        "autonomy_days": 0.10,
    }

    def norm_within(s: pd.Series) -> pd.Series:
        lo, hi = s.min(), s.max()
        if pd.isna(lo) or hi == lo:
            return pd.Series(np.where(s.notna(), 50.0, np.nan), index=s.index)
        return (s - lo) / (hi - lo) * 100

    b["efficiency_score"] = np.nan
    rank_cols = ("catch_per_seaday_t", "fishing_intensity_%", "availability_%",
                 "deployment_eff_%", "autonomy_days", "efficiency_score")
    for col in rank_cols:
        b[f"rank_{col}"] = pd.Series(pd.NA, index=b.index, dtype="Int64")

    for grp, idx in b.groupby("peer_group").groups.items():
        g = b.loc[idx]
        if len(g) >= 2:
            score = pd.Series(0.0, index=g.index)
            wsum = pd.Series(0.0, index=g.index)
            for col, w in weights.items():
                if col not in g.columns:
                    continue
                n = norm_within(pd.to_numeric(g[col], errors="coerce"))
                score = score.add(n.fillna(0) * w, fill_value=0)
                wsum = wsum.add(n.notna().astype(float) * w, fill_value=0)
            b.loc[idx, "efficiency_score"] = (score / wsum.replace(0, np.nan)).round(1)
        for col in rank_cols:
            if col in g.columns:
                r = pd.to_numeric(b.loc[idx, col], errors="coerce").rank(
                    ascending=False, method="min")
                b.loc[idx, f"rank_{col}"] = r.astype("Int64")

    return b.sort_values(["peer_group", "efficiency_score"], ascending=[True, False])


# =========================================================================
# MAIN
# =========================================================================
def main() -> int:
    ap = argparse.ArgumentParser(description="GFW fleet efficiency analytic")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--input", help="CSV событий одного судна")
    src.add_argument("--input-dir", help="Папка с *events*.csv (флот)")
    ap.add_argument("--outdir", default="output/gfw_fleet")
    ap.add_argument("--shipyards", help="CSV справочника верфей (name,lat,lon)")
    ap.add_argument("--recent-years", type=int, default=3,
                    help="Сколько последних лет усреднять для текущих KPI (по умолч. 3)")
    ap.add_argument("--rmrs-dir", default="output",
                    help="Папка с rmrs_events_<IMO>.json для классового прогноза")
    ap.add_argument("--catch-ref", default=None,
                    help="CSV справочника вылова (по умолч. data/reference/nbamr_vessel_catch.csv)")
    args = ap.parse_args()

    yards = rep.load_shipyards(args.shipyards)
    print(f"Справочник верфей: {len(yards)} точек")
    catch_ref = cargo.load_catch_reference(args.catch_ref)
    print(f"Справочник вылова: {len(catch_ref)} судов")

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
    cards, yearlies, encs = [], [], []

    for path in files:
        df, vessel = rep.load_events(path)
        if df.empty or "type" not in df.columns:
            print(f"[skip] пустой/некорректный файл: {path}")
            continue
        res = vessel_scorecard(df, vessel, yards, recent_years=args.recent_years,
                               rmrs_dir=args.rmrs_dir, catch_ref=catch_ref)
        cards.append(res["card"])
        if not res["yearly"].empty:
            yearlies.append(res["yearly"])
        if not res["encounters"].empty:
            encs.append(res["encounters"])
        c = res["card"]
        print(f"\n=== {vessel} [{c.get('vessel_type') or '—'}] ===")
        print(f"  период: {c['period_start']} → {c['period_end']} ({c['years_observed']} лет)")
        print(f"  интенсивность промысла: {c['fishing_intensity_%']}% | "
              f"готовность: {c['availability_%']}% | "
              f"эфф. развёртывания: {c['deployment_eff_%']}%")
        if c.get("catch_total_t"):
            print(f"  ВЫЛОВ ({c.get('catch_as_of')}): {c['catch_total_t']} т | "
                  f"вылов/пром.день: {c.get('catch_per_fishing_day_t')} т | "
                  f"вылов/сутки в море: {c.get('catch_per_seaday_t')} т")
            print(f"  набор груза → перегруз: каденция {c.get('offload_cadence_days')} сут | "
                  f"набор за рейс ~{c.get('effective_hold_t')} т | "
                  f"след. перегруз ≈ {c.get('next_offload_eta')}")
        print(f"  автономность: {c['autonomy_days']} дн/рейс | "
              f"перегрузы в море: {c['transshipments']} | "
              f"бункеровки: {c['bunkerings']} | "
              f"сдача в море: {c['at_sea_offload_share_%']}%")
        if c.get("docking_cycle_months"):
            print(f"  цикл докования (GFW): ~{c['docking_cycle_months']} мес | "
                  f"прогноз: {c['predicted_next_docking']} ({c['sales_window']})")
        if c.get("next_class_survey_type"):
            print(f"  класс РС: {c['next_class_survey_type']} → {c.get('next_class_survey_date')} "
                  f"({c.get('class_survey_status')}, {c.get('surveys_source')})")
        if c.get("predicted_next_maintenance"):
            print(f"  ИТОГО обслуживание: {c['predicted_next_maintenance']} "
                  f"({c.get('maintenance_driver')}, {c.get('sales_window_combined')})")

    if not cards:
        print("Нет данных для анализа.")
        return 1

    scorecard = pd.DataFrame(cards)
    benchmark = build_benchmark(scorecard)
    yearly_all = pd.concat(yearlies, ignore_index=True) if yearlies else pd.DataFrame()
    enc_cols = ["vessel", "start", "end", "dur_hours", "counterparty",
                "encounteredVesselFlag", "kind", "latitude", "longitude"]
    enc_all = pd.concat(encs, ignore_index=True) if encs else pd.DataFrame()
    if not enc_all.empty:
        enc_all = enc_all[[c for c in enc_cols if c in enc_all.columns]].copy()
        enc_all["start"] = pd.to_datetime(enc_all["start"]).dt.strftime("%Y-%m-%d %H:%M")
        enc_all["end"] = pd.to_datetime(enc_all["end"]).dt.strftime("%Y-%m-%d %H:%M")
        enc_all["dur_hours"] = enc_all["dur_hours"].round(1)

    scorecard.to_csv(os.path.join(args.outdir, "fleet_scorecard.csv"), index=False)
    benchmark.to_csv(os.path.join(args.outdir, "fleet_benchmark.csv"), index=False)
    yearly_all.to_csv(os.path.join(args.outdir, "fleet_yearly.csv"), index=False)
    enc_all.to_csv(os.path.join(args.outdir, "encounters.csv"), index=False)

    if len(benchmark) > 1:
        print("\n=== ФЛОТОВЫЙ БЕНЧМАРК (efficiency_score внутри типа судна) ===")
        cols = ["vessel", "peer_group", "vessel_type", "efficiency_score",
                "catch_per_seaday_t", "fishing_intensity_%", "availability_%",
                "autonomy_days"]
        show = benchmark[[c for c in cols if c in benchmark.columns]]
        print(show.to_string(index=False))

    sw_col = "sales_window_combined" if "sales_window_combined" in scorecard.columns else "sales_window"
    hot = scorecard[scorecard[sw_col].isin(["approaching", "due_soon"])]
    if not hot.empty:
        print("\n=== Горячие лиды (обслуживание близко/просрочено) ===")
        cols = ["vessel", "imo", "predicted_next_maintenance", "maintenance_driver",
                "next_mandatory_docking_class", sw_col]
        cols = [c for c in cols if c in hot.columns]
        print(hot[cols].to_string(index=False))

    print(f"\nРезультаты сохранены в: {args.outdir}/")
    print("  fleet_scorecard.csv, fleet_benchmark.csv, fleet_yearly.csv, encounters.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
