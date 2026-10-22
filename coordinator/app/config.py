"""Настройки координатора. Значения берутся из переменных окружения VF_*."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VF_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://videofetch:videofetch@localhost:5432/videofetch"

    # блоки и аренда
    default_chunk_size: int = 1000
    lease_seconds: int = 60
    heartbeat_interval_seconds: int = 10
    heartbeat_timeout_seconds: int = 30
    reaper_interval_seconds: int = 5
    max_chunk_attempts: int = 5

    # Цель обхода: разрешены только перечисленные хосты (по умолчанию — свой mock-сервис).
    default_url_template: str = "http://mock:8080/{id}"
    allowed_target_hosts: str = "mock,localhost,127.0.0.1"

    # Развёртывание агентов по SSH
    agent_source_dir: str = "/srv/agent"
    agent_coordinator_url: str = "http://coordinator:8000"  # как агент видит координатор
    agent_remote_dir: str = "videofetch-agent"
    agent_python: str = "python3"
    ssh_key_dir: str = "/run/secrets"
    ssh_known_hosts: str = "/tmp/vf_known_hosts"
    ssh_connect_timeout: int = 10

    @property
    def allowed_hosts(self) -> frozenset[str]:
        return frozenset(h.strip().lower() for h in self.allowed_target_hosts.split(",") if h.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
