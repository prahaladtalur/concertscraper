from __future__ import annotations

import pytest

from app.db import build_engine, normalise_database_url


class TestNormaliseDatabaseUrl:
    @pytest.mark.parametrize(
        "given,expected",
        [
            # What Neon's dashboard actually prints.
            (
                "postgresql://user:pw@ep-cool-name.us-west-2.aws.neon.tech/neondb?sslmode=require",
                "postgresql+psycopg://user:pw@ep-cool-name.us-west-2.aws.neon.tech/neondb?sslmode=require",
            ),
            # Heroku-style legacy scheme.
            (
                "postgres://user:pw@host:5432/db",
                "postgresql+psycopg://user:pw@host:5432/db",
            ),
            # Already explicit — must not be double-prefixed.
            (
                "postgresql+psycopg://user:pw@host/db",
                "postgresql+psycopg://user:pw@host/db",
            ),
            # SQLite passes through untouched.
            ("sqlite:///./concertscraper.db", "sqlite:///./concertscraper.db"),
            ("sqlite://", "sqlite://"),
        ],
    )
    def test_rewrites_provider_urls_to_an_explicit_driver(self, given, expected):
        assert normalise_database_url(given) == expected

    def test_is_idempotent(self):
        once = normalise_database_url("postgres://u:p@h/db")
        assert normalise_database_url(once) == once


class TestBuildEngine:
    def test_sqlite_gets_cross_thread_access(self):
        engine = build_engine("sqlite://")
        # APScheduler's worker thread shares this engine with request handlers.
        assert engine.dialect.name == "sqlite"

    def test_postgres_url_builds_with_pre_ping(self):
        # No connection is opened until first use, so this is safe offline and
        # verifies the psycopg driver resolves and the URL parses.
        engine = build_engine(
            "postgresql://user:pw@ep-x.neon.tech/neondb?sslmode=require"
        )
        assert engine.dialect.name == "postgresql"
        assert engine.dialect.driver == "psycopg"
        # Dead pooled connections must be discarded, not raised on — Neon's free
        # tier scales to zero when idle.
        assert engine.pool._pre_ping is True


class TestBlankDatabaseUrl:
    """A defined-but-empty DATABASE_URL must not break the whole CLI.

    An unset GitHub secret interpolates to "", which previously reached
    create_engine at import time and turned every command — including the one
    meant to diagnose configuration — into a SQLAlchemy traceback.
    """

    def test_blank_falls_back_to_the_documented_default(self):
        from app.config import DEFAULT_DATABASE_URL, Settings

        assert Settings(database_url="").database_url == DEFAULT_DATABASE_URL
        assert Settings(database_url="   ").database_url == DEFAULT_DATABASE_URL

    def test_real_url_is_untouched(self):
        from app.config import Settings

        url = "postgresql://u:p@host/db"
        assert Settings(database_url=url).database_url == url

    def test_blank_url_still_builds_a_usable_engine(self):
        from app.config import Settings

        engine = build_engine(Settings(database_url="").database_url)
        assert engine.dialect.name == "sqlite"
