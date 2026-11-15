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


def bootstrap_admin() -> None:
    """Skapar första administratören från miljövariabler, om angivet och inga konton finns."""
    cfg = get_config()
    if not (cfg.first_admin_email and cfg.first_admin_password):
        return
    from app.models import User
    from app.security import passwords
    from app.services import vehicles

    with session_scope() as db:
        if (db.scalar(select(func.count()).select_from(User)) or 0) > 0:
            return
        user = User(
            email=cfg.first_admin_email.strip().lower(),
            display_name=cfg.first_admin_email.split("@")[0],
            password_hash=passwords.hash_password(cfg.first_admin_password),
            is_admin=True,
        )
        db.add(user)
        db.flush()
        vehicles.default_vehicle(db, user.id)
        vehicles.default_driver(db, user.id, user.display_name)
        logger.info("Första administratörskontot skapades från miljövariabler.")


def _start_dev_ticker():
    """Utvecklingsläge utan Redis (DRIVELOG_JOBS_EAGER=true): kör de schemalagda jobben i en
    bakgrundstråd så att insamling, demoläge och notiser fungerar även utan worker och beat.
    I drift sköts detta av Celery beat – tråden startas aldrig där."""
    import threading

    cfg = get_config()
    if not cfg.jobs_eager or cfg.environment in ("test", "production"):
        return None
    stop = threading.Event()

    def loop() -> None:
        from app.worker import tasks

        minutes = 0
        while not stop.wait(60):
            minutes += 1
            try:
                tasks.tick()
                if minutes % 15 == 0:
                    tasks.calendar_sync_due()
                if minutes % (24 * 60) == 0:
                    tasks.maintenance()
            except Exception:
                logger.exception("Schemalagt jobb misslyckades")

    threading.Thread(target=loop, name="drivelog-dev-ticker", daemon=True).start()
    logger.info("Utvecklingsläge: schemalagda jobb körs i API-processen.")
    return stop


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    cfg = get_config()
    configure_logging()
    problems = cfg.validate_for_production()
    if problems:
        for p in problems:
            logger.error("Konfigurationsfel: %s", p)
        raise RuntimeError("Osäker produktionskonfiguration: " + " ".join(problems))
    for d in (cfg.data_dir, cfg.uploads_dir, cfg.exports_dir, cfg.backups_dir):
        d.mkdir(parents=True, exist_ok=True)
    if not cfg.is_production:
        init_schema()
    bootstrap_admin()
    stop = _start_dev_ticker()
    yield
    if stop is not None:
        stop.set()


def create_app() -> FastAPI:
    cfg = get_config()
    app = FastAPI(
        title="SMGD DriveLog API",
        version=__version__,
        description=(
            "REST-API för SMGD DriveLog – intelligent körjournal. Webbappen autentiserar med "
            "sessionskaka + X-CSRF-Token; Home Assistant-integrationen med Bearer-token."
        ),
        openapi_url=f"{cfg.api_prefix}/openapi.json",
        docs_url=f"{cfg.api_prefix}/docs",
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
            checks["queue"] = "eager"
        else:
            try:
                import redis

                redis.Redis.from_url(cfg.redis_url, socket_connect_timeout=1, socket_timeout=1).ping()
                checks["queue"] = "ok"
            except Exception:
                checks["queue"] = "error"
        ok = checks["database"] == "ok" and checks["queue"] != "error"
        if not ok:
            response.status_code = 503
        return {"status": "ok" if ok else "degraded", "checks": checks}

    return app


app = create_app()
