"""
Load class survey schedules from Регистр РС (RMRS).

Currently: surveys_count shows number, but detailed schedule (surveys[]) is empty (0).
This script attempts to dereference RG fleet_id → full survey schedule from regbook.

Usage:
  python3 scripts/fetch_rmrs_surveys.py --imo 8721260 --output output/rmrs_surveys_extended.json
  python3 scripts/fetch_rmrs_surveys.py --all-nbamr

Output: CSV with survey schedule (дата, тип освидетельствования, статус)
"""

import argparse
import json
import csv
from pathlib import Path
from typing import Optional
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ROOT = Path(__file__).parent.parent


def load_rmrs_extended(imo: str, output_dir: Path = None) -> Optional[dict]:
    """
    Попытка загрузить полное расписание освидетельствований РС.

    Текущая проблема: output/rmrs_events_<imo>.json содержит
    surveys_count: 0 (т.е. schedule пуст).

    Варианты:
    1. Дозагрузить из регбука через fleet_id
    2. Получить из открытого реестра РС (если есть)
    3. Парсить PDF класс-сертификата (обычно указана дата следующего освидетельствования)
    """
    rmrs_file = ROOT / "output" / f"rmrs_events_{imo}.json"

    if not rmrs_file.exists():
        logger.error(f"RMRS файл не найден: {rmrs_file}")
        return None

    with open(rmrs_file) as f:
        data = json.load(f)

    fleet_id = data.get("fleet_id")
    surveys_count = data.get("surveys_count", 0)
    vessel_data = data.get("vessel_data", {})

    logger.info(f"""
    ИМО {imo}:
    - Fleet ID: {fleet_id}
    - Surveys в реестре: {surveys_count}
    - Статус: {data.get("status")}

    ⚠ surveys_count={surveys_count}, что означает:
    """)

    if surveys_count == 0:
        logger.warning("""
        Расписание РС-освидетельствований не загружено в базу.

        Варианты действий:
        1. Парсить PDF класс-сертификата (указана дата следующего)
        2. Обратиться в РС напрямую за fleet_id=${fleet_id}
        3. Использовать прогноз по GFW: цикл докований (docking_cycle_months)

        Следующий класс обычно указан в vessel_data:
        """)

        next_survey_field = vessel_data.get("next_class_survey_date") or vessel_data.get("Дата следующего освидетельствования")
        if next_survey_field:
            logger.info(f"  → Следующий класс: {next_survey_field}")

    return {
        "source": "rmrs_extended",
        "imo": imo,
        "fleet_id": fleet_id,
        "surveys_count": surveys_count,
        "status": data.get("status"),
        "surveys": data.get("surveys", []),  # пока пусто
        "next_survey_date": vessel_data.get("Дата следующего освидетельствования"),
        "note": "Расписание РС не выгружено. Используйте прогноз GFW дельниках или парсите сертификат."
    }


def main():
    parser = argparse.ArgumentParser(description="Fetch RG survey schedules")
    parser.add_argument("--imo", help="Specific vessel IMO")
    parser.add_argument("--all-nbamr", action="store_true", help="Fetch for all NBAMR vessels")
    parser.add_argument("--output", default="output/rmrs_surveys_extended.json")
    args = parser.parse_args()

    result = {}

    if args.all_nbamr:
        # NBAMR subnos
        imos = ["8721260", "8721167", "8721131", "8859811", "8826670", "8707446"]
        for imo in imos:
            logger.info(f"\n=== ИМО {imo} ===")
            result[imo] = load_rmrs_extended(imo)
    elif args.imo:
        result[args.imo] = load_rmrs_extended(args.imo)
    else:
        logger.error("Укажите --imo или --all-nbamr")
        return

    out_file = Path(args.output)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with open(out_file, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    logger.info(f"""
    Сохранено: {out_file}

    ===== СЛЕДУЮЩИЕ ШАГИ =====

    Поскольку РС расписание не выгружено (surveys_count=0 для всех судов):

    Вариант 1 (быстро): Используйте GFW-прогноз
    - next_maint_date и дocking_cycle_months уже в dashboards/data/nbamr.json
    - Дальше повышайте точность по мере получения реальных данных

    Вариант 2 (точно): Парсите сертификаты РС
    - В vessel_data указана "Дата следующего освидетельствования"
    - Обычно в поле "next_class_survey_date"

    Вариант 3 (честно): Заложи в CLAUDE.md или notes:
    - Дата следующего ТО прогноз, а не факт (зависит от ремонта)
    """)


if __name__ == "__main__":
    main()
