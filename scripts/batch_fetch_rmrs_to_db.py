#!/usr/bin/env python3
"""
Batch-runner: берет IMO из таблицы vessels, запрашивает RMRS и пишет в staging.

Таблица создается автоматически:
  rmrs_events_staging

Запуск:
  python3 scripts/batch_fetch_rmrs_to_db.py --limit 50 --dry-run
  python3 scripts/batch_fetch_rmrs_to_db.py --limit 200
  python3 scripts/batch_fetch_rmrs_to_db.py --limit 200 --expand-surveys
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

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

from scripts.fetch_rmrs_events_template import extract_rmrs_payload


CREATE_STAGING_SQL = """
CREATE TABLE IF NOT EXISTS rmrs_events_staging (
    id BIGSERIAL PRIMARY KEY,
    imo BIGINT NOT NULL UNIQUE,
    vessel_name TEXT,
    source_status TEXT NOT NULL,         -- ok | ok_via_regbook | not_access | error
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

CREATE TABLE IF NOT EXISTS vessel_names_history (
    id BIGSERIAL PRIMARY KEY,
    imo BIGINT NOT NULL,
    vessel_name TEXT NOT NULL,
    source TEXT NOT NULL, -- vessels | rmrs
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (imo, vessel_name, source)
);
CREATE INDEX IF NOT EXISTS ix_vessel_names_history_imo ON vessel_names_history (imo);
CREATE INDEX IF NOT EXISTS ix_vessel_names_history_source ON vessel_names_history (source);
"""


UPSERT_SQL = text(
    """
    INSERT INTO rmrs_events_staging (
        imo, vessel_name, vessel_name_rmrs, source_status, rmrs_number, class_status, surveys_count,
        message, error_text, payload, fetched_at
    )
    VALUES (
        :imo, :vessel_name, :vessel_name_rmrs, :source_status, :rmrs_number, :class_status, :surveys_count,
        :message, :error_text, CAST(:payload AS JSONB), :fetched_at
    )
    ON CONFLICT (imo) DO UPDATE SET
        vessel_name = EXCLUDED.vessel_name,
        vessel_name_rmrs = EXCLUDED.vessel_name_rmrs,
        source_status = EXCLUDED.source_status,
        rmrs_number = EXCLUDED.rmrs_number,
        class_status = EXCLUDED.class_status,
        surveys_count = EXCLUDED.surveys_count,
        message = EXCLUDED.message,
        error_text = EXCLUDED.error_text,
        payload = EXCLUDED.payload,
        fetched_at = EXCLUDED.fetched_at
    """
)

UPSERT_NAME_HISTORY_SQL = text(
    """
    INSERT INTO vessel_names_history (imo, vessel_name, source, first_seen_at, last_seen_at)
    VALUES (:imo, :vessel_name, :source, :seen_at, :seen_at)
    ON CONFLICT (imo, vessel_name, source) DO UPDATE SET
        last_seen_at = EXCLUDED.last_seen_at
    """
)


def _session():
    return _load_session_local()()


def ensure_staging_table() -> None:
    with _session() as session:
        session.execute(text(CREATE_STAGING_SQL))
        session.commit()


def fetch_imos(limit: int, offset: int) -> list[tuple[int, str]]:
    with _session() as session:
        rows = session.execute(
            text(
                """
                SELECT imo, name
                FROM vessels
                ORDER BY imo
                OFFSET :offset
                LIMIT :limit
                """
            ),
            {"offset": offset, "limit": limit},
        ).fetchall()
    return [(int(imo), (name or "").strip()) for imo, name in rows]


def run_batch(limit: int, offset: int, sleep_sec: float, dry_run: bool, expand_surveys: bool) -> None:
    imos = fetch_imos(limit=limit, offset=offset)
    if not imos:
        print("[INFO] В таблице vessels нет IMO для обработки.")
        return

    if not dry_run:
        ensure_staging_table()

    ok = 0
    not_access = 0
    error = 0

    print(f"[INFO] К обработке IMO: {len(imos)} (offset={offset}, limit={limit}, dry_run={dry_run})")

    for idx, (imo, vessel_name) in enumerate(imos, start=1):
        status = "error"
        error_text = ""
        payload: dict = {}
        try:
            payload = extract_rmrs_payload(str(imo))
            status = str(payload.get("status", "error"))
        except Exception as exc:  # noqa: BLE001
            status = "error"
            error_text = str(exc)
            payload = {"imo": str(imo), "status": "error", "message": error_text}

        if status in {"ok", "ok_via_regbook"}:
            ok += 1
        elif status == "not_access":
            not_access += 1
        else:
            error += 1

        vessel_data = payload.get("vessel_data", {}) if isinstance(payload, dict) else {}
        vessel_name_rmrs = str(vessel_data.get("Name of vessel", "")).strip()
        seen_at = datetime.now(timezone.utc)
        row = {
            "imo": imo,
            "vessel_name": vessel_name,
            "vessel_name_rmrs": vessel_name_rmrs,
            "source_status": status,
            "rmrs_number": str(vessel_data.get("RS Number", "")),
            "class_status": str(vessel_data.get("Class status", "")),
            "surveys_count": int(payload.get("surveys_count", 0) or 0),
            "message": str(payload.get("message", "")),
            "error_text": error_text,
            "payload": json.dumps(payload, ensure_ascii=False),
            "fetched_at": seen_at,
        }

        if dry_run:
            print(f"  [{idx}/{len(imos)}] IMO={imo} status={status} surveys={row['surveys_count']}")
        else:
            with _session() as session:
                session.execute(UPSERT_SQL, row)
                if vessel_name:
                    session.execute(
                        UPSERT_NAME_HISTORY_SQL,
                        {"imo": imo, "vessel_name": vessel_name, "source": "vessels", "seen_at": seen_at},
                    )
                if vessel_name_rmrs:
                    session.execute(
                        UPSERT_NAME_HISTORY_SQL,
                        {"imo": imo, "vessel_name": vessel_name_rmrs, "source": "rmrs", "seen_at": seen_at},
                    )
                session.commit()
            print(f"  [{idx}/{len(imos)}] IMO={imo} status={status} -> upserted")

        if sleep_sec > 0:
            time.sleep(sleep_sec)

    print(
        "[DONE] "
        f"total={len(imos)} ok={ok} not_access={not_access} error={error} "
        f"mode={'dry_run' if dry_run else 'write_db'}"
    )

    if expand_surveys and not dry_run and ok > 0:
        print("[INFO] Раскладка surveys -> rmrs_surveys_staging ...")
        from scripts.load_rmrs_surveys_staging import load_surveys

        load_surveys(dry_run=False)


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch выгрузка RMRS в staging-таблицу PostgreSQL.")
    parser.add_argument("--limit", type=int, default=100, help="Сколько IMO взять из vessels.")
    parser.add_argument("--offset", type=int, default=0, help="Сдвиг по списку vessels.")
    parser.add_argument("--sleep-sec", type=float, default=0.1, help="Пауза между IMO-запросами.")
    parser.add_argument("--dry-run", action="store_true", help="Только запросы RMRS без записи в БД.")
    parser.add_argument(
        "--expand-surveys",
        action="store_true",
        help="После batch вызвать load_rmrs_surveys_staging для всех ok-записей.",
    )
    args = parser.parse_args()

    run_batch(
        limit=max(0, args.limit),
        offset=max(0, args.offset),
        sleep_sec=max(0.0, args.sleep_sec),
        dry_run=bool(args.dry_run),
        expand_surveys=bool(args.expand_surveys),
    )


if __name__ == "__main__":
    main()
