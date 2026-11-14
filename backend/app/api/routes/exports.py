"""Export av körjournalen till Excel: förhandskontroll, förhandsvisning, skapa, lista, hämta och radera."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Query, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator

from app.api.deps import DbDep, PrincipalDep, get_vehicle
from app.api.errors import ApiError, bad_request, not_found
from app.schemas.common import ORM
from app.services import settings as settings_svc
from app.services.export import checks as checks_mod
from app.services.export import service as export_svc
from app.services.export.service import ExportError
from app.services.stats import common
from app.services.stats.common import Period

router = APIRouter(prefix="/exports", tags=["Exports"])

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_TRIP_IDS = 5000
Layout = Literal["simple", "detailed"]


class ExportOut(ORM):
    id: uuid.UUID
    vehicle_id: uuid.UUID
    created_at: datetime
    finished_at: datetime | None
    params: dict[str, Any]
    status: str
    filename: str | None
    sha256: str | None
    is_complete: bool
    warnings: list[dict[str, Any]]
    summary: dict[str, Any]
    error: str | None


class ExportCreate(BaseModel):
    vehicle_id: uuid.UUID | None = None
    date_from: date | None = Field(default=None, alias="from")
    date_to: date | None = Field(default=None, alias="to")
    trip_ids: list[uuid.UUID] | None = Field(default=None, max_length=MAX_TRIP_IDS)
    label: str | None = Field(default=None, max_length=120)
    # simple = ett blad med tjänsteresorna, detailed = fullständig arbetsbok. Utelämnad: inställningen export.layout.
    layout: Layout | None = None

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _check(self) -> ExportCreate:
        if self.trip_ids is None and (self.date_from is None or self.date_to is None):
            raise ValueError("Ange period (från och till) eller välj resor.")
        if self.date_from is not None and self.date_to is not None and self.date_to < self.date_from:
            raise ValueError("Tilldatum får inte ligga före fråndatum.")
        if self.trip_ids is not None and not self.trip_ids:
            raise ValueError("Inga resor valda.")
        return self


_ERROR_STATUS = {"not_found": 404, "no_file": 409, "file_missing": 410}


def _api_error(exc: ExportError) -> ApiError:
    return ApiError(_ERROR_STATUS.get(exc.code, 400), exc.message, exc.code)


def _bounds(db, user_id: uuid.UUID, date_from: date | None, date_to: date | None):
    tz = settings_svc.user_timezone(db, user_id)
    start = common.local_midnight(date_from, tz) if date_from else None
    end = common.local_midnight(date_to + timedelta(days=1), tz) if date_to else None
    return tz, start, end


def content_disposition(filename: str) -> str:
    """Bilaga med ASCII-reserv och korrekt UTF-8-namn (RFC 6266/5987)."""
    fallback = (
        filename.replace("Å", "A").replace("Ä", "A").replace("Ö", "O").replace("å", "a").replace("ä", "a").replace("ö", "o")
    )
    fallback = "".join(ch if 32 <= ord(ch) < 127 and ch not in '"\\;' else "_" for ch in fallback)
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"


def _period_args(
    db, principal, vehicle_id: uuid.UUID | None, date_from: date | None, date_to: date | None,
    trip_ids: list[uuid.UUID] | None,
):
    """Gemensam tolkning av period/urval för förhandskontroll och förhandsvisning."""
    vehicle = get_vehicle(db, principal, vehicle_id)
    if trip_ids is None and (date_from is None or date_to is None):
        raise bad_request("Ange period (från och till) eller välj resor.", "invalid_period")
    if date_from is not None and date_to is not None and date_to < date_from:
        raise bad_request("Tilldatum får inte ligga före fråndatum.", "invalid_period")
    if trip_ids is not None and len(trip_ids) > MAX_TRIP_IDS:
        raise bad_request("För många resor valda.", "too_many_trips")
    tz, start, end = _bounds(db, principal.user.id, date_from, date_to)
    if trip_ids is not None:
        start = end = None
    return vehicle, tz, start, end


@router.get("/checks", summary="Förhandskontroll inför export")
def precheck(
    principal: PrincipalDep,
    db: DbDep,
    vehicle_id: uuid.UUID | None = None,
    date_from: date | None = Query(default=None, alias="from"),
    date_to: date | None = Query(default=None, alias="to"),
    trip_ids: list[uuid.UUID] | None = Query(default=None),
    layout: Layout | None = Query(default=None, description="simple eller detailed; utelämnad: inställningen"),
) -> dict[str, Any]:
    vehicle, tz, start, end = _period_args(db, principal, vehicle_id, date_from, date_to, trip_ids)
    user_id = principal.user.id
    scope = settings_svc.journal_scope(db, user_id)
    layout = export_svc.resolve_layout(db, user_id, layout)
    result = checks_mod.run_checks(db, vehicle=vehicle, start=start, end=end, tz=tz, trip_ids=trip_ids, scope=scope)
    summary = None
    if layout == settings_svc.LAYOUT_SIMPLE:
        journal, _ = export_svc.build_simple_journal(db, vehicle=vehicle, start=start, end=end, tz=tz, trip_ids=trip_ids)
        summary = export_svc.simple_summary(journal)
    db.commit()
    return {
        "vehicle_id": str(vehicle.id),
        "is_demo": vehicle.is_demo,
        "scope": scope,
        "layout": layout,
        "period": {"start": start, "end": end, "timezone": str(tz)},
        **checks_mod.summarize(result),
        "status_label": checks_mod.status_label(result, scope),
        "checks": result,
        "summary": summary,
    }


@router.get("/preview", summary="Förhandsvisning av den enkla körjournalen")
def preview(
    principal: PrincipalDep,
    db: DbDep,
    vehicle_id: uuid.UUID | None = None,
    date_from: date | None = Query(default=None, alias="from"),
    date_to: date | None = Query(default=None, alias="to"),
    trip_ids: list[uuid.UUID] | None = Query(default=None),
) -> dict[str, Any]:
    """Exakt de rader och summor som den enkla exporten (layout ``simple``) skulle innehålla."""
    vehicle, tz, start, end = _period_args(db, principal, vehicle_id, date_from, date_to, trip_ids)
    journal, trips = export_svc.build_simple_journal(db, vehicle=vehicle, start=start, end=end, tz=tz, trip_ids=trip_ids)
    if trip_ids is not None and len(trips) != len(set(trip_ids)):
        raise not_found("En eller flera valda resor")
    return {
        "vehicle_id": str(vehicle.id),
        "is_demo": vehicle.is_demo,
        "layout": settings_svc.LAYOUT_SIMPLE,
        "scope": settings_svc.journal_scope(db, principal.user.id),
        "period": {"start": start, "end": end, "timezone": str(tz), "label": journal.period_label},
        "filename": export_svc.download_name(
            vehicle, Period("export", start, end, journal.period_label), tz, selection=trip_ids is not None
        ),
        **journal.as_dict(),
    }


@router.post("", response_model=ExportOut, status_code=201, summary="Skapa en Excel-körjournal")
def create(body: ExportCreate, principal: PrincipalDep, db: DbDep) -> Any:
    vehicle = get_vehicle(db, principal, body.vehicle_id)
    tz, start, end = _bounds(db, principal.user.id, body.date_from, body.date_to)
    try:
        job = export_svc.create_export(
            db, user_id=principal.user.id, vehicle=vehicle, start=start, end=end, trip_ids=body.trip_ids,
            actor=principal.actor, label=body.label, tz=tz, layout=body.layout,
        )
    except ExportError as exc:
        db.rollback()
        raise _api_error(exc) from exc
    db.commit()
    return job


@router.get("", response_model=list[ExportOut], summary="Skapade exporter")
def list_all(principal: PrincipalDep, db: DbDep, vehicle_id: uuid.UUID | None = None) -> Any:
    if vehicle_id is not None:
        get_vehicle(db, principal, vehicle_id)  # ägarkontroll
    return export_svc.list_exports(db, principal.user.id, vehicle_id)


@router.get("/{export_id}", response_model=ExportOut, summary="En export")
def get_one(export_id: uuid.UUID, principal: PrincipalDep, db: DbDep) -> Any:
    try:
        return export_svc.get_export(db, principal.user.id, export_id)
    except ExportError as exc:
        raise not_found("Exporten") from exc


@router.get("/{export_id}/download", summary="Hämta exportfilen (.xlsx)")
def download(export_id: uuid.UUID, principal: PrincipalDep, db: DbDep) -> FileResponse:
    try:
        job = export_svc.get_export(db, principal.user.id, export_id)
        path = export_svc.export_path(job)
    except ExportError as exc:
        raise _api_error(exc) from exc
    filename = job.filename or "Körjournal.xlsx"
    return FileResponse(
        path,
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": content_disposition(filename), "Cache-Control": "no-store",
                 "X-Content-Type-Options": "nosniff"},
    )


@router.delete("/{export_id}", status_code=204, summary="Radera exporten och filen")
def delete(export_id: uuid.UUID, principal: PrincipalDep, db: DbDep) -> Response:
    try:
        job = export_svc.get_export(db, principal.user.id, export_id)
    except ExportError as exc:
        raise not_found("Exporten") from exc
    export_svc.delete_export(db, job, actor=principal.actor)
    db.commit()
    return Response(status_code=204)
