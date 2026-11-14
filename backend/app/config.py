"""Applikationskonfiguration via miljövariabler.

Inga hemligheter, VIN eller adresser är hårdkodade. Allt som är känsligt läses
från miljön eller lagras krypterat i databasen via webbgränssnittet.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_INSECURE_DEFAULT = "dev-only-insecure-secret-change-me"


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DRIVELOG_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    environment: str = Field(default="development", description="development | production | test")
    database_url: str = "sqlite:///./data/drivelog.sqlite3"
    redis_url: str = "redis://localhost:6379/0"

    # Publik bas-URL, används för OAuth-redirects och länkar i notiser.
    base_url: str = "http://localhost:3000"
    api_prefix: str = "/api/v1"

    secret_key: str = _INSECURE_DEFAULT
    # Kommaseparerade Fernet-nycklar; den första används för kryptering, övriga för rotation.
    encryption_keys: str = ""

    data_dir: Path = Path("./data")
    timezone: str = "Europe/Stockholm"

    session_ttl_hours: int = 24 * 14
    cookie_secure: bool | None = None  # None = härleds från environment
    cookie_name: str = "drivelog_session"

    max_upload_bytes: int = 10 * 1024 * 1024
    max_import_rows: int = 20000

    login_rate_limit_attempts: int = 8
    login_rate_limit_window_seconds: int = 300

    # Valfri bootstrap av första administratören (annars sker det i webbgränssnittet).
    first_admin_email: str | None = None
    first_admin_password: str | None = None

    cors_origins: str = ""
    log_level: str = "INFO"
    # Kör bakgrundsjobb synkront i processen (test/utveckling utan Redis).
    jobs_eager: bool = False

    @field_validator("database_url")
    @classmethod
    def _normalize_db_url(cls, v: str) -> str:
        if v.startswith("postgres://"):
            v = "postgresql://" + v[len("postgres://") :]
        if v.startswith("postgresql://"):
            v = "postgresql+psycopg://" + v[len("postgresql://") :]
        return v

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def use_secure_cookies(self) -> bool:
        if self.cookie_secure is not None:
            return self.cookie_secure
        return self.is_production

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "imports"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    def validate_for_production(self) -> list[str]:
        """Returnerar en lista med allvarliga konfigurationsfel i produktion."""
        problems: list[str] = []
        if self.is_production:
            if self.secret_key == _INSECURE_DEFAULT or len(self.secret_key) < 32:
                problems.append("DRIVELOG_SECRET_KEY måste sättas till ett slumpat värde på minst 32 tecken.")
            if not self.encryption_keys:
                problems.append("DRIVELOG_ENCRYPTION_KEYS måste innehålla minst en Fernet-nyckel.")
            if not self.base_url.startswith("https://"):
                problems.append("DRIVELOG_BASE_URL måste använda https i produktion.")
        return problems


@lru_cache
def get_config() -> AppConfig:
    return AppConfig()


def reset_config_cache() -> None:
    get_config.cache_clear()
