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
    # Tom sträng = ingen Redis (enprocessläget): inga anslutningsförsök alls.
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
    # API-dokumentationen (/api/v1/docs och openapi.json) visas inte för omvärlden i produktion
    # om den inte uttryckligen slås på.
    public_docs: bool = False
    first_admin_email: str | None = None
    first_admin_password: str | None = None
    # Får första kontot skapas i webbgränssnittet (POST /auth/setup)? Home Assistant-tillägget
    # stänger av det (DRIVELOG_WEB_SETUP=false): där skapas kontot alltid ur inställningarna,
    # eftersom API:t nås av alla andra tillägg på Home Assistants interna nät medan det väntar.
    web_setup: bool = True

    cors_origins: str = ""
    log_level: str = "INFO"
    # Enprocessläget: bakgrundsjobb körs direkt i API-processen och schemat (tick, kalender,
    # nattligt underhåll) i en bakgrundstråd – ingen Redis, ingen Celery. Används av
    # Home Assistant-tillägget och scripts/dev.sh. I Compose-driften är det avstängt.
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
    def redis_enabled(self) -> bool:
        return bool(self.redis_url.strip())

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def sqlite_in_memory(self) -> bool:
        url = self.database_url
        return self.is_sqlite and (":memory:" in url or "mode=memory" in url or url.rstrip("/") in ("sqlite:", "sqlite+pysqlite:"))

    @property
    def single_process(self) -> bool:
        """Enprocessläget: jobb och schemaläggare i API-processen (inte i testsviten)."""
        return self.jobs_eager and self.environment != "test"

    def mode_description(self) -> str:
        """Beskrivning av driftläget för loggen vid start."""
        if self.single_process:
            db = "SQLite" if self.is_sqlite else self.database_url.split(":", 1)[0]
            return f"Enprocessläge: {db}, jobb och schemaläggare i API-processen"
        if self.environment == "test":
            return "Testläge: jobb körs direkt, ingen schemaläggare"
        return "Compose-läge: bakgrundsjobb och schema i Celery (Redis-kö)"

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
            if self.sqlite_in_memory:
                problems.append("DRIVELOG_DATABASE_URL får inte vara en minnesdatabas i produktion.")
            if not self.jobs_eager and not self.redis_enabled:
                problems.append(
                    "DRIVELOG_REDIS_URL saknas: Compose-läget kräver Redis. "
                    "Sätt DRIVELOG_JOBS_EAGER=true för enprocessläget."
                )
            if self.is_sqlite and not self.jobs_eager:
                problems.append(
                    "SQLite i produktion kräver enprocessläget (DRIVELOG_JOBS_EAGER=true); "
                    "Celery-processer kan inte dela en SQLite-fil säkert."
                )
            origins = [o.strip() for o in self.cors_origins.split(",") if o.strip()]
            if any(o == "*" or not o.lower().startswith("https://") for o in origins):
                # CORS med kakor: "*" speglar varje anropares ursprung och ger läsrätt till API:t.
                problems.append(
                    "DRIVELOG_CORS_ORIGINS får inte innehålla \"*\" eller adresser utan https i produktion."
                )
        return problems

    def production_warnings(self) -> list[str]:
        """Varningar i produktion som inte stoppar starten.

        Krypteringsnycklarna: en lista som bara är blanksteg eller kommatecken gör att hemligheterna
        krypteras med en nyckel härledd ur DRIVELOG_SECRET_KEY, och en nyckel i fel format ger fel
        först när en hemlighet ska läsas eller sparas. Starten stoppas inte – en installation som
        fungerar i dag ska starta likadant, och dess sparade hemligheter går bara att läsa med den
        nyckel som används nu – men det sägs tydligt i loggen.
        """
        warnings: list[str] = []
        if self.is_production and self.encryption_keys:
            from cryptography.fernet import Fernet

            keys = [k.strip() for k in self.encryption_keys.split(",") if k.strip()]
            if not keys:
                warnings.append(
                    "DRIVELOG_ENCRYPTION_KEYS innehåller ingen nyckel; hemligheterna krypteras med en nyckel "
                    "härledd ur DRIVELOG_SECRET_KEY. Byt inte nyckeln utan att först koppla från och "
                    "ansluta integrationerna igen."
                )
            for k in keys:
                try:
                    Fernet(k.encode())
                except (ValueError, TypeError):
                    warnings.append("DRIVELOG_ENCRYPTION_KEYS innehåller en nyckel som inte är en giltig Fernet-nyckel.")
                    break
        return warnings


@lru_cache
def get_config() -> AppConfig:
    return AppConfig()


def reset_config_cache() -> None:
    get_config.cache_clear()
