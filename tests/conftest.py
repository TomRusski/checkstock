"""Фикстуры тестов.

Тесты идут против НАСТОЯЩЕГО PostgreSQL (не SQLite): проверяются блокировки
строк, триггеры, партиальные уникальные индексы — то, на чём держится учёт.

Тестовая база отдельная (checkstock_test), создаётся автоматически, схема
накатывается теми же миграциями Alembic, что и в проде. Перед каждым тестом
база очищается и заполняется тестовыми данными из ТЗ.
"""

from __future__ import annotations

import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.config import get_settings

TEST_URL = get_settings().test_db_url
# Подменяем URL до импорта приложения: app.db создаёт engine на импорте.
os.environ["DATABASE_URL"] = TEST_URL
os.environ["SEED_ON_START"] = "false"
get_settings.cache_clear()


def _create_database_if_missing(url: str) -> None:
    parsed = make_url(url)
    admin_url = parsed.set(database="postgres")
    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT", future=True)
    with engine.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": parsed.database}
        ).scalar()
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{parsed.database}"'))
    engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def _database() -> str:
    _create_database_if_missing(TEST_URL)
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", TEST_URL)
    command.upgrade(config, "head")
    return TEST_URL


@pytest.fixture(scope="session")
def engine(_database: str):
    from app.db import engine as app_engine

    return app_engine


@pytest.fixture
def session(engine):
    """Сессия для проверок в тестах. Данные засеваются и коммитятся сразу."""
    from app.db import SessionLocal
    from app.seed import reset_demo_data

    with SessionLocal() as s:
        reset_demo_data(s)
        s.commit()
        yield s
        s.rollback()


@pytest.fixture
def client(_database: str, session):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def wh(session) -> dict[str, int]:
    """Имена складов -> id (Север, Юг из тестовых данных)."""
    from sqlalchemy import select

    from app.models import Warehouse

    return {w.name: w.id for w in session.scalars(select(Warehouse)).all()}
