"""Historikimport: uppladdning, förhandsgranskning, bekräfta och ångra."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.deps import DbDep, PrincipalDep, WebPrincipalDep, get_vehicle
from app.api.errors import ApiError, bad_request, not_found
from app.config import get_config
from app.models import ImportBatch, ImportedFile, ImportRow
from app.models.enums import COVERAGE_LABELS_SV, BatchStatus
from app.schemas.common import ImportBatchOut, ImportRowOut
from app.services import settings as settings_svc
from app.services.importer import service as imp

router = APIRouter(prefix="/imports", tags=["Imports"])

MAX_FILES = 24


class CommitRequest(BaseModel):
    coverage: str | None = Field(default=None, pattern="^(complete|selection|incomplete|unknown)$")
    apply_merge_rules: bool | None = None


def _batch(db, principal, batch_id: uuid.UUID) -> ImportBatch:
    return imp.get_batch(db, principal.user.id, batch_id)


def _batch_out(db, batch: ImportBatch) -> ImportBatchOut:
    """Importen med ``stored`` per fil i sammanställningen (originalfilen finns kvar på disk)."""
    out = ImportBatchOut.model_validate(batch)
    out.summary = imp.summary_with_storage(db, batch)
    return out


@router.post("/preview", response_model=ImportBatchOut, status_code=201,
             summary="Ladda upp en eller flera Volvo-exporter (XLSX/CSV) och förhandsgranska")
async def preview(
    principal: WebPrincipalDep,
    db: DbDep,
    files: Annotated[list[UploadFile], File(description="En eller flera XLSX/CSV-filer")],
    vehicle_id: Annotated[uuid.UUID | None, Form()] = None,
) -> ImportBatchOut:
    cfg = get_config()
    if not files:
        raise bad_request("Ingen fil har laddats upp.")
    if len(files) > MAX_FILES:
        raise bad_request(f"Högst {MAX_FILES} filer kan laddas upp åt gången.")
    vehicle = get_vehicle(db, principal, vehicle_id)
    payload: list[tuple[str, bytes]] = []
    for f in files:
        # Läs högst gränsen + 1 byte så att för stora filer avvisas utan att fylla minnet.
        data = await f.read(cfg.max_upload_bytes + 1)
        if len(data) > cfg.max_upload_bytes:
            raise ApiError(413, f"Filen {f.filename!r} är större än tillåtna {cfg.max_upload_bytes // (1024 * 1024)} MB.",
                           "file_too_large")
        payload.append((f.filename or "fil", data))
    batch = imp.create_preview(db, user_id=principal.user.id, vehicle=vehicle, files=payload, actor=principal.actor)
    db.commit()
    return _batch_out(db, batch)


@router.get("", summary="Tidigare importer")
def list_batches(principal: PrincipalDep, db: DbDep, vehicle_id: uuid.UUID | None = None,
                 limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    vehicle = get_vehicle(db, principal, vehicle_id)
    rows = db.execute(
        select(ImportBatch).where(ImportBatch.user_id == principal.user.id, ImportBatch.vehicle_id == vehicle.id)
        .order_by(ImportBatch.created_at.desc()).limit(limit)
    ).scalars()
    return {
        "items": [_batch_out(db, b).model_dump(mode="json") for b in rows],
        "coverage_labels": {k: v for k, v in COVERAGE_LABELS_SV.items()},
        "default_coverage": settings_svc.get_section(db, principal.user.id, "import")["default_coverage"],
    }


@router.get("/{batch_id}", response_model=ImportBatchOut, summary="En import med sammanställning")
def get_batch(batch_id: uuid.UUID, principal: PrincipalDep, db: DbDep) -> ImportBatchOut:
    return _batch_out(db, _batch(db, principal, batch_id))


@router.get("/{batch_id}/rows", summary="Rader i en import (med fel och varningar per rad)")
def rows(
    batch_id: uuid.UUID, principal: PrincipalDep, db: DbDep,
    status: list[str] | None = Query(default=None), only_problems: bool = False,
    page: int = Query(default=1, ge=1), page_size: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    batch = _batch(db, principal, batch_id)
    stmt = select(ImportRow).join(ImportedFile).where(ImportedFile.batch_id == batch.id)
    if status:
        stmt = stmt.where(ImportRow.status.in_(status))
    if only_problems:
        stmt = stmt.where(ImportRow.status.in_(("error", "duplicate_differs")))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    items = db.execute(
        stmt.order_by(ImportedFile.filename, ImportRow.row_number).offset((page - 1) * page_size).limit(page_size)
    ).scalars().all()
    files = {f.id: f.filename for f in batch.files}
    out = []
    for r in items:
        d = ImportRowOut.model_validate(r).model_dump(mode="json")
        d["filename"] = files.get(r.file_id)
        out.append(d)
    return {"items": out, "total": total, "page": page, "page_size": page_size}


@router.post("/{batch_id}/commit", summary="Bekräfta importen och skapa resorna")
def commit(batch_id: uuid.UUID, body: CommitRequest, principal: WebPrincipalDep, db: DbDep) -> dict[str, Any]:
    batch = _batch(db, principal, batch_id)
    result = imp.commit(db, batch, actor=principal.actor, coverage=body.coverage, apply_merge_rules=body.apply_merge_rules)
    db.commit()
    return {**result, **imp.summary_with_storage(db, batch, {"files": result.get("files")})}


@router.post("/{batch_id}/undo", summary="Ångra importen (resorna makuleras)")
def undo(batch_id: uuid.UUID, principal: WebPrincipalDep, db: DbDep) -> dict[str, Any]:
    batch = _batch(db, principal, batch_id)
    result = imp.undo(db, batch, actor=principal.actor)
    db.commit()
    return result


@router.delete("/{batch_id}/files", summary="Radera lagrade originalfiler (resorna påverkas inte)")
def delete_files(batch_id: uuid.UUID, principal: WebPrincipalDep, db: DbDep) -> dict[str, int]:
    batch = _batch(db, principal, batch_id)
    removed = imp.delete_original_files(db, batch, actor=principal.actor)
    db.commit()
    return {"removed": removed}


@router.get("/{batch_id}/files/{file_id}/download", summary="Hämta en lagrad originalfil")
def download_file(batch_id: uuid.UUID, file_id: uuid.UUID, principal: WebPrincipalDep, db: DbDep) -> FileResponse:
    from pathlib import Path

    batch = _batch(db, principal, batch_id)
    f = db.get(ImportedFile, file_id)
    if f is None or f.batch_id != batch.id or not f.stored_path:
        raise not_found("Filen")
    path = Path(f.stored_path).resolve()
    root = get_config().uploads_dir.resolve()
    if root not in path.parents or not path.is_file():  # skydd mot sökvägsmanipulation
        raise not_found("Filen")
    return FileResponse(path, filename=f.filename, media_type="application/octet-stream")


__all__ = ["router", "BatchStatus"]
