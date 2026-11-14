"""Inställningar, tjänsteställen, säkerhetskopior och radering av historik."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from fastapi import APIRouter, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import DbDep, PrincipalDep, WebPrincipalDep, get_vehicle, owned_vehicle_ids
from app.api.errors import ApiError, bad_request, not_found
from app.models import AuditEvent, KnownLocation, WorkplacePeriod
from app.schemas.common import AuditOut
from app.services import audit, backup, privacy
from app.services import settings as settings_svc
from app.services.learning import feedback

router = APIRouter(tags=["Settings"])


class WorkplaceIn(BaseModel):
    kind: str = Field(default="location", pattern="^(location|home)$")
    location_id: uuid.UUID | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    note: str = Field(default="", max_length=500)


class DeleteHistory(BaseModel):
    scope: str = Field(pattern="^(observations|calendar|exports|trips|notifications)$")
    vehicle_id: uuid.UUID | None = None
    confirm: str = Field(description='Skriv "RADERA" för att bekräfta')


def _reevaluate(db, principal) -> None:
    for vid in owned_vehicle_ids(db, principal.user.id):
        feedback.after_decisions(db, user_id=principal.user.id, vehicle_id=vid, actor=audit.SYSTEM)


def _workplace_out(db, wp: WorkplacePeriod) -> dict[str, Any]:
    loc = db.get(KnownLocation, wp.location_id) if wp.location_id else None
    return {"id": str(wp.id), "kind": wp.kind, "location_id": str(wp.location_id) if wp.location_id else None,
            "location_name": loc.name if loc else ("Bostaden" if wp.kind == "home" else None),
            "valid_from": wp.valid_from, "valid_to": wp.valid_to, "note": wp.note}


@router.get("/settings", summary="Alla inställningar (med standardvärden)")
def get_settings(principal: PrincipalDep, db: DbDep) -> dict[str, Any]:
    return {"settings": settings_svc.get_all(db, principal.user.id), "defaults": settings_svc.DEFAULTS}


@router.patch("/settings/{section}", summary="Ändra en sektion av inställningarna")
def update_settings(section: str, body: dict[str, Any], principal: WebPrincipalDep, db: DbDep) -> dict[str, Any]:
    if section not in settings_svc.DEFAULTS:
        raise not_found("Sektionen")
    before = settings_svc.get_section(db, principal.user.id, section)
    updated = settings_svc.update_section(db, principal.user.id, section, body)
    audit.record(db, principal.actor, "settings.update", "settings", section,
                 before={k: before.get(k) for k in body}, after=body)
    if section in ("tax", "ai", "calendar", "abbreviations"):
        _reevaluate(db, principal)  # påverkar förslag för oavgjorda resor
    db.commit()
    return updated


@router.get("/settings/workplaces/list", summary="Tjänsteställen med giltighetsperioder")
def list_workplaces(principal: PrincipalDep, db: DbDep) -> list[dict[str, Any]]:
    rows = db.execute(
        select(WorkplacePeriod).where(WorkplacePeriod.user_id == principal.user.id)
        .order_by(WorkplacePeriod.valid_from.asc().nulls_first())
    ).scalars()
    return [_workplace_out(db, wp) for wp in rows]


@router.post("/settings/workplaces", status_code=201, summary="Lägg till tjänsteställe (plats eller bostaden)")
def add_workplace(body: WorkplaceIn, principal: WebPrincipalDep, db: DbDep) -> dict[str, Any]:
    if body.valid_from and body.valid_to and body.valid_to < body.valid_from:
        raise bad_request("Slutdatum ligger före startdatum.")
    if body.kind == "location":
        if body.location_id is None:
            raise bad_request("Välj vilken plats som är tjänsteställe.")
        loc = db.get(KnownLocation, body.location_id)
        if loc is None or loc.owner_user_id != principal.user.id:
            raise not_found("Platsen")
    wp = WorkplacePeriod(
        user_id=principal.user.id, kind=body.kind, location_id=body.location_id if body.kind == "location" else None,
        valid_from=body.valid_from, valid_to=body.valid_to, note=body.note,
    )
    db.add(wp)
    db.flush()
    audit.record(db, principal.actor, "workplace.create", "workplace_period", wp.id, after=body.model_dump(mode="json"))
    _reevaluate(db, principal)
    db.commit()
    return _workplace_out(db, wp)


@router.delete("/settings/workplaces/{workplace_id}", status_code=204, summary="Ta bort tjänsteställe")
def delete_workplace(workplace_id: uuid.UUID, principal: WebPrincipalDep, db: DbDep) -> Response:
    wp = db.get(WorkplacePeriod, workplace_id)
    if wp is None or wp.user_id != principal.user.id:
        raise not_found("Tjänstestället")
    audit.record(db, principal.actor, "workplace.delete", "workplace_period", wp.id)
    db.delete(wp)
    db.flush()
    _reevaluate(db, principal)
    db.commit()
    return Response(status_code=204)


@router.get("/audit", response_model=list[AuditOut], summary="Ändringslogg")
def audit_log(principal: WebPrincipalDep, db: DbDep, limit: int = 100, entity_type: str | None = None) -> list[AuditEvent]:
    q = select(AuditEvent).where(AuditEvent.user_id == principal.user.id)
    if entity_type:
        q = q.where(AuditEvent.entity_type == entity_type)
    return list(db.execute(q.order_by(AuditEvent.at.desc()).limit(max(1, min(limit, 500)))).scalars())


@router.post("/settings/delete-history", summary="Radera historik permanent (sekretess)")
def delete_history(body: DeleteHistory, principal: WebPrincipalDep, db: DbDep) -> dict[str, Any]:
    if body.confirm != "RADERA":
        raise bad_request('Skriv "RADERA" för att bekräfta raderingen.', "confirmation_required")
    vehicle = get_vehicle(db, principal, body.vehicle_id) if body.scope in ("observations", "trips") else None
    removed = privacy.delete_history(db, user_id=principal.user.id, scope=body.scope, vehicle=vehicle, actor=principal.actor)
    db.commit()
    return {"removed": removed}


def _admin(principal) -> None:
    if not principal.user.is_admin:
        raise ApiError(403, "Endast administratören kan hantera säkerhetskopior.", "forbidden")


@router.get("/backups", summary="Säkerhetskopior på servern")
def list_backups(principal: WebPrincipalDep) -> list[dict[str, Any]]:
    _admin(principal)
    return backup.list_backups()


@router.post("/backups", status_code=201, summary="Skapa säkerhetskopia nu")
def create_backup(principal: WebPrincipalDep, db: DbDep) -> dict[str, Any]:
    _admin(principal)
    path = backup.create_backup(db)
    audit.record(db, principal.actor, "backup.create", "backup", path.name)
    db.commit()
    return {"name": path.name, "size_bytes": path.stat().st_size}


@router.get("/backups/{name}/download", summary="Hämta en säkerhetskopia")
def download_backup(name: str, principal: WebPrincipalDep) -> FileResponse:
    _admin(principal)
    try:
        path = backup.backup_path(name)
    except backup.BackupError as exc:
        raise not_found("Säkerhetskopian") from exc
    return FileResponse(path, filename=name, media_type="application/zip")


@router.delete("/backups/{name}", status_code=204, summary="Radera en säkerhetskopia")
def delete_backup(name: str, principal: WebPrincipalDep, db: DbDep) -> Response:
    _admin(principal)
    try:
        backup.delete_backup(name)
    except backup.BackupError as exc:
        raise not_found("Säkerhetskopian") from exc
    audit.record(db, principal.actor, "backup.delete", "backup", name)
    db.commit()
    return Response(status_code=204)
