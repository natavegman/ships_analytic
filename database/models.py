from __future__ import annotations

import enum
from datetime import date as dt_date

from sqlalchemy import Date, Enum as SAEnum, Float, ForeignKey, Index, Integer, MetaData, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy ORM models."""

    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_name)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class CatchSource(str, enum.Enum):
    AMP = "AMP"
    GFW_CALC = "GFW_CALC"
    MANUAL = "MANUAL"


class Company(Base):
    __tablename__ = "companies"

    # Natural key from the domain: INN can have leading zeros, so we store it as string.
    inn: Mapped[str] = mapped_column(String(12), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    group_companies: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role_in_holding: Mapped[str | None] = mapped_column(String(255), nullable=True)

    vessels: Mapped[list["Vessel"]] = relationship(back_populates="base_owner", cascade="all, delete-orphan")
    quota_transfers_received: Mapped[list["QuotaTransfer"]] = relationship(
        back_populates="actual_quota_owner",
        cascade="all, delete-orphan",
        foreign_keys="QuotaTransfer.actual_quota_owner_inn",
    )
    quota_limits: Mapped[list["QuotaLimit"]] = relationship(
        back_populates="inn_owner",
        cascade="all, delete-orphan",
        foreign_keys="QuotaLimit.inn_owner_inn",
    )


class Vessel(Base):
    __tablename__ = "vessels"

    # Natural key from the domain: IMO is the stable identifier.
    imo: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    project: Mapped[str | None] = mapped_column(String(255), nullable=True)

    base_owner_inn: Mapped[str | None] = mapped_column(
        String(12),
        ForeignKey("companies.inn", ondelete="SET NULL"),
        nullable=True,
    )
    gfw_id: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)

    base_owner: Mapped["Company | None"] = relationship(back_populates="vessels", foreign_keys=[base_owner_inn])

    catches: Mapped[list["DailyCatch"]] = relationship(back_populates="vessel", cascade="all, delete-orphan")
    quota_transfers: Mapped[list["QuotaTransfer"]] = relationship(back_populates="vessel", cascade="all, delete-orphan")


class QuotaLimit(Base):
    """
    Quotas limits per company (INN) / year / basin / species (object_lova).
    """

    __tablename__ = "quotas_limits"

    year: Mapped[int] = mapped_column(Integer, primary_key=True)
    inn_owner_inn: Mapped[str] = mapped_column(
        String(12),
        ForeignKey("companies.inn", ondelete="CASCADE"),
        primary_key=True,
    )
    basin: Mapped[str] = mapped_column(String(255), primary_key=True)
    object_lova: Mapped[str] = mapped_column(String(255), primary_key=True)
    volume_tons: Mapped[float] = mapped_column(Float, nullable=False)

    inn_owner: Mapped["Company"] = relationship(back_populates="quota_limits", foreign_keys=[inn_owner_inn])


class QuotaTransfer(Base):
    """
    Rental/transfer intervals: for a given vessel, catch ownership changes in time.
    """

    __tablename__ = "quota_transfers"

    __table_args__ = (
        # Helps the future "who owned catch on date X" query.
        Index("ix_quota_transfers_vessel_owner_dates", "vessel_id", "actual_quota_owner_inn", "start_date", "end_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vessel_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("vessels.imo", ondelete="CASCADE"),
        nullable=False,
    )
    actual_quota_owner_inn: Mapped[str] = mapped_column(
        String(12),
        ForeignKey("companies.inn", ondelete="CASCADE"),
        nullable=False,
    )
    start_date: Mapped[dt_date] = mapped_column(Date, nullable=False)
    end_date: Mapped[dt_date] = mapped_column(Date, nullable=False)

    vessel: Mapped["Vessel"] = relationship(back_populates="quota_transfers", foreign_keys=[vessel_id])
    actual_quota_owner: Mapped["Company"] = relationship(
        back_populates="quota_transfers_received",
        foreign_keys=[actual_quota_owner_inn],
    )


class DailyCatch(Base):
    """
    Actual daily catches.

    Note: `source` is an enum with allowed values from ARCHITECTURE_V2.md.
    """

    __tablename__ = "daily_catches"

    __table_args__ = (
        Index("ix_daily_catches_vessel_date", "vessel_id", "date"),
    )

    vessel_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("vessels.imo", ondelete="CASCADE"),
        primary_key=True,
    )
    date: Mapped[dt_date] = mapped_column(Date, primary_key=True)
    volume_tons: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[CatchSource] = mapped_column(
        SAEnum(CatchSource, name="catch_source", native_enum=True),
        primary_key=True,
    )

    vessel: Mapped["Vessel"] = relationship(back_populates="catches", foreign_keys=[vessel_id])


class MarketPrice(Base):
    __tablename__ = "market_prices"

    __table_args__ = (
        Index("ix_market_prices_year_month", "year_month"),
    )

    year_month: Mapped[str] = mapped_column(String(16), primary_key=True)
    species: Mapped[str] = mapped_column(String(255), primary_key=True)
    price_usd_kg: Mapped[float] = mapped_column(Float, nullable=False)

