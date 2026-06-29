#!/usr/bin/env python3
"""
Пакетная загрузка треков судов из Global Fishing Watch по vessel_id.

Источники (режим --mode):
  auto   — сначала public-global-all-tracks (как выгрузка с карты GFW),
           при 403 → fallback на AIS Presence (1 точка/час, без speed из AIS)
  tracks — только tracks API (нужен Advanced-доступ к all-tracks)
  presence — только hourly presence (доступен на базовом токене)

Список судов:
  --from-cache     data/gfw_our_vessels.json (gfw_id не пустой)
  --ids ID1,ID2    явный список vessel_id
  --vessels-file   CSV/JSON со столбцом gfw_id или vessel_id

Выход:
  data/gfw_tracks/{vessel_id}/track_{start}_{end}.csv
  data/gfw_tracks/manifest.csv

Примеры:
  .venv/bin/python scripts/fetch_gfw_tracks_batch.py \\
    --from-cache --limit 5 --start 2026-01-01 --end 2026-04-14

  .venv/bin/python scripts/fetch_gfw_tracks_batch.py \\
    --ids 43663694e-ede3-2e29-e8d1-b27d830d0e5b \\
    --start 2026-01-01 --end 2026-04-14 --mode auto

  .venv/bin/python scripts/fetch_gfw_tracks_batch.py \\
    --from-cache --group "ГК РРПК" --start 2026-01-01 --end 2026-03-31
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_env = ROOT / ".env"
if _env.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_env)
    except ImportError:
        import os
        for line in _env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip().strip('"').strip("'")

from scripts.gfw_client import (
    GfwApiError,
    fetch_vessel_presence_hourly,
    fetch_vessel_tracks_raw,
    get_token,
    parse_tracks_response,
    presence_rows_to_track_points,
    split_date_range,
    tracks_access_available,
)

DEFAULT_OUT = ROOT / "data" / "gfw_tracks"
MANIFEST_COLS = [
    "fetched_at", "vessel_id", "vessel_name", "inn", "company",
    "start_date", "end_date", "source", "points", "path", "status", "error",
]

# Пресеты bbox (west, south, east, north) для presence fallback
BBOX_PRESETS: dict[str, tuple[float, float, float, float]] = {
  # Дальний Восток / Охотское / Берингово — основной промысел РФ
  "russia-pacific": (130.0, 42.0, 180.0, 66.0),
  # Камчатка + Охотское (уже для одного судна)
  "kamchatka": (145.0, 48.0, 165.0, 58.0),
  # Сахалин / Охотское
  "sakhalin": (136.0, 44.0, 156.0, 56.0),
  # Западная Атлантика (Мурманск / Норвегия) — для северного флота
  "barents": (10.0, 68.0, 60.0, 82.0),
}


def load_vessels_from_cache(limit: int | None = None, group: str | None = None) -> list[dict]:
    path = ROOT / "data" / "gfw_our_vessels.json"
    rows = json.loads(path.read_text(encoding="utf-8"))
    out = []
    group_inns: set[str] | None = None
    if group:
        groups_path = ROOT / "data" / "company_groups_enriched.csv"
        if groups_path.exists():
            gdf = pd.read_csv(groups_path, dtype=str)
            mask = gdf["Группа_Компаний"].fillna("").str.contains(group, case=False, regex=False)
            group_inns = set(gdf.loc[mask, "ИНН"].dropna().astype(str))
    for r in rows:
        vid = (r.get("gfw_id") or "").strip()
        if not vid:
            continue
        if group_inns is not None and str(r.get("inn") or "") not in group_inns:
            continue
        out.append({
            "vessel_id": vid,
            "name": r.get("gfw_name") or r.get("name") or "",
            "inn": r.get("inn") or "",
            "company": r.get("company") or "",
        })
    if limit:
        out = out[:limit]
    return out


def load_vessels_from_file(path: Path) -> list[dict]:
    if path.suffix.lower() == ".json":
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            raw = raw.get("vessels") or raw.get("entries") or [raw]
    else:
        df = pd.read_csv(path, dtype=str)
        col = next(
            (c for c in df.columns if c.lower() in ("gfw_id", "vessel_id", "vesselid")),
            None,
        )
        if not col:
            raise ValueError(f"В {path} нет колонки gfw_id / vessel_id")
        raw = df.to_dict("records")
        for r in raw:
            r["vessel_id"] = r.get(col)
    out = []
    for r in raw:
        vid = str(r.get("vessel_id") or r.get("gfw_id") or "").strip()
        if vid:
            out.append({
                "vessel_id": vid,
                "name": r.get("name") or r.get("gfw_name") or r.get("shipName") or "",
                "inn": r.get("inn") or "",
                "company": r.get("company") or "",
            })
    return out


def parse_bbox(s: str) -> tuple[float, float, float, float]:
    if s in BBOX_PRESETS:
        return BBOX_PRESETS[s]
    parts = [float(x.strip()) for x in s.split(",")]
    if len(parts) != 4:
        raise ValueError("bbox: west,south,east,north или пресет kamchatka|russia-pacific|...")
    return parts[0], parts[1], parts[2], parts[3]


def write_track_csv(path: Path, points: list[dict]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["lon", "lat", "course", "timestamp", "speed", "depth", "seg_id"]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for p in points:
            w.writerow({k: p.get(k, "") for k in cols})
    return len(points)


def write_readme(path: Path, meta: dict) -> None:
    lines = [
        "# GFW track export",
        "",
        f"- vessel_id: {meta.get('vessel_id')}",
        f"- period: {meta.get('start_date')} → {meta.get('end_date')}",
        f"- source: {meta.get('source')}",
        f"- points: {meta.get('points')}",
        f"- fetched_at: {meta.get('fetched_at')}",
        "",
        "Для полного трека (speed, depth, seg_id как на карте GFW) нужен доступ к",
        "dataset public-global-all-tracks (Advanced). При source=presence_hourly —",
        "1 позиция/час, speed/course вычислены по соседним точкам.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def append_manifest(manifest_path: Path, row: dict) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not manifest_path.exists()
    with manifest_path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_COLS, extrasaction="ignore")
        if write_header:
            w.writeheader()
        w.writerow(row)


def fetch_one_period(
    vessel: dict,
    start: str,
    end: str,
    *,
    mode: str,
    bbox: tuple[float, float, float, float],
    out_dir: Path,
    force: bool,
    tracks_ok: bool | None,
) -> dict:
    vid = vessel["vessel_id"]
    out_path = out_dir / vid / f"track_{start}_{end}.csv"
    fetched_at = datetime.now(timezone.utc).isoformat()

    if out_path.exists() and not force:
        n = sum(1 for _ in open(out_path, encoding="utf-8")) - 1
        return {
            "fetched_at": fetched_at,
            "vessel_id": vid,
            "vessel_name": vessel.get("name", ""),
            "inn": vessel.get("inn", ""),
            "company": vessel.get("company", ""),
            "start_date": start,
            "end_date": end,
            "source": "cached",
            "points": n,
            "path": str(out_path.relative_to(ROOT)),
            "status": "skipped",
            "error": "",
        }

    use_tracks = mode == "tracks" or (mode == "auto" and tracks_ok)
    points: list[dict] = []
    source = ""
    err = ""

    try:
        if use_tracks:
            ctype, payload = fetch_vessel_tracks_raw(vid, start, end, fmt="CSV")
            points = parse_tracks_response(payload, vid, content_type=ctype)
            source = "all-tracks"
            if not points:
                # иногда CSV пустой — пробуем JSON
                ctype, payload = fetch_vessel_tracks_raw(vid, start, end, fmt="JSON")
                points = parse_tracks_response(payload, vid, content_type=ctype)
        else:
            rows = fetch_vessel_presence_hourly(vid, start, end, bbox)
            points = presence_rows_to_track_points(rows, vid)
            source = "presence_hourly"
    except GfwApiError as e:
        if e.status_code == 403 and mode == "auto":
            rows = fetch_vessel_presence_hourly(vid, start, end, bbox)
            points = presence_rows_to_track_points(rows, vid)
            source = "presence_hourly"
        else:
            err = str(e)
    except Exception as e:
        err = str(e)

    status = "ok" if points and not err else "error"
    if points and not err:
        n = write_track_csv(out_path, points)
        write_readme(out_path.with_suffix(".README.md"), {
            "vessel_id": vid,
            "start_date": start,
            "end_date": end,
            "source": source,
            "points": n,
            "fetched_at": fetched_at,
        })
    else:
        n = 0

    return {
        "fetched_at": fetched_at,
        "vessel_id": vid,
        "vessel_name": vessel.get("name", ""),
        "inn": vessel.get("inn", ""),
        "company": vessel.get("company", ""),
        "start_date": start,
        "end_date": end,
        "source": source,
        "points": n,
        "path": str(out_path.relative_to(ROOT)) if n else "",
        "status": status,
        "error": err,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Пакетная загрузка GFW-треков по vessel_id")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--from-cache", action="store_true", help="Суда из gfw_our_vessels.json")
    src.add_argument("--ids", help="Список vessel_id через запятую")
    src.add_argument("--vessels-file", type=Path, help="CSV/JSON со столбцом gfw_id")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--mode", choices=["auto", "tracks", "presence"], default="auto")
    ap.add_argument("--bbox", default="russia-pacific", help="Пресет или west,south,east,north")
    ap.add_argument("--chunk-days", type=int, default=90, help="Длина интервала запроса (дней)")
    ap.add_argument("--outdir", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--limit", type=int, help="Макс. число судов")
    ap.add_argument("--group", help="Фильтр по Группа_Компаний (подстрока)")
    ap.add_argument("--force", action="store_true", help="Перекачать даже если файл есть")
    ap.add_argument("--sleep", type=float, default=1.0, help="Пауза между запросами (сек)")
    args = ap.parse_args()

    if not get_token():
        print("Ошибка: задайте GFW_API_TOKEN в .env", file=sys.stderr)
        return 1

    if args.from_cache:
        vessels = load_vessels_from_cache(limit=args.limit, group=args.group)
    elif args.ids:
        vessels = [
            {"vessel_id": x.strip(), "name": "", "inn": "", "company": ""}
            for x in args.ids.split(",") if x.strip()
        ]
        if args.limit:
            vessels = vessels[: args.limit]
    else:
        vessels = load_vessels_from_file(args.vessels_file)
        if args.limit:
            vessels = vessels[: args.limit]

    if not vessels:
        print("Нет судов для загрузки.")
        return 1

    bbox = parse_bbox(args.bbox)
    chunks = split_date_range(args.start, args.end, max_days=args.chunk_days)
    manifest_path = args.outdir / "manifest.csv"

    tracks_ok: bool | None = None
    if args.mode == "auto":
        tracks_ok = tracks_access_available(vessels[0]["vessel_id"])
        print(
            "Доступ к public-global-all-tracks:",
            "да" if tracks_ok else "нет (будет presence hourly)",
        )

    print(
        f"Судов: {len(vessels)} | период: {args.start}→{args.end} "
        f"({len(chunks)} чанк(ов)) | mode={args.mode} | bbox={args.bbox}"
    )

    ok = err_count = 0
    for i, vessel in enumerate(vessels, 1):
        name = vessel.get("name") or vessel["vessel_id"][:8]
        print(f"\n[{i}/{len(vessels)}] {name} ({vessel['vessel_id']})")
        for start, end in chunks:
            row = fetch_one_period(
                vessel, start, end,
                mode=args.mode,
                bbox=bbox,
                out_dir=args.outdir,
                force=args.force,
                tracks_ok=tracks_ok,
            )
            append_manifest(manifest_path, row)
            if row["status"] == "ok":
                ok += 1
                print(f"  ✓ {start}→{end}: {row['points']} точек ({row['source']})")
            elif row["status"] == "skipped":
                print(f"  · {start}→{end}: кэш ({row['points']} точек)")
            else:
                err_count += 1
                print(f"  ✗ {start}→{end}: {row['error'] or 'нет точек'}")
            time.sleep(args.sleep)

    print(f"\nГотово. Успешно: {ok}, ошибок: {err_count}")
    print(f"Манифест: {manifest_path}")
    print("Анализ: .venv/bin/python scripts/gwf_analytic.py --input data/gfw_tracks/<vessel_id>/track_....csv")
    return 0 if err_count == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
