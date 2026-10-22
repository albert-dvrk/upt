"""Настройки координатора. Значения берутся из переменных окружения VF_*."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VF_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://videofetch:videofetch@localhost:5432/videofetch"
    db_pool_size: int = 20
    db_max_overflow: int = 30  # вместе с пулом не больше max_connections PostgreSQL (по умолчанию 100)
    db_pool_timeout: int = 20

    # блоки и аренда
    default_chunk_size: int = 1000
    lease_seconds: int = 60
    heartbeat_interval_seconds: int = 5
    heartbeat_timeout_seconds: int = 30
    reaper_interval_seconds: int = 5
    max_chunk_attempts: int = 5

    # Цель обхода: разрешены только перечисленные хосты (по умолчанию — свой mock-сервис).
    default_url_template: str = "http://mock:8080/{id}"
    allowed_target_hosts: str = "mock,localhost,127.0.0.1"

    # Аутентификация и сессии
    auth_enabled: bool = True
    secret_key: str | None = None  # ключ подписи сессий; если не задан — случайный на время работы процесса
    session_ttl_seconds: int = 8 * 3600
    cookie_secure: bool = False  # True при работе по HTTPS
    admin_username: str = "admin"
    admin_password: str | None = None  # создаёт первого администратора, если пользователей ещё нет
    login_max_failures: int = 5
    login_lock_seconds: int = 60
    agent_ca_file: str | None = None  # сертификат координатора, который заливается на серверы (HTTPS)

    # Развёртывание агентов по SSH
    agent_source_dir: str = "/srv/agent"
    agent_coordinator_url: str = "http://coordinator:8000"  # как агент видит координатор
    agent_remote_dir: str = "videofetch-agent"
    agent_python: str = "python3"
    ssh_key_dir: str = "/run/secrets"
    ssh_known_hosts: str = "/tmp/vf_known_hosts"
    ssh_connect_timeout: int = 10

    # Веб-терминал SSH (только администраторы)
    terminal_enabled: bool = True
    terminal_idle_seconds: int = 300  # неактивный сеанс закрывается
    terminal_max_seconds: int = 1800  # предельная длительность сеанса
    terminal_max_sessions: int = 5  # одновременных сеансов на процесс координатора
    terminal_max_commands_logged: int = 500  # записей о командах в аудите на сеанс

    @property
    def allowed_hosts(self) -> frozenset[str]:
        return frozenset(h.strip().lower() for h in self.allowed_target_hosts.split(",") if h.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
