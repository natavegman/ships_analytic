#!/usr/bin/env python3
from __future__ import annotations

import os
import re
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy.dialects.postgresql import insert

from database.db_session import SessionLocal
from database.models import Company
from scripts.dadata_client import DaDataEnricher


BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_CSV = BASE_DIR / "data" / "company_groups.csv"
ARTIFACT_PATTERN = "Проверка контрагента"


def _normalize_inn(value: object) -> str:
    inn = re.sub(r"\D", "", str(value or ""))
    return inn if len(inn) in (10, 12) else ""


def _infer_role_from_comment(comment: str) -> str:
    c = (comment or "").lower()
    if "банк" in c:
        return "Банк"
    if "торговый дом" in c:
        return "Торговый дом"
    if "без квот" in c:
        return "Без квот"
    return "Добыча"


def load_legacy_companies(csv_path: Path) -> pd.DataFrame:
    df = pd.read_csv(csv_path, dtype=str).fillna("")
    for col in ("Юр_Лицо", "Группа_Компаний", "Комментарий", "Роль_в_холдинге"):
        if col not in df.columns:
            df[col] = ""
    if "ИНН" not in df.columns:
        raise ValueError("В исходном CSV отсутствует колонка 'ИНН'")
    df["ИНН"] = df["ИНН"].apply(_normalize_inn)
    return df[df["ИНН"] != ""].copy()


def enrich_with_dadata(df: pd.DataFrame, enricher: DaDataEnricher) -> pd.DataFrame:
    cleaned = df.copy()
    inns = cleaned["ИНН"].dropna().astype(str).unique().tolist()
    cache: dict[str, object] = {}

    for inn in inns:
        try:
            cache[inn] = enricher.get_info(inn)
        except Exception:
            cache[inn] = None

    def clean_name(row: pd.Series) -> str:
        original = str(row.get("Юр_Лицо", "")).strip()
        info = cache.get(str(row["ИНН"]))
        if info and getattr(info, "name_full", ""):
            return str(info.name_full).strip()
        return "" if ARTIFACT_PATTERN.lower() in original.lower() else original

    def clean_role(row: pd.Series) -> str:
        explicit = str(row.get("Роль_в_холдинге", "")).strip()
        if explicit:
            return explicit
        info = cache.get(str(row["ИНН"]))
        if info and getattr(info, "role", ""):
            return str(info.role).strip()
        return _infer_role_from_comment(str(row.get("Комментарий", "")))

    cleaned["name_clean"] = cleaned.apply(clean_name, axis=1)
    cleaned["role_clean"] = cleaned.apply(clean_role, axis=1)
    cleaned["group_clean"] = cleaned["Группа_Компаний"].astype(str).str.strip()
    return cleaned.drop_duplicates(subset=["ИНН"], keep="last")


def save_to_companies(cleaned: pd.DataFrame) -> int:
    rows = []
    for _, row in cleaned.iterrows():
        name = str(row.get("name_clean", "")).strip()
        if not name:
            continue
        rows.append(
            {
                "inn": str(row["ИНН"]).strip(),
                "name": name,
                "group_companies": str(row.get("group_clean", "")).strip() or None,
                "role_in_holding": str(row.get("role_clean", "")).strip() or "Добыча",
            }
        )
    if not rows:
        return 0

    stmt = insert(Company).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Company.inn],
        set_={
            "name": stmt.excluded.name,
            "group_companies": stmt.excluded.group_companies,
            "role_in_holding": stmt.excluded.role_in_holding,
        },
    )
    with SessionLocal() as session:
        session.execute(stmt)
        session.commit()
    return len(rows)


def main() -> None:
    load_dotenv()
    source_csv = Path(os.getenv("COMPANY_SOURCE_CSV", str(DEFAULT_SOURCE_CSV)))
    if not source_csv.exists():
        raise FileNotFoundError(f"Исходный CSV не найден: {source_csv}")
    df = load_legacy_companies(source_csv)
    with DaDataEnricher() as enricher:
        cleaned = enrich_with_dadata(df, enricher)
    upserted = save_to_companies(cleaned)
    print(f"Записано/обновлено компаний в PostgreSQL: {upserted}")


if __name__ == "__main__":
    main()

