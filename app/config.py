"""Настройки приложения. Всё через переменные окружения / .env."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+psycopg://checkstock:checkstock@localhost:15432/checkstock"
    test_database_url: str | None = None
    seed_on_start: bool = True
    sql_echo: bool = False

    @property
    def test_db_url(self) -> str:
        """URL тестовой базы: отдельная БД на том же сервере."""
        if self.test_database_url:
            return self.test_database_url
        base, _, _ = self.database_url.rpartition("/")
        return f"{base}/checkstock_test"


@lru_cache
def get_settings() -> Settings:
    return Settings()
