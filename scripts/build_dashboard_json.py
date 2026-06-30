"""
Build dashboards/data/<company>.json from GFW analytic output CSVs.

Usage:
    python3 scripts/build_dashboard_json.py --company nbamr
    python3 scripts/build_dashboard_json.py --company nbamr --events-dir data/nbamr_events --output-dir output/gfw_fleet

Reads:
    output/gfw_fleet/fleet_scorecard.csv  (or fleet_benchmark.csv for ranks)
    output/gfw_fleet/fleet_benchmark.csv
    output/gfw_fleet/encounters.csv
    data/reference/<company>_vessel_catch.csv  (optional, вылов)

Writes:
    dashboards/data/<company>.json
"""

import argparse
import csv
import json
import os
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parent.parent


def read_csv(path):
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def parse_float(v, default=None):
    try:
        return float(v) if v not in (None, "", "nan") else default
    except Exception:
        return default


def parse_int(v, default=0):
    try:
        return int(float(v)) if v not in (None, "", "nan") else default
    except Exception:
        return default


def short_name(vessel_name: str) -> str:
    """ALEXANDR BELYAKOV(RUS) -> А. Беляков  (best-effort)"""
    name = re.sub(r"\([A-Z]+\)$", "", vessel_name).strip()
    parts = name.split()
    TRANSLATE = {
        "ALEXANDR": "А.", "ALEXANDER": "А.",
        "ILYA": "И.", "NIKOLAY": "Н.",
        "KAPITAN": "Кап.", "CAPTAIN": "Кап.",
        "SEAWIND1": "Сивинд-1", "SEAWIND": "Сивинд",
        "TSARITSA": "Царица",
        "BELYAKOV": "Беляков", "KONOVALOV": "Коновалов",
        "CHEPIK": "Чепик", "FALEYEV": "Фалеев",
        "MASLOVETS": "Масловец",
    }
    if len(parts) == 1:
        return TRANSLATE.get(parts[0], parts[0].capitalize())
    first = TRANSLATE.get(parts[0], parts[0].capitalize())
    rest = " ".join(TRANSLATE.get(p, p.capitalize()) for p in parts[1:])
    return f"{first} {rest}"


VESSEL_TYPE_MAP = {
    "trawler": "БМРТ",
    "factory": "Плавзавод (СТР/РТМКС)",
}

def compute_fleet_efficiency_index(vessels):
    """
    Индекс эффективности флота (ИЭФ) 0-100.
    Только для однотипных судов (trawler/БМРТ). Плавзавод не ранжируется.

    Методология (лучшие практики ФАО + Норвегия + Россия):
      35% — catch_per_seaday_t  (вылов т/морской день — главная метрика, не зависит от AIS)
      25% — availability_%      (доля времени в море, не в ремонте)
      20% — at_sea_offload_%    (автономность сдачи, без захода в порт)
      20% — repair_burden_%     (ремонтная нагрузка, инвертированная)
    """
    WEIGHTS = {
        "seaDay": 0.35,
        "avail": 0.25,
        "offload_share": 0.20,
        "repair_rel": 0.20,   # (100 - repair_pct) normalized
    }

    def normalize(values):
        """Min-max scale to 0-1, None → skip."""
        vals = [v for v in values if v is not None]
        if not vals or max(vals) == min(vals):
            return [0.5 if v is not None else None for v in values]
        lo, hi = min(vals), max(vals)
        return [((v - lo) / (hi - lo)) if v is not None else None for v in values]

    by_type = {}
    for v in vessels:
        pt = "trawler" if "БМРТ" in v.get("type", "") else v.get("type", "other")
        by_type.setdefault(pt, []).append(v)

    for pt, group in by_type.items():
        if pt not in ("trawler",):
            for v in group:
                v["ief"] = None
                v["ief_rank"] = None
            continue

        sea_days = [v.get("seaDay") for v in group]
        avails    = [v.get("avail") for v in group]
        offloads  = [v.get("offload_share") for v in group]
        rep_inv   = [(100 - v["repair_pct"]) if v.get("repair_pct") is not None else None for v in group]

        n_sea  = normalize(sea_days)
        n_avl  = normalize(avails)
        n_off  = normalize(offloads)
        n_rep  = normalize(rep_inv)

        scores = []
        for i, v in enumerate(group):
            parts = [
                (n_sea[i], WEIGHTS["seaDay"]),
                (n_avl[i], WEIGHTS["avail"]),
                (n_off[i], WEIGHTS["offload_share"]),
                (n_rep[i], WEIGHTS["repair_rel"]),
            ]
            used_w = sum(w for val, w in parts if val is not None)
            score_raw = sum(val * w for val, w in parts if val is not None)
            score = round((score_raw / used_w) * 100, 1) if used_w > 0 else None
            scores.append(score)
            v["ief"] = score

        # Rank by score descending (1 = best)
        ranked = sorted(
            [(i, s) for i, s in enumerate(scores) if s is not None],
            key=lambda x: -x[1]
        )
        for rank_pos, (i, _) in enumerate(ranked, 1):
            group[i]["ief_rank"] = rank_pos


# Алиасы имён судов: имя в scorecard -> имя в gfw_our_vessels.json
GFW_NAME_ALIAS = {
    "SEAWIND1": "SIVIND",
}


def _norm_name(name):
    return re.sub(r"\([A-Z]+\)$", "", name or "").strip().upper()


def load_gfw_ids():
    """{normalized_gfw_name -> gfw_id, imo -> gfw_id} из gfw_our_vessels.json."""
    path = ROOT / "data" / "gfw_our_vessels.json"
    by_name, by_imo = {}, {}
    if not path.exists():
        return by_name, by_imo
    with open(path, encoding="utf-8") as f:
        for v in json.load(f):
            gid = v.get("gfw_id")
            if not gid:
                continue
            gn = _norm_name(v.get("gfw_name"))
            if gn:
                by_name[gn] = gid
            imo = (str(v.get("imo")) if v.get("imo") else "").strip()
            if imo:
                by_imo[imo] = gid
    return by_name, by_imo


def resolve_gfw_id(vessel_name, imo, by_name, by_imo):
    """Сопоставить судно с gfw_id: по имени -> алиасу -> IMO."""
    n = _norm_name(vessel_name)
    if n in by_name:
        return by_name[n]
    if n in GFW_NAME_ALIAS and GFW_NAME_ALIAS[n] in by_name:
        return by_name[GFW_NAME_ALIAS[n]]
    imo = (str(imo) if imo else "").strip()
    if imo and imo in by_imo:
        return by_imo[imo]
    return None


# Нормализация объектов лова к общему знаменателю (вылов/квота используют одни ключи)
SPECIES_NORM = {
    "минтай": "Минтай", "кальмар": "Кальмар", "краб": "Краб", "треска": "Треска",
    "макрурусы": "Макрурусы", "макрурусы (210)": "Макрурусы", "терпуги": "Терпуги",
    "терпуги (692)": "Терпуги",
}


def norm_species(s):
    s = (s or "").strip()
    return SPECIES_NORM.get(s.lower(), s)


def load_quota(group_inns):
    """Квоты группы компаний из quota_summary.csv по списку ИНН.
    Дедуп: один договор может быть пересмотрен несколько раз за год (строки-корректировки),
    суммировать их нельзя. Берём максимум объёма по каждому договору
    (ИНН, год, объект, доля%, дата договора, тип квоты), затем суммируем уникальные договоры.
    Возвращает (by_year, by_entity_year):
      by_year:        {year -> {species -> tons}}          — вся группа
      by_entity_year: {year -> {entity_name -> tons}}      — разбивка по юрлицам
    """
    path = ROOT / "output" / "quota_summary.csv"
    by_year = defaultdict(lambda: defaultdict(float))
    by_entity_year = defaultdict(lambda: defaultdict(float))
    if not path.exists() or not group_inns:
        return by_year, by_entity_year
    inn_set = set(group_inns.keys())
    import csv as _csv
    _csv.field_size_limit(10 ** 7)
    contracts = {}  # (inn, year, species, share, contract_start, quota_type) -> max volume
    with open(path, encoding="utf-8") as f:
        for row in _csv.DictReader(f):
            inn = (row.get("ИНН") or "").strip()
            if inn not in inn_set:
                continue
            try:
                yr = int(row.get("Год"))
                vol = float(row.get("Объем_Тонн") or 0)
            except Exception:
                continue
            sp = norm_species(row.get("Объект_Лова"))
            key = (inn, yr, sp, row.get("Доля_%"), row.get("Дата_Начала_Договора"), row.get("Тип_Квоты"))
            contracts[key] = max(contracts.get(key, 0.0), vol)
    for (inn, yr, sp, _s, _d, _t), vol in contracts.items():
        by_year[yr][sp] += vol
        by_entity_year[yr][group_inns[inn]] += vol
    return by_year, by_entity_year


def load_prices(company_id):
    """Цены по объекту: {species -> {usd, rub, as_of, source}} из data/reference/<company>_prices.csv."""
    path = ROOT / "data" / "reference" / f"{company_id}_prices.csv"
    prices = {}
    if not path.exists():
        return prices
    with open(path, encoding="utf-8") as f:
        rows = [ln for ln in f if not ln.lstrip().startswith("#")]
    for r in csv.DictReader(rows):
        sp = norm_species(r.get("species"))
        if not sp:
            continue
        prices[sp] = {
            "usd": parse_float(r.get("price_usd_per_t")),
            "rub": parse_float(r.get("price_rub_per_t")),
            "as_of": (r.get("as_of") or "").strip(),
            "source": (r.get("source") or "").strip(),
        }
    return prices


def load_financials(company_id):
    """Финансы компании по годам из data/reference/<company>_financials.csv."""
    path = ROOT / "data" / "reference" / f"{company_id}_financials.csv"
    fin = {}
    if not path.exists():
        return fin
    with open(path, encoding="utf-8") as f:
        rows = [ln for ln in f if not ln.lstrip().startswith("#")]
    for r in csv.DictReader(rows):
        try:
            yr = int(r.get("year"))
        except Exception:
            continue
        fin[yr] = {
            "revenue_rub_m": parse_float(r.get("revenue_rub_m")),
            "net_profit_rub_m": parse_float(r.get("net_profit_rub_m")),
            "capex_repair_rub_m": parse_float(r.get("capex_repair_rub_m")),
            "source": (r.get("source") or "").strip(),
        }
    return fin


def load_verification_flags():
    """Флаги верификации данных из data/reference/nbamr_data_verification.csv."""
    path = ROOT / "data" / "reference" / "nbamr_data_verification.csv"
    flags = defaultdict(list)
    if not path.exists():
        return flags
    with open(path, encoding="utf-8") as f:
        rows = [ln.strip() for ln in f if ln.strip() and not ln.lstrip().startswith("#")]
    for r in csv.DictReader(rows):
        if not r or not r.get("domain"):
            continue
        domain = r.get("domain")
        status = r.get("status") or "pending"
        flags[domain].append({
            "year": r.get("year") if r.get("year") != "all" else "all",
            "field": r.get("field"),
            "status": status,
            "value": r.get("value"),
            "note": r.get("note"),
        })
    return flags


def load_rmrs(imo):
    """Класс-данные из регистра РС (output/rmrs_events_<imo>.json) — надёжный источник."""
    imo = (str(imo) if imo else "").strip()
    if not imo:
        return None
    path = ROOT / "output" / f"rmrs_events_{imo}.json"
    if not path.exists():
        return None
    try:
        d = json.load(open(path, encoding="utf-8"))
    except Exception:
        return None
    vd = d.get("vessel_data", {})
    return {
        "rs_number": vd.get("RS Number") or vd.get("Регистровый номер", ""),
        "class_notation": vd.get("RS Class notation") or vd.get("Символ класса", ""),
        "built": vd.get("Дата постройки", ""),
        "keel_laid": vd.get("Дата закладки киля", ""),
        "build_country": vd.get("Страна постройки", ""),
        "hull_material": vd.get("Материал корпуса", ""),
        "port_registry": vd.get("Port of registry") or vd.get("Порт приписки", ""),
        "gross_tonnage": vd.get("Валовая вместимость", ""),
        "status": d.get("status", ""),
        "surveys_count": d.get("surveys_count", 0),
        "source_path": d.get("source_path", ""),
    }


def build_company_json(
    company_id: str,
    events_dir: Path,
    output_dir: Path,
    catch_file,
    out_file: Path,
    company_meta: dict,
):
    scorecard = {r["vessel"]: r for r in read_csv(output_dir / "fleet_scorecard.csv")}
    benchmark = {r["vessel"]: r for r in read_csv(output_dir / "fleet_benchmark.csv")}
    encounters = read_csv(output_dir / "encounters.csv")

    # External reliable sources
    gfw_by_name, gfw_by_imo = load_gfw_ids()
    inn = company_meta.get("inn", "")
    group_inns = company_meta.get("group_inns") or ({inn: company_meta.get("name", "")} if inn else {})
    quota_by_year, quota_by_entity = load_quota(group_inns)
    prices = load_prices(company_id)
    financials = load_financials(company_id)
    verification_flags = load_verification_flags()

    # Class surveys
    repairs_dir = output_dir.parent / "gfw_repairs"
    surveys_by_vessel = defaultdict(list)
    for r in read_csv(repairs_dir / "class_surveys.csv"):
        surveys_by_vessel[r["vessel"]].append(r)

    # Last yard repairs
    yards_by_vessel = defaultdict(list)
    for r in read_csv(repairs_dir / "repairs.csv"):
        if r.get("is_yard") == "True":
            yards_by_vessel[r["vessel"]].append(r)

    # Yearly KPI by vessel x year
    yearly_rows = read_csv(output_dir / "fleet_yearly.csv")
    yearly_kpi = defaultdict(dict)  # vessel -> {year -> {kpi fields}}
    for r in yearly_rows:
        vessel = r["vessel"]
        try:
            yr = int(r["year"])
        except Exception:
            continue
        yearly_kpi[vessel][yr] = {
            "fishing_intensity_%": parse_float(r.get("fishing_intensity_%")),
            "deployment_eff_%": parse_float(r.get("deployment_eff_%")),
            "availability_%": parse_float(r.get("availability_%")),
            "fishing_days": parse_float(r.get("fishing_days")),
            "at_sea_days": parse_float(r.get("at_sea_days")),
            "days_observed": parse_float(r.get("days_observed")),
            "repair_days": parse_float(r.get("repair_days")),
        }

    catch_ref = {}
    if catch_file and catch_file.exists():
        for r in read_csv(catch_file):
            catch_ref[r.get("vessel") or r.get("name", "")] = r

    # Years range from scorecard
    years_set = set()
    for r in scorecard.values():
        try:
            y_start = int(r["period_start"][:4])
            y_end = int(r["period_end"][:4])
            for y in range(y_start, y_end + 1):
                years_set.add(y)
        except Exception:
            pass
    years = sorted(years_set) if years_set else list(range(2012, 2027))

    # Transshipments by vessel x year
    transship_by_year = defaultdict(lambda: [0] * len(years))
    year_idx = {y: i for i, y in enumerate(years)}
    for enc in encounters:
        if enc.get("kind") != "transshipment":
            continue
        vessel = enc.get("vessel", "")
        try:
            yr = int(enc["start"][:4])
        except Exception:
            continue
        if yr in year_idx and vessel in scorecard:
            transship_by_year[vessel][year_idx[yr]] += 1

    # Build vessels list
    vessels = []
    for vessel, sc in scorecard.items():
        bm = benchmark.get(vessel, {})
        catch_r = catch_ref.get(vessel, {})

        vtype_raw = sc.get("peer_group", sc.get("vessel_type", ""))
        vtype = VESSEL_TYPE_MAP.get(vtype_raw, sc.get("vessel_type", vtype_raw))

        v = {
            "name": vessel,
            "short": short_name(vessel),
            "type": vtype,
            "ief": None,       # filled by compute_fleet_efficiency_index
            "ief_rank": None,  # filled by compute_fleet_efficiency_index
            "intensity": parse_float(sc.get("fishing_intensity_%")),
            "avail": parse_float(sc.get("availability_%")),
            "deploy": parse_float(sc.get("deployment_eff_%")),
            "autonomy": parse_float(sc.get("autonomy_days")),
            "repair_pct": parse_float(sc.get("repair_burden_%")),
            "repair_days": parse_float(sc.get("total_repair_days")),
            "window": sc.get("sales_window_combined") or sc.get("sales_window", ""),
            "months_to": parse_float(sc.get("months_to_next_maintenance") or sc.get("months_to_next_docking")),
            "last_dock": sc.get("last_docking", ""),
            "next_maint": sc.get("predicted_next_maintenance") or sc.get("predicted_next_docking", ""),
            "cycle": parse_float(sc.get("docking_cycle_months")),
            "imo": sc.get("imo", "—"),
            # catch fields (from scorecard or catch_ref)
            "catch": parse_float(sc.get("catch_total_t") or catch_r.get("catch_total_t")),
            "catch_pollock": parse_float(sc.get("catch_pollock_t") or catch_r.get("catch_pollock_t")),
            "catchDay": parse_float(sc.get("catch_per_fishing_day_t")),
            "seaDay": parse_float(sc.get("catch_per_seaday_t")),
            "offload_cadence": parse_float(sc.get("offload_cadence_days")),
            "offload_share": parse_float(sc.get("at_sea_offload_share_%")),
            # maintenance details
            "maintenance_driver": sc.get("maintenance_driver", ""),
            "next_class_survey_type": sc.get("next_class_survey_type", ""),
            "next_class_survey_date": sc.get("next_class_survey_date", ""),
            "next_mandatory_docking": sc.get("next_mandatory_docking_class", ""),
            "class_survey_status": sc.get("class_survey_status", ""),
            "total_layup_days": parse_float(sc.get("total_layup_days")),
            "dry_dock_count": parse_int(sc.get("dry_dock_repairs")),
            # verification: GFW map deeplink id + РС register cross-check
            "gfw_id": resolve_gfw_id(vessel, sc.get("imo"), gfw_by_name, gfw_by_imo),
            "rmrs": load_rmrs(sc.get("imo")),
            # due surveys
            "due_surveys": [
                {
                    "label": s["survey_label"],
                    "window_from": s["date_next_early"],
                    "window_to": s["date_next_late"],
                    "docking_required": s["docking_required"] == "True",
                    "status": s["status"],
                }
                for s in surveys_by_vessel.get(vessel, [])
                if s.get("is_due") == "True"
            ],
            # last 3 yard stops
            "last_yards": [
                {
                    "start": r["start"][:10],
                    "end": r["end"][:10],
                    "dur_days": parse_float(r.get("dur_days")),
                    "port": r.get("port", ""),
                    "port_flag": r.get("port_flag", ""),
                    "nearest_yard": r.get("nearest_yard", ""),
                    "event_kind": r.get("event_kind", ""),
                }
                for r in sorted(yards_by_vessel.get(vessel, []), key=lambda x: x["start"], reverse=True)[:3]
            ],
        }
        vessels.append(v)

    # Compute Индекс Эффективности Флота (ИЭФ)
    compute_fleet_efficiency_index(vessels)

    # Sort: trawlers by ИЭФ rank, factory last
    def sort_key(v):
        if v["ief_rank"] is None:
            return (1, 99)
        return (0, v["ief_rank"])
    vessels.sort(key=sort_key)

    # Catch as_of from scorecard
    catch_as_of = ""
    for sc in scorecard.values():
        v_cao = sc.get("catch_as_of", "")
        if v_cao:
            catch_as_of = v_cao
            break

    # Build yearly_kpi_by_year: vessel -> list indexed by years
    yearly_kpi_export = {}
    kpi_fields = ["fishing_intensity_%", "deployment_eff_%", "availability_%",
                  "fishing_days", "at_sea_days", "days_observed", "repair_days"]
    for vessel in scorecard:
        vdata = yearly_kpi.get(vessel, {})
        yearly_kpi_export[vessel] = {
            field: [vdata.get(y, {}).get(field) for y in years]
            for field in kpi_fields
        }

    # ---- Квоты группы: к общему знаменателю (год -> {total, by_species, by_entity}) ----
    quota_export = {}
    for yr, sp_map in quota_by_year.items():
        quota_export[str(yr)] = {
            "total_t": round(sum(sp_map.values()), 1),
            "by_species": {sp: round(t, 1) for sp, t in sorted(sp_map.items(), key=lambda x: -x[1])},
            "by_entity": {e: round(t, 1) for e, t in sorted(quota_by_entity.get(yr, {}).items(), key=lambda x: -x[1])},
        }

    # ---- Утилизация квоты за сезон вылова ----
    catch_year = catch_as_of[:4] if catch_as_of else ""
    fleet_catch_total = round(sum(v.get("catch") or 0 for v in vessels), 1)
    fleet_catch_pollock = round(sum(v.get("catch_pollock") or 0 for v in vessels), 1)
    quota_this_year = quota_by_year.get(int(catch_year)) if catch_year.isdigit() else None
    quota_mintai = round(quota_this_year.get("Минтай", 0), 1) if quota_this_year else None
    quota_total_year = round(sum(quota_this_year.values()), 1) if quota_this_year else None
    quota_util = {
        "year": catch_year,
        "as_of": catch_as_of,
        "fleet_catch_total_t": fleet_catch_total,
        "fleet_catch_pollock_t": fleet_catch_pollock,
        "quota_total_t": quota_total_year,
        "quota_mintai_t": quota_mintai,
        "util_total_pct": round(fleet_catch_total / quota_total_year * 100, 1) if quota_total_year else None,
        "util_mintai_pct": round(fleet_catch_pollock / quota_mintai * 100, 1) if quota_mintai else None,
        "remaining_mintai_t": round(quota_mintai - fleet_catch_pollock, 1) if quota_mintai else None,
    }

    # ---- Деньги: выручка-оценка = вылов × цена (если цены заданы) ----
    p_mintai = prices.get("Минтай", {})
    price_usd = p_mintai.get("usd")
    price_rub = p_mintai.get("rub")
    usd_rub = company_meta.get("usd_rub")  # курс-допущение для перекрёстного пересчёта
    if price_usd and not price_rub and usd_rub:
        price_rub = round(price_usd * usd_rub, 1)
    if price_rub and not price_usd and usd_rub:
        price_usd = round(price_rub / usd_rub, 1)
    has_price = bool(price_usd or price_rub)
    for v in vessels:
        c = v.get("catch") or 0
        v["revenue_usd_m"] = round(c * price_usd / 1_000_000, 2) if price_usd else None
        v["revenue_rub_m"] = round(c * price_rub / 1_000_000, 2) if price_rub else None

    money = {
        "has_price": has_price,
        "price_mintai_usd_per_t": price_usd,
        "price_mintai_rub_per_t": price_rub,
        "usd_rub": usd_rub,
        "price_as_of": p_mintai.get("as_of", ""),
        "price_source": p_mintai.get("source", ""),
        "prices_file": f"data/reference/{company_id}_prices.csv",
        "fleet_revenue_usd_m": round(fleet_catch_total * price_usd / 1_000_000, 2) if price_usd else None,
        "fleet_revenue_rub_m": round(fleet_catch_total * price_rub / 1_000_000, 2) if price_rub else None,
    }

    payload = {
        "company": company_meta.get("name", company_id.upper()),
        "full_name": company_meta.get("full_name", ""),
        "fleet_type": company_meta.get("fleet_type", ""),
        "inn": inn,
        "group_entities": list(group_inns.values()),
        "catch_as_of": catch_as_of,
        "catch_species": company_meta.get("catch_species", "Минтай"),
        "vessels": vessels,
        "transship_by_year": {k: v for k, v in transship_by_year.items()},
        "yearly_kpi": yearly_kpi_export,
        "years": years,
        "quota": quota_export,
        "quota_util": quota_util,
        "money": money,
        "financials": {str(y): f for y, f in financials.items()},
        "verification": {k: v for k, v in verification_flags.items()},
    }

    out_file.parent.mkdir(parents=True, exist_ok=True)
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"Written: {out_file}  ({len(vessels)} vessels)")


COMPANY_META = {
    "nbamr": {
        "name": "НБАМР",
        "full_name": "Находкинская база активного морского рыболовства",
        "fleet_type": "БМРТ + плавзавод",
        "catch_species": "Минтай",
        "inn": "2508007948",
        # Группа компаний — квоты агрегируются по всем юрлицам группы (важно для финпрофиля)
        "group_inns": {
            "2508007948": "ПАО НБАМР",
            "2540288411": "Кальмар-1",
            "6501268232": "Рускор",
        },
        "usd_rub": 90.0,  # курс-допущение для перекрёстного пересчёта цен (если задана одна валюта)
    }
}


def main():
    parser = argparse.ArgumentParser(description="Build dashboard JSON from GFW output CSVs")
    parser.add_argument("--company", default="nbamr")
    parser.add_argument("--events-dir", default=None, help="Path to events CSV dir (unused, for reference)")
    parser.add_argument("--output-dir", default=None, help="Path to gfw_fleet output dir")
    parser.add_argument("--catch-file", default=None, help="Optional catch reference CSV")
    parser.add_argument("--out", default=None, help="Output JSON path")
    args = parser.parse_args()

    company = args.company
    output_dir = Path(args.output_dir) if args.output_dir else ROOT / "output" / "gfw_fleet"
    catch_file = Path(args.catch_file) if args.catch_file else ROOT / "data" / "reference" / f"{company}_vessel_catch.csv"
    out_file = Path(args.out) if args.out else ROOT / "dashboards" / "data" / f"{company}.json"

    meta = COMPANY_META.get(company, {"name": company.upper(), "full_name": "", "fleet_type": "", "catch_species": "Минтай"})

    build_company_json(
        company_id=company,
        events_dir=ROOT / "data" / f"{company}_events",
        output_dir=output_dir,
        catch_file=catch_file,
        out_file=out_file,
        company_meta=meta,
    )


if __name__ == "__main__":
    main()
