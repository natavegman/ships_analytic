#!/usr/bin/env python3
"""
Второй loader: раскладывает surveys из rmrs_events_staging в rmrs_surveys_staging.

Одна строка = одно освидетельствование (survey) по IMO.

Запуск:
  python3 scripts/load_rmrs_surveys_staging.py
  python3 scripts/load_rmrs_surveys_staging.py --imo 9157820
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


def _load_session_local():
    import importlib.util

    path = BASE_DIR / "database" / "db_session.py"
    spec = importlib.util.spec_from_file_location("quotas_db_session", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load db session module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SessionLocal


from sqlalchemy import text

def _session():
    return _load_session_local()()

CREATE_SURVEYS_STAGING_SQL = """
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
"""

DELETE_BY_IMO_SQL = text("DELETE FROM rmrs_surveys_staging WHERE imo = :imo")

INSERT_SURVEY_SQL = text(
    """
    INSERT INTO rmrs_surveys_staging (
        imo, survey_type, survey_name, survey_code, date_last, date_next,
        postponement, survey_status, row_css_class, fetched_at
    )
    VALUES (
        :imo, :survey_type, :survey_name, :survey_code, :date_last, :date_next,
        :postponement, :survey_status, :row_css_class, :fetched_at
    )
    """
)

SELECT_EVENTS_SQL_TEMPLATE = """
    SELECT imo, payload, fetched_at, source_status
    FROM rmrs_events_staging
    WHERE source_status IN ('ok', 'ok_via_regbook')
    {imo_filter}
    ORDER BY imo
    """


def ensure_surveys_table() -> None:
    with _session() as session:
        session.execute(text(CREATE_SURVEYS_STAGING_SQL))
        session.commit()


def _survey_rows_from_payload(imo: int, payload: dict[str, Any], fetched_at: datetime) -> list[dict[str, Any]]:
    surveys = payload.get("surveys")
    if not isinstance(surveys, list):
        return []

    rows: list[dict[str, Any]] = []
    for item in surveys:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "imo": imo,
                "survey_type": str(item.get("Type", "")).strip(),
                "survey_name": str(item.get("Survey", "")).strip(),
                "survey_code": str(item.get("Code", "")).strip(),
                "date_last": str(item.get("Date of last survey", "")).strip(),
                "date_next": str(item.get("Date / time the next survey", "")).strip(),
                "postponement": str(item.get("Postponement", "")).strip(),
                "survey_status": str(item.get("Status", "")).strip(),
                "row_css_class": str(item.get("row_css_class", "")).strip(),
                "fetched_at": fetched_at,
            }
        )
    return rows


def _synthetic_rows_from_regbook_payload(imo: int, payload: dict[str, Any], fetched_at: datetime) -> list[dict[str, Any]]:
    vessel_data = payload.get("vessel_data")
    if not isinstance(vessel_data, dict):
        return []
    class_status = str(vessel_data.get("Class status", "")).strip()
    class_notation = str(vessel_data.get("RS Class notation", "")).strip()
    if not class_status and not class_notation:
        return []
    return [
        {
            "imo": imo,
            "survey_type": "Regbook",
            "survey_name": "Class status snapshot",
            "survey_code": "REGBOOK.CLASS_STATUS",
            "date_last": class_status,
            "date_next": "",
            "postponement": "",
            "survey_status": class_notation,
            "row_css_class": "synthetic_regbook",
            "fetched_at": fetched_at,
        }
    ]


def load_surveys(imo: int | None = None, dry_run: bool = False) -> None:
    imo_filter = "AND imo = :imo" if imo is not None else ""
    query = text(SELECT_EVENTS_SQL_TEMPLATE.format(imo_filter=imo_filter))

    if not dry_run:
        ensure_surveys_table()

    total_events = 0
    total_surveys = 0

    with _session() as session:
        params: dict[str, Any] = {}
        if imo is not None:
            params["imo"] = imo
        events = session.execute(query, params).mappings().all()

        if not events:
            print("[INFO] Нет записей source_status IN (ok, ok_via_regbook) в rmrs_events_staging.")
            return

        for event in events:
            event_imo = int(event["imo"])
            payload_raw = event["payload"]
            if isinstance(payload_raw, str):
                payload = json.loads(payload_raw)
            elif isinstance(payload_raw, dict):
                payload = payload_raw
            else:
                payload = {}

            fetched_at = event.get("fetched_at") or datetime.now(timezone.utc)
            source_status = str(event.get("source_status", "")).strip()
            survey_rows = _survey_rows_from_payload(event_imo, payload, fetched_at)
            if not survey_rows and source_status == "ok_via_regbook":
                survey_rows = _synthetic_rows_from_regbook_payload(event_imo, payload, fetched_at)
            total_events += 1
            total_surveys += len(survey_rows)

            if dry_run:
                print(f"  IMO={event_imo} surveys={len(survey_rows)}")
                continue

            session.execute(DELETE_BY_IMO_SQL, {"imo": event_imo})
            if survey_rows:
                session.execute(INSERT_SURVEY_SQL, survey_rows)

        if not dry_run:
            session.commit()

    print(
        "[DONE] "
        f"events={total_events} surveys={total_surveys} "
        f"mode={'dry_run' if dry_run else 'write_db'}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Раскладка RMRS surveys в rmrs_surveys_staging.")
    parser.add_argument("--imo", type=int, default=None, help="Обработать только один IMO.")
    parser.add_argument("--dry-run", action="store_true", help="Без записи в БД.")
    args = parser.parse_args()

    load_surveys(imo=args.imo, dry_run=bool(args.dry_run))


if __name__ == "__main__":
    main()
