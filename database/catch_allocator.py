from __future__ import annotations

from datetime import date as dt_date
from datetime import datetime
from typing import Any, Callable

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from .db_session import SessionLocal
from .models import QuotaTransfer, Vessel


class CatchAllocator:
    """
    Resolves legal catch owner INN for a vessel and date.

    Input payload format:
        {"vessel_id": 10, "date": "2025-05-10", "volume": 150}
    """

    def __init__(self, session_factory: Callable[[], Session] | None = None) -> None:
        self._session_factory = session_factory or SessionLocal

    @staticmethod
    def _parse_payload(payload: dict[str, Any]) -> tuple[int, dt_date, float]:
        if not isinstance(payload, dict):
            raise ValueError("payload must be a dict")

        if "vessel_id" not in payload:
            raise ValueError("payload must include 'vessel_id'")
        if "date" not in payload:
            raise ValueError("payload must include 'date'")
        if "volume" not in payload:
            raise ValueError("payload must include 'volume'")

        vessel_id = int(payload["vessel_id"])

        raw_date = payload["date"]
        if isinstance(raw_date, dt_date):
            catch_date = raw_date
        elif isinstance(raw_date, str):
            try:
                catch_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
            except ValueError as exc:
                raise ValueError("payload['date'] must be in YYYY-MM-DD format") from exc
        else:
            raise ValueError("payload['date'] must be a string in YYYY-MM-DD format")

        volume = float(payload["volume"])
        if volume <= 0:
            raise ValueError("payload['volume'] must be greater than 0")

        return vessel_id, catch_date, volume

    def resolve_owner_inn(self, payload: dict[str, Any]) -> str:
        """
        Return company INN that legally owns catch on a given date.

        Resolution logic:
        1) If there is an active quota transfer for vessel/date -> transfer owner INN.
        2) Otherwise -> vessel.base_owner_inn.
        """

        vessel_id, catch_date, _ = self._parse_payload(payload)

        with self._session_factory() as session:
            transfer_stmt = (
                select(QuotaTransfer.actual_quota_owner_inn)
                .where(
                    and_(
                        QuotaTransfer.vessel_id == vessel_id,
                        QuotaTransfer.start_date <= catch_date,
                        QuotaTransfer.end_date >= catch_date,
                    )
                )
                .order_by(QuotaTransfer.start_date.desc(), QuotaTransfer.id.desc())
                .limit(1)
            )
            transfer_owner_inn = session.execute(transfer_stmt).scalar_one_or_none()
            if transfer_owner_inn:
                return transfer_owner_inn

            vessel_stmt = select(Vessel.base_owner_inn).where(Vessel.imo == vessel_id).limit(1)
            base_owner_inn = session.execute(vessel_stmt).scalar_one_or_none()
            if base_owner_inn:
                return base_owner_inn

        raise LookupError(f"Cannot resolve owner INN for vessel_id={vessel_id} on date={catch_date.isoformat()}")

