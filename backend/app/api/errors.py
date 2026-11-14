"""Enhetliga felsvar: {"detail": "<svenskt meddelande>", "code": "<maskinläsbar kod>"}."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.services.importer.service import ImportError_
from app.services.settings import SettingsError
from app.services.trips.segments import TripError

logger = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(self, status_code: int, detail: str, code: str = "error", data: dict[str, Any] | None = None):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.code = code
        self.data = data or {}


def not_found(what: str = "Resursen") -> ApiError:
    return ApiError(404, f"{what} hittades inte.", "not_found")


def bad_request(detail: str, code: str = "bad_request", data: dict[str, Any] | None = None) -> ApiError:
    return ApiError(400, detail, code, data)


def conflict(detail: str, code: str = "conflict", data: dict[str, Any] | None = None) -> ApiError:
    return ApiError(409, detail, code, data)


_TRIP_STATUS = {"not_found": 404, "segment_not_found": 404}


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        body: dict[str, Any] = {"detail": exc.detail, "code": exc.code}
        if exc.data:
            body["data"] = exc.data
        return JSONResponse(status_code=exc.status_code, content=body)

    @app.exception_handler(TripError)
    async def _trip_error(_: Request, exc: TripError) -> JSONResponse:
        body: dict[str, Any] = {"detail": exc.message, "code": exc.code}
        if exc.data:
            body["data"] = exc.data
        return JSONResponse(status_code=_TRIP_STATUS.get(exc.code, 409), content=body)

    @app.exception_handler(ImportError_)
    async def _import_error(_: Request, exc: ImportError_) -> JSONResponse:
        status = 404 if exc.code == "not_found" else 400
        return JSONResponse(status_code=status, content={"detail": exc.message, "code": exc.code})

    @app.exception_handler(SettingsError)
    async def _settings_error(_: Request, exc: SettingsError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc), "code": "invalid_setting"})

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        fields = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err.get("loc", []) if p not in ("body", "query", "path"))
            fields.append({"field": loc, "message": err.get("msg", "")})
        return JSONResponse(
            status_code=422,
            content={"detail": "Ogiltiga uppgifter i förfrågan.", "code": "validation_error", "fields": fields},
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Detaljer loggas på servern men lämnas aldrig ut till klienten.
        logger.exception("Ohanterat fel i %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "Ett oväntat fel inträffade.", "code": "internal_error"})
