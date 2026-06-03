import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

import pandas as pd
from sqlalchemy import delete
from sqlalchemy.exc import SQLAlchemyError

from database.db_session import SessionLocal
from database.models import QuotaLimit

# Бассейны, полностью исключённые из поиска и аналитики
EXCLUDED_BASINS = [
    "Волжско-Каспийский рыбохозяйственный бассейн",
    "Западный",
]


def is_basin_excluded(basin: str) -> bool:
    """Проверяет, входит ли бассейн в список исключённых."""
    b = (basin or "").strip()
    if not b:
        return False
    bl = b.lower()
    if bl == "западный" or bl.startswith("западный "):
        return True
    if "волжск" in bl and "каспий" in bl:
        return True
    return False


@dataclass
class QuotaRecord:
    group: str
    legal_name: str
    inn: str
    year: int
    basin: str
    species: str
    quota_type: str
    share_pct: float
    volume_tons: float
    change_reason: str = ""
    contract_start: str = ""
    contract_end: str = ""


def load_company_groups(mapping_path: Path) -> pd.DataFrame:
    """
    Загружаем справочник соответствия юрлица/ИНН -> группа компаний.
    Ожидаемые колонки: Группа_Компаний,Юр_Лицо,ИНН
    """
    df = pd.read_csv(mapping_path, dtype=str)
    for col in ["Группа_Компаний", "Юр_Лицо", "ИНН"]:
        if col not in df.columns:
            raise ValueError(f"В файле {mapping_path} отсутствует обязательная колонка '{col}'")
    df["ИНН"] = df["ИНН"].fillna("").astype(str).str.strip()
    return df


def normalize_basin(raw_basin: str) -> str:
    val = (raw_basin or "").strip().lower()
    if "север" in val:
        return "Северный"
    if "дальневост" in val:
        return "Дальневосточный"
    if "запад" in val:
        return "Западный"
    if "международ" in val or "африк" in val or "фарер" in val or "неафк" in val:
        return "Международные воды"
    return raw_basin


def normalize_quota_type(raw_type: str) -> str:
    val = (raw_type or "").strip().lower()
    if "прибреж" in val:
        return "Прибрежная"
    if "инвест" in val or "ц-1" in val or "ц-2" in val:
        return "Инвестиционная"
    if "международ" in val or "африк" in val or "фарер" in val or "неафк" in val:
        return "Международная"
    # по умолчанию считаем исторической промышленной квотой
    return "Промышленная"


def normalize_species(raw_species: str) -> str:
    if not raw_species:
        return raw_species
    val = raw_species.strip().lower()
    mapping = {
        "треск": "Треска",
        "пикш": "Пикша",
        "минта": "Минтай",
        "сельд": "Сельдь",
        "путас": "Путассу",
        "краб": "Краб",
        "кальмар": "Кальмар",
        "кревет": "Креветка",
    }
    for key, std in mapping.items():
        if key in val:
            return std
    return raw_species


def compute_volume_from_share(odu_tons: float, share_pct: float) -> float:
    if odu_tons is None or pd.isna(odu_tons):
        return float("nan")
    return odu_tons * share_pct / 100.0


def build_odu_index(odu_df: pd.DataFrame) -> Dict[Tuple[int, str, str], float]:
    """
    Индекс ОДУ по (год, бассейн, объект_лова) -> ОДУ_тонн.
    Ожидаемые колонки: Год,Бассейн,Объект_Лова,ОДУ_Тонн
    (эту таблицу можно собрать из приказов ОДУ отдельно и скормить сюда).
    """
    required = ["Год", "Бассейн", "Объект_Лова", "ОДУ_Тонн"]
    for col in required:
        if col not in odu_df.columns:
            raise ValueError(f"В таблице ОДУ отсутствует обязательная колонка '{col}'")
    odu_index: Dict[Tuple[int, str, str], float] = {}
    for _, row in odu_df.iterrows():
        year = int(row["Год"])
        basin = normalize_basin(str(row["Бассейн"]))
        species = normalize_species(str(row["Объект_Лова"]))
        odu_index[(year, basin, species)] = float(row["ОДУ_Тонн"])
    return odu_index


def attach_group(df: pd.DataFrame, group_df: pd.DataFrame) -> pd.DataFrame:
    """
    Мерджим по ИНН, при отсутствии ИНН — по названию юрлица (более грубое сопоставление).
    Ожидается, что во входном df есть колонки: Юр_Лицо,ИНН.
    """
    df["ИНН"] = df["ИНН"].fillna("").astype(str).str.strip()
    group_df = group_df.copy()
    group_df["ИНН"] = group_df["ИНН"].fillna("").astype(str).str.strip()

    # сначала точный мердж по ИНН (из group_df приходит колонка с суффиксом _grp)
    merged = df.merge(
        group_df[["Группа_Компаний", "ИНН"]],
        on="ИНН",
        how="left",
        suffixes=("", "_grp"),
    )
    # подставляем группу из справочника (колонка _grp)
    if "Группа_Компаний_grp" in merged.columns:
        merged["Группа_Компаний"] = merged["Группа_Компаний_grp"].fillna(merged["Группа_Компаний"])
        merged = merged.drop(columns=["Группа_Компаний_grp"])

    # где не нашлось по ИНН, пробуем по названию
    mask_no_group = merged["Группа_Компаний"].isna() | (merged["Группа_Компаний"].astype(str).str.strip() == "")
    if mask_no_group.any():
        tmp = merged[mask_no_group].merge(
            group_df[["Группа_Компаний", "Юр_Лицо"]],
            on="Юр_Лицо",
            how="left",
            suffixes=("", "_by_name"),
        )
        # после merge по Юр_Лицо колонка с группы справочника может быть Группа_Компаний_by_name
        group_col = "Группа_Компаний_by_name" if "Группа_Компаний_by_name" in tmp.columns else "Группа_Компаний"
        merged.loc[mask_no_group, "Группа_Компаний"] = tmp[group_col].values
    return merged


def calculate_variance_and_reason(df: pd.DataFrame) -> pd.DataFrame:
    """
    На входе — агрегированный датафрейм с колонками:
    Группа_Компаний,Юр_Лицо,ИНН,Год,Бассейн,Объект_Лова,Тип_Квоты,Доля_%,Объем_Тонн
    На выходе — добавляем колонку Причина_Изменения.
    """
    df = df.sort_values(["ИНН", "Бассейн", "Объект_Лова", "Тип_Квоты", "Год"]).reset_index(drop=True)

    reasons: List[str] = []
    # Для определения «Биология/ОДУ» нужно смотреть, падает ли объём по виду суммарно по всем компаниям
    total_by_year_species = (
        df.groupby(["Год", "Бассейн", "Объект_Лова"])["Объем_Тонн"]
        .sum()
        .reset_index()
        .rename(columns={"Объем_Тонн": "Total_Объем_Тонн"})
    )
    df = df.merge(total_by_year_species, on=["Год", "Бассейн", "Объект_Лова"], how="left")

    # словарь для быстрых сравнений тоталов по годам
    totals = {}
    for _, row in total_by_year_species.iterrows():
        totals[(row["Год"], row["Бассейн"], row["Объект_Лова"])] = row["Total_Объем_Тонн"]

    for idx, row in df.iterrows():
        year = row["Год"]
        inn = row["ИНН"]
        basin = row["Бассейн"]
        species = row["Объект_Лова"]
        quota_type = row["Тип_Квоты"]
        volume = row["Объем_Тонн"]

        # по умолчанию — пусто
        reason = ""

        prev_year = year - 1
        prev_mask = (
            (df["ИНН"] == inn)
            & (df["Бассейн"] == basin)
            & (df["Объект_Лова"] == species)
            & (df["Тип_Квоты"] == quota_type)
            & (df["Год"] == prev_year)
        )
        if not prev_mask.any():
            # не было в прошлом году — либо рост, либо новая квота; причину не ставим
            reasons.append(reason)
            continue

        prev_volume = df.loc[prev_mask, "Объем_Тонн"].sum()
        if prev_volume <= 0 or pd.isna(prev_volume) or pd.isna(volume):
            reasons.append(reason)
            continue

        delta_pct = (volume - prev_volume) / prev_volume * 100.0

        if delta_pct < -5.0:
            # проверяем, упал ли общий объем по виду/бассейну
            total_curr = totals.get((year, basin, species))
            total_prev = totals.get((prev_year, basin, species))
            if total_curr is not None and total_prev is not None and total_prev > 0:
                total_delta_pct = (total_curr - total_prev) / total_prev * 100.0
                if total_delta_pct < -5.0:
                    reason = "Биология/ОДУ"
                else:
                    reason = "Регулятор/Инвестквоты"
            else:
                reason = "Регулятор/Инвестквоты"
        # если доля/объем исчезли полностью в текущем году —
        # это будет видно на записях следующего года, здесь достаточно обработки выше.

        reasons.append(reason)

    df["Причина_Изменения"] = reasons
    return df


def example_transform(  # заглушка: сюда вы будете подставлять реальные датафреймы, распарсенные из приказов
    quota_tables: List[pd.DataFrame],
    odu_df: pd.DataFrame,
    company_groups_path: Path,
) -> pd.DataFrame:
    """
    quota_tables — список датафреймов, полученных из различных приказов
    (распределение долей / инвестквот / международных квот).

    Ожидаемые колонки в сырых таблицах (можно адаптировать под фактический формат):
    - Год
    - Бассейн
    - Объект_Лова
    - Тип_Квоты
    - Юр_Лицо
    - ИНН
    - Доля_%
    - Объем_Тонн (может быть пустым, тогда считаем по ОДУ)
    """
    group_df = load_company_groups(company_groups_path)
    odu_index = build_odu_index(odu_df)

    all_rows: List[QuotaRecord] = []

    for tbl in quota_tables:
        for _, r in tbl.iterrows():
            year = int(r["Год"])
            basin = normalize_basin(str(r["Бассейн"]))
            species = normalize_species(str(r["Объект_Лова"]))
            quota_type = normalize_quota_type(str(r["Тип_Квоты"]))
            legal_name = str(r["Юр_Лицо"])
            inn = str(r.get("ИНН", "") or "").strip()
            share_pct = float(r.get("Доля_%", 0) or 0)

            contract_start = str(r.get("Дата_Начала_Договора", "") or "")
            contract_end = str(r.get("Дата_Окончания_Договора", "") or "")

            if pd.notna(r.get("Объем_Тонн", None)):
                volume_tons = float(r["Объем_Тонн"])
            else:
                odu_tons = odu_index.get((year, basin, species))
                volume_tons = compute_volume_from_share(odu_tons, share_pct)

            all_rows.append(
                QuotaRecord(
                    group="",  # заполним после маппинга
                    legal_name=legal_name,
                    inn=inn,
                    year=year,
                    basin=basin,
                    species=species,
                    quota_type=quota_type,
                    share_pct=share_pct,
                    volume_tons=volume_tons,
                    contract_start=contract_start,
                    contract_end=contract_end,
                )
            )

    df = pd.DataFrame([dataclasses.asdict(r) for r in all_rows])

    # Переименуем в целевые русские колонки
    df = df.rename(
        columns={
            "group": "Группа_Компаний",
            "legal_name": "Юр_Лицо",
            "inn": "ИНН",
            "year": "Год",
            "basin": "Бассейн",
            "species": "Объект_Лова",
            "quota_type": "Тип_Квоты",
            "share_pct": "Доля_%",
            "volume_tons": "Объем_Тонн",
            "contract_start": "Дата_Начала_Договора",
            "contract_end": "Дата_Окончания_Договора",
        }
    )

    # Подтягиваем группы компаний по ИНН/названию
    df = attach_group(df, group_df)

    # Считаем отклонения и причины
    df = calculate_variance_and_reason(df)

    # Оставляем только нужные колонки в правильном порядке
    df = df[
        [
            "Группа_Компаний",
            "Юр_Лицо",
            "ИНН",
            "Год",
            "Бассейн",
            "Объект_Лова",
            "Тип_Квоты",
            "Доля_%",
            "Объем_Тонн",
            "Дата_Начала_Договора",
            "Дата_Окончания_Договора",
            "Причина_Изменения",
        ]
    ]

    return df


def main():
    """
    Пример точки входа.
    Здесь вы должны:
    - скачать/прочитать локально таблицы из приказов ОДУ и приказов о распределении квот;
    - привести их к ожидаемым колонкам;
    - передать в example_transform.

    В данном прототипе мы просто читаем пустые/демонстрационные таблицы,
    чтобы показать структуру.
    """
    base_dir = Path(__file__).resolve().parents[1]
    company_groups_path = base_dir / "data" / "company_groups.csv"

    # Базовый сценарий: используем уже распарсенный CSV из scripts/parse_order_texts_to_quota_csv.py
    parsed_quota_path = base_dir / "data" / "parsed_quota_rows.csv"
    if not parsed_quota_path.exists():
        print(
            "Файл data/parsed_quota_rows.csv не найден.\n"
            "Сначала выполните:\n"
            "  python scripts/fetch_order_texts.py\n"
            "  python scripts/parse_order_texts_to_quota_csv.py"
        )
        return

    raw_df = pd.read_csv(parsed_quota_path, dtype={"inn": str, "nd": str})

    # Обогащение квотами 2026 с calculations.fish.gov.ru (если скрипт уже отработал)
    calculations_2026_path = base_dir / "data" / "calculations_2026_quota_rows.csv"
    if calculations_2026_path.exists():
        calc_df = pd.read_csv(calculations_2026_path, dtype={"inn": str})
        calc_df = calc_df[~calc_df["basin"].fillna("").apply(is_basin_excluded)]
        if len(calc_df) > 0:
            raw_df = pd.concat([raw_df, calc_df], ignore_index=True)
            # дедупликация: при совпадении (inn, year, basin, species) оставляем первую запись (из приказов)
            key_cols = ["inn", "year", "basin", "species"]
            raw_df = raw_df.drop_duplicates(subset=key_cols, keep="first")
            print(f"Добавлены квоты 2026 с calculations.fish.gov.ru, всего строк после объединения: {len(raw_df)}")

    # Исключаем бассейны из EXCLUDED_BASINS (Волжско-Каспийский, Западный)
    n_before = len(raw_df)
    raw_df = raw_df[~raw_df["basin"].fillna("").apply(is_basin_excluded)]
    n_after = len(raw_df)
    if n_before > n_after:
        print(f"Исключено записей по бассейнам {EXCLUDED_BASINS}: {n_before - n_after}")

    # Приводим к ожидаемым колонкам для example_transform
    quota_df = raw_df.rename(
        columns={
            "year": "Год",
            "basin": "Бассейн",
            "species": "Объект_Лова",
            "quota_type": "Тип_Квоты",
            "legal_name": "Юр_Лицо",
            "inn": "ИНН",
            "share_pct": "Доля_%",
            "volume_tons": "Объем_Тонн",
            "contract_date_start": "Дата_Начала_Договора",
            "contract_date_end": "Дата_Окончания_Договора",
        }
    )

    # В этом сценарии мы используем уже готовые объёмы из приказов, поэтому ОДУ не нужен
    odu_df = pd.DataFrame(columns=["Год", "Бассейн", "Объект_Лова", "ОДУ_Тонн"])

    result_df = example_transform([quota_df], odu_df, company_groups_path)

    # Запись распарсенных лимитов квот напрямую в DWH (таблица quotas_limits).
    quota_limits_df = result_df.rename(
        columns={
            "Год": "year",
            "ИНН": "inn_owner_inn",
            "Бассейн": "basin",
            "Объект_Лова": "object_lova",
            "Объем_Тонн": "volume_tons",
        }
    )[["year", "inn_owner_inn", "basin", "object_lova", "volume_tons"]].copy()

    quota_limits_df["inn_owner_inn"] = quota_limits_df["inn_owner_inn"].fillna("").astype(str).str.strip()
    quota_limits_df["basin"] = quota_limits_df["basin"].fillna("").astype(str).str.strip()
    quota_limits_df["object_lova"] = quota_limits_df["object_lova"].fillna("").astype(str).str.strip()
    quota_limits_df["year"] = pd.to_numeric(quota_limits_df["year"], errors="coerce").astype("Int64")
    quota_limits_df["volume_tons"] = pd.to_numeric(quota_limits_df["volume_tons"], errors="coerce")

    # Оставляем только валидные строки и дедуплицируем по PK quotas_limits.
    quota_limits_df = quota_limits_df.dropna(subset=["year", "volume_tons"])
    quota_limits_df = quota_limits_df[
        (quota_limits_df["inn_owner_inn"] != "")
        & (quota_limits_df["basin"] != "")
        & (quota_limits_df["object_lova"] != "")
    ]
    quota_limits_df = quota_limits_df.drop_duplicates(
        subset=["year", "inn_owner_inn", "basin", "object_lova"],
        keep="last",
    )

    records = quota_limits_df.to_dict(orient="records")
    if not records:
        print("Нет валидных записей для загрузки в quotas_limits.")
        return

    years_to_replace = sorted({int(r["year"]) for r in records})

    try:
        with SessionLocal() as session:
            # Чтобы загрузка была идемпотентной, пересобираем лимиты только для затронутых лет.
            session.execute(delete(QuotaLimit).where(QuotaLimit.year.in_(years_to_replace)))
            session.bulk_insert_mappings(QuotaLimit, records)
            session.commit()
        print(f"Готово. Загружено {len(records)} записей в quotas_limits (годы: {years_to_replace}).")
    except SQLAlchemyError as exc:
        raise RuntimeError("Не удалось записать лимиты квот в таблицу quotas_limits.") from exc


if __name__ == "__main__":
    main()

