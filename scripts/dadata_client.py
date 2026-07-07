from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from dadata import Dadata


@dataclass
class DaDataCompanyInfo:
    inn: str
    name_full: str
    name_short: str
    director_name: str
    okved: str
    status: str
    capital: float | None
    role: str
    geo_lat: float | None
    geo_lon: float | None
    dadata_last_updated: date
    address_text: str = ""


class DaDataEnricher:
    """
    Enricher for DaData organization lookup by INN.

    Required env vars (if not passed explicitly):
    - DADATA_API_KEY
    - DADATA_SECRET_KEY
    """

    def __init__(self, api_key: str | None = None, secret_key: str | None = None) -> None:
        self.api_key = (api_key or os.getenv("DADATA_API_KEY", "")).strip()
        self.secret_key = (secret_key or os.getenv("DADATA_SECRET_KEY", "")).strip()
        if not self.api_key or not self.secret_key:
            raise ValueError("DaData API keys are required: DADATA_API_KEY and DADATA_SECRET_KEY")
        self._client = Dadata(self.api_key, self.secret_key)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "DaDataEnricher":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    @staticmethod
    def map_role_by_okved(okved: str) -> str:
        okved_norm = (okved or "").strip()
        if okved_norm.startswith("03.1"):
            return "Добыча"
        if okved_norm.startswith("10.2"):
            return "Береговой завод"
        if okved_norm.startswith("46.38"):
            return "Торговый дом"
        if okved_norm.startswith("64."):
            return "Финансовый хаб"
        return "Добыча"

    def get_info(self, inn: str) -> DaDataCompanyInfo | None:
        inn_clean = re.sub(r"\D", "", str(inn or ""))
        if len(inn_clean) not in (10, 12):
            return None

        result = self._client.find_by_id(name="party", query=inn_clean)
        if not result:
            return None

        row = result[0]
        data = row.get("data") or {}
        name_info = data.get("name") or {}
        management = data.get("management") or {}
        state = data.get("state") or {}
        opf = data.get("opf") or {}
        address_obj = data.get("address") or {}
        address_data = address_obj.get("data") or {}
        address_text = str(address_obj.get("unrestricted_value") or address_obj.get("value") or "").strip()

        okved = str(data.get("okved") or "").strip()
        capital_raw = data.get("capital")
        capital: float | None
        try:
            capital = float(capital_raw) if capital_raw is not None else None
        except (TypeError, ValueError):
            capital = None

        def _to_float(value: Any) -> float | None:
            try:
                return float(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        return DaDataCompanyInfo(
            inn=inn_clean,
            name_full=str(name_info.get("full_with_opf") or row.get("value") or "").strip(),
            name_short=str(name_info.get("short_with_opf") or "").strip(),
            director_name=str(management.get("name") or "").strip(),
            okved=okved,
            status=str(state.get("status") or "").strip(),
            capital=capital,
            role=self.map_role_by_okved(okved),
            geo_lat=_to_float(address_data.get("geo_lat")),
            geo_lon=_to_float(address_data.get("geo_lon")),
            dadata_last_updated=date.today(),
            address_text=address_text,
        )

