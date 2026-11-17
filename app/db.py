"""Подключение к БД и сессии.

Стек сознательно синхронный (SQLAlchemy 2.0 + psycopg3): учётная логика — это
транзакции и блокировки строк, их проще читать и отлаживать в синхронном виде.
FastAPI выполняет sync-эндпоинты в threadpool, так что параллельные запросы
обрабатываются нормально (это проверяется тестом test_concurrency.py).
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

settings = get_settings()

engine = create_engine(
    settings.database_url,
    echo=settings.sql_echo,
    pool_pre_ping=True,
    future=True,
    # Не ждём блокировку вечно: лучше внятная ошибка, чем зависший запрос.
    # statement_timeout страховает от «тяжёлых» запросов на прототипе.
    connect_args={"options": "-c lock_timeout=10s -c statement_timeout=60s"},
)

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def get_session() -> Iterator[Session]:
    """Одна сессия = одна транзакция на HTTP-запрос.

    Успех -> commit, любое исключение -> rollback. Частично применённых
    проводок в базе не остаётся никогда.
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
