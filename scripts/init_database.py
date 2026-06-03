#!/usr/bin/env python3
"""
Создает схему PostgreSQL: ORM-таблицы + RMRS staging.

Запуск (после docker compose up):
  python3 scripts/init_database.py
  python3 scripts/init_database.py --seed-vessels
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert

from database.db_session import get_engine, session_scope
from database.models import Base, Vessel

RMRS_STAGING_SQL = """
CREATE TABLE IF NOT EXISTS rmrs_events_staging (
    id BIGSERIAL PRIMARY KEY,
    imo BIGINT NOT NULL UNIQUE,
    vessel_name TEXT,
    source_status TEXT NOT NULL,
    rmrs_number TEXT,
    class_status TEXT,
    surveys_count INTEGER NOT NULL DEFAULT 0,
    message TEXT,
    error_text TEXT,
    payload JSONB NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE rmrs_events_staging ADD COLUMN IF NOT EXISTS vessel_name_rmrs TEXT;
CREATE INDEX IF NOT EXISTS ix_rmrs_events_staging_status ON rmrs_events_staging (source_status);
CREATE INDEX IF NOT EXISTS ix_rmrs_events_staging_fetched_at ON rmrs_events_staging (fetched_at);

CREATE TABLE IF NOT EXISTS rmrs_surveys_staging (
    id BIGSERIAL PRIMARY KEY,
    imo BIGINT NOT NULL,
    survey_type TEXT,
    survey_name TEXT,
    survey_code TEXT,
    date_last TEXT,
    date_next TEXT,
    postponement TEXT,
    survey_status TEXT,
    row_css_class TEXT,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_rmrs_surveys_staging_imo ON rmrs_surveys_staging (imo);
CREATE INDEX IF NOT EXISTS ix_rmrs_surveys_staging_code ON rmrs_surveys_staging (survey_code);
CREATE INDEX IF NOT EXISTS ix_rmrs_surveys_staging_status ON rmrs_surveys_staging (survey_status);

CREATE TABLE IF NOT EXISTS vessel_names_history (
    id BIGSERIAL PRIMARY KEY,
    imo BIGINT NOT NULL,
    vessel_name TEXT NOT NULL,
    source TEXT NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (imo, vessel_name, source)
);
CREATE INDEX IF NOT EXISTS ix_vessel_names_history_imo ON vessel_names_history (imo);
CREATE INDEX IF NOT EXISTS ix_vessel_names_history_source ON vessel_names_history (source);
"""

GFW_VESSELS_JSON = BASE_DIR / "data" / "gfw_our_vessels.json"


def create_schema() -> None:
    engine = get_engine()
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text(RMRS_STAGING_SQL))
    print("[OK] Схема создана: ORM-таблицы + rmrs_*_staging")


def seed_vessels_from_gfw() -> int:
    if not GFW_VESSELS_JSON.exists():
        print(f"[WARN] Нет файла {GFW_VESSELS_JSON}, пропуск seed vessels.")
        return 0

    items = json.loads(GFW_VESSELS_JSON.read_text(encoding="utf-8"))
    rows: list[dict] = []
    seen_imos: set[int] = set()

    for item in items:
        if not isinstance(item, dict):
            continue
        imo_raw = item.get("imo")
        if imo_raw in (None, ""):
            continue
        try:
            imo = int(str(imo_raw).strip())
        except ValueError:
            continue
        if imo in seen_imos:
            continue
        seen_imos.add(imo)

        name = str(item.get("gfw_name") or item.get("name") or f"IMO {imo}").strip()
        if not name:
            name = f"IMO {imo}"

        rows.append(
            {
                "imo": imo,
                "name": name[:255],
                "project": None,
                "base_owner_inn": None,
                "gfw_id": (str(item.get("gfw_id")).strip() if item.get("gfw_id") else None),
            }
        )

    if not rows:
        print("[WARN] В gfw_our_vessels.json нет записей с IMO.")
        return 0

    stmt = insert(Vessel).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Vessel.imo],
        set_={
            "name": stmt.excluded.name,
            "gfw_id": stmt.excluded.gfw_id,
        },
    )

    with session_scope() as session:
        session.execute(stmt)

    print(f"[OK] Загружено/обновлено судов в vessels: {len(rows)}")
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Инициализация PostgreSQL схемы Quotas_analytic.")
    parser.add_argument("--seed-vessels", action="store_true", help="Загрузить vessels из data/gfw_our_vessels.json.")
    args = parser.parse_args()

    create_schema()
    if args.seed_vessels:
        seed_vessels_from_gfw()


if __name__ == "__main__":
    main()
