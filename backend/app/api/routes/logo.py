"""Företagsloggan i underlaget: ladda upp, visa, byt och ta bort (DEL 11, 1.3.0).

Bara PNG och JPEG (avgjort av filens innehåll), högst 1 MB. Bilden lämnas bara ut till den
inloggade ägaren. "Visa loggan i underlaget" är inställningen ``export.show_logo``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Response, UploadFile
from pydantic import BaseModel

from app.api.deps import DbDep, WebPrincipalDep
from app.api.errors import bad_request, not_found
from app.services import audit
from app.services.export import logo as logo_svc

router = APIRouter(prefix="/settings/logo", tags=["Settings"])


class LogoOut(BaseModel):
    has_logo: bool
    content_type: str | None = None
    width: int | None = None
    height: int | None = None
    size_bytes: int | None = None
    uploaded_at: str | None = None
    show_in_export: bool


@router.get("", summary="Företagsloggan (bilden)", response_class=Response)
def get_logo(principal: WebPrincipalDep, db: DbDep) -> Response:
    found = logo_svc.read(db, principal.user.id)
    if found is None:
        raise not_found("Loggan")
    data, content_type = found
    return Response(content=data, media_type=content_type, headers={"Cache-Control": "no-store"})


@router.post("", response_model=LogoOut, summary="Ladda upp eller byt företagsloggan (PNG eller JPEG, högst 1 MB)")
async def upload_logo(
    file: Annotated[UploadFile, File(description="PNG eller JPEG, högst 1 MB")],
    principal: WebPrincipalDep,
    db: DbDep,
) -> LogoOut:
    data = await file.read(logo_svc.MAX_BYTES + 1)
    try:
        out = logo_svc.save(db, principal.user.id, data)
    except logo_svc.LogoError as exc:
        raise bad_request(str(exc), "invalid_logo") from exc
    audit.record(db, principal.actor, "settings.logo_uploaded", "settings", "export",
                 after={k: out[k] for k in ("content_type", "width", "height", "size_bytes")})
    db.commit()
    return LogoOut(**out)


@router.delete("", response_model=LogoOut, summary="Ta bort företagsloggan")
def delete_logo(principal: WebPrincipalDep, db: DbDep) -> LogoOut:
    out = logo_svc.delete(db, principal.user.id)
    audit.record(db, principal.actor, "settings.logo_removed", "settings", "export")
    db.commit()
    return LogoOut(**out)
