"""Database session management.

Runs on SQLite locally and Postgres in CI/production. The engine options differ
between the two in ways that matter, so they're set explicitly rather than left
to defaults — see `build_engine`.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings
from .models import Base


def normalise_database_url(url: str) -> str:
    """Accept the URL forms hosted Postgres providers actually hand you.

    Neon, Heroku, Supabase and friends print `postgres://` or `postgresql://`,
    but SQLAlchemy 2.x needs an explicit driver and psycopg 3 registers as
    `postgresql+psycopg`. Rewriting here means DATABASE_URL can be pasted
    verbatim out of a provider dashboard, which is where it always comes from.
    """
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def build_engine(url: str) -> Engine:
    url = normalise_database_url(url)

    if url.startswith("sqlite"):
        # check_same_thread=False so the APScheduler worker thread can share
        # the engine with the request handlers.
        return create_engine(
            url, connect_args={"check_same_thread": False}, future=True
        )

    # Neon and similar free tiers scale the database to zero when idle and
    # recycle connections aggressively. pool_pre_ping discards dead connections
    # instead of raising on first use, which is the difference between a
    # scheduled run that works and one that fails every time after a quiet spell.
    #
    # prepare_threshold=None disables psycopg's automatic prepared statements.
    # Managed Postgres is usually reached through a PgBouncer-style pooler in
    # transaction mode (Neon's `-pooler` host, Supabase's `pooler.` host), where
    # a prepared statement created on one backend is missing on the next and
    # queries start failing with "prepared statement does not exist". Ingest
    # reruns the same lookup once per event, so it would cross psycopg's
    # 5-execution threshold within the first seconds of every poll. At this
    # query volume the statements save nothing worth that failure mode.
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=300,
        connect_args={"prepare_threshold": None},
        future=True,
    )


_settings = get_settings()
engine = build_engine(_settings.database_url)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db() -> None:
    Base.metadata.create_all(engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
