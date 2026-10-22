"""Настройки координатора. Значения берутся из переменных окружения VF_*."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VF_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://videofetch:videofetch@localhost:5432/videofetch"
    default_chunk_size: int = 1000
    lease_seconds: int = 60
    heartbeat_interval_seconds: int = 10


@lru_cache
def get_settings() -> Settings:
    return Settings()
