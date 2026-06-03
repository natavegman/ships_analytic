from __future__ import annotations

import os
from contextlib import contextmanager
from functools import lru_cache
from typing import Generator

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


load_dotenv()


def _get_database_url() -> str:
    """
    Resolve PostgreSQL connection string.

    Preferred: DATABASE_URL, e.g.:
      postgresql+psycopg2://user:pass@host:5432/dbname

    Fallback (optional) env vars:
      POSTGRES_HOST / POSTGRES_PORT / POSTGRES_DB / POSTGRES_USER / POSTGRES_PASSWORD
    """

    url = os.getenv("DATABASE_URL")
    if url:
        return url

    host = os.getenv("POSTGRES_HOST")
    port = os.getenv("POSTGRES_PORT", "5432")
    db = os.getenv("POSTGRES_DB")
    user = os.getenv("POSTGRES_USER")
    password = os.getenv("POSTGRES_PASSWORD")

    if host and db and user and password:
        return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{db}"

    raise RuntimeError(
        "Database connection is not configured. Set DATABASE_URL or POSTGRES_HOST/POSTGRES_DB/"
        "POSTGRES_USER/POSTGRES_PASSWORD."
    )


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """
    Create SQLAlchemy engine once per process.

    Notes for scalability:
    - pool_pre_ping keeps connections healthy for long-running workers.
    - pool_size/max_overflow can be tuned later based on load.
    """

    url = _get_database_url()
    echo = os.getenv("SQLALCHEMY_ECHO", "0").lower() in {"1", "true", "yes"}

    return create_engine(
        url,
        echo=echo,
        pool_pre_ping=True,
        pool_size=int(os.getenv("SQLALCHEMY_POOL_SIZE", "5")),
        max_overflow=int(os.getenv("SQLALCHEMY_MAX_OVERFLOW", "10")),
    )


SessionLocal = sessionmaker(
    bind=get_engine(),
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
    class_=Session,
)


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """
    Provide a transactional scope around a series of operations.

    Usage:
        with session_scope() as session:
            session.add(...)
    """

    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

