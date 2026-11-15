"""SMGD DriveLog – FastAPI-applikationen."""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select, text

from app import __version__
from app.api import errors
from app.api.router import api_router
from app.config import get_config
from app.db import Base, get_engine, session_scope
from app.logging_setup import configure_logging

logger = logging.getLogger("drivelog")


def init_schema() -> None:
    """Skapar tabeller i utveckling/test. I produktion används Alembic-migrationer."""
    import app.models  # noqa: F401

    Base.metadata.create_all(get_engine())


class BootstrapError(RuntimeError):
    """DRIVELOG_FIRST_ADMIN_* är ogiltiga; API:t startar inte (installationen lämnas inte öppen)."""


def bootstrap_admin() -> None:
    """Skapar första administratören från miljövariabler, om angivet och inga konton finns.

    Körs vid uppstart innan API:t tar emot anrop. Finns redan ett konto görs ingenting.
    Är uppgifterna ogiltiga (t.ex. för svagt lösenord) avbryts uppstarten med ett tydligt
    fel i loggen – annars skulle installationen stå öppen för vem som helst att ta över
    via förstagångsinställningen. Lösenordet skrivs aldrig ut.
    """
    cfg = get_config()
    email_raw = (cfg.first_admin_email or "").strip()
    password = cfg.first_admin_password or ""
    if not email_raw and not password:
        return
    from app.models import User
    from app.security import passwords
    from app.services import vehicles

    with session_scope() as db:
        if (db.scalar(select(func.count()).select_from(User)) or 0) > 0:
            email = email_raw.lower()
            if email and db.scalar(select(func.count()).select_from(User).where(func.lower(User.email) == email)) == 0:
                # Kontot ägaren angav skapades aldrig: någon annan hann skapa det första kontot
                # (eller adressen har bytts). Säg det tydligt i stället för att tiga.
                logger.warning(
                    "DRIVELOG_FIRST_ADMIN_EMAIL är satt till %s, men det finns redan konton och inget av dem "
                    "har den adressen – inget konto skapades. Har du inte själv skapat de befintliga "
                    "kontona: stäng av tjänsten och kontrollera vem som har gjort det.",
                    email,
                )
            return
        if not email_raw or not password:
            raise BootstrapError(
                "Både DRIVELOG_FIRST_ADMIN_EMAIL och DRIVELOG_FIRST_ADMIN_PASSWORD måste anges för att skapa "
                "första kontot."
            )
        email = email_raw.lower()
        if "@" not in email or email.startswith("@") or email.endswith("@") or " " in email:
            raise BootstrapError("DRIVELOG_FIRST_ADMIN_EMAIL är inte en giltig e-postadress.")
        problem = passwords.validate_password_strength(password)
        if problem:
            raise BootstrapError(f"DRIVELOG_FIRST_ADMIN_PASSWORD duger inte: {problem}")
        user = User(
            email=email,
            display_name=email.split("@")[0],
            password_hash=passwords.hash_password(password),
            is_admin=True,
        )
        db.add(user)
        db.flush()
        vehicles.default_vehicle(db, user.id)
        vehicles.default_driver(db, user.id, user.display_name)
        logger.info("Första administratörskontot skapades från miljövariabler.")


def start_scheduler():
    """Enprocessläget: startar schemaläggaren i API-processen (se app.scheduler).

    I Compose-driften sköter Celery beat schemat och i testsviten styrs jobben av testerna –
    där startas ingen tråd.
    """
    if not get_config().single_process:
        return None
    from app import scheduler

    sched = scheduler.from_config()
    sched.start()
    return sched


_scheduler = None


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    global _scheduler
    cfg = get_config()
    configure_logging()
    problems = cfg.validate_for_production()
    if problems:
        for p in problems:
            logger.error("Konfigurationsfel: %s", p)
        raise RuntimeError("Osäker produktionskonfiguration: " + " ".join(problems))
    logger.info("%s.", cfg.mode_description())
    for d in (cfg.data_dir, cfg.uploads_dir, cfg.exports_dir, cfg.backups_dir):
        d.mkdir(parents=True, exist_ok=True)
    if not cfg.is_production:
        init_schema()
    try:
        bootstrap_admin()
    except BootstrapError as exc:
        logger.error("Första kontot kunde inte skapas: %s API:t startas inte.", exc)
        raise
    _scheduler = start_scheduler()
    try:
        yield
    finally:
        if _scheduler is not None:
            _scheduler.stop()
            _scheduler = None


def create_app() -> FastAPI:
    cfg = get_config()
    # I produktion ligger API:t öppet mot internet: beskrivningen av alla slutpunkter visas
    # då bara om ägaren har valt det (DRIVELOG_PUBLIC_DOCS=true).
    docs_enabled = cfg.public_docs or not cfg.is_production
    app = FastAPI(
        title="SMGD DriveLog API",
        version=__version__,
        description=(
            "REST-API för SMGD DriveLog – intelligent körjournal. Webbappen autentiserar med "
            "sessionskaka + X-CSRF-Token; Home Assistant-integrationen med Bearer-token."
        ),
        openapi_url=f"{cfg.api_prefix}/openapi.json" if docs_enabled else None,
        docs_url=f"{cfg.api_prefix}/docs" if docs_enabled else None,
        redoc_url=None,
        lifespan=lifespan,
    )
    origins = [o.strip() for o in cfg.cors_origins.split(",") if o.strip()]
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Content-Type", "X-CSRF-Token", "Authorization"],
        )

    @app.middleware("http")
    async def security_headers(request: Request, call_next) -> Response:  # type: ignore[no-untyped-def]
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        response: Response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if request.url.path.startswith(cfg.api_prefix) and "Cache-Control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        if cfg.is_production:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response

    errors.install(app)
    app.include_router(api_router, prefix=cfg.api_prefix)

    @app.get(f"{cfg.api_prefix}/health", tags=["Integration health"], summary="Liveness")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get(f"{cfg.api_prefix}/health/ready", tags=["Integration health"], summary="Readiness (databas och kö)")
    def ready(response: Response) -> dict[str, object]:
        checks: dict[str, str] = {}
        try:
            with get_engine().connect() as conn:
                conn.execute(text("SELECT 1"))
            checks["database"] = "ok"
        except Exception:
            checks["database"] = "error"
        if cfg.jobs_eager or cfg.environment == "test":
            # Enprocessläget: ingen kö att fråga (Redis används inte).
            checks["queue"] = "eager"
            if cfg.single_process:
                checks["scheduler"] = "ok" if _scheduler is not None and _scheduler.running else "error"
        else:
            try:
                import redis

                redis.Redis.from_url(cfg.redis_url, socket_connect_timeout=1, socket_timeout=1).ping()
                checks["queue"] = "ok"
            except Exception:
                checks["queue"] = "error"
        ok = checks["database"] == "ok" and "error" not in (checks["queue"], checks.get("scheduler"))
        if not ok:
            response.status_code = 503
        return {"status": "ok" if ok else "degraded", "checks": checks}

    return app


app = create_app()
