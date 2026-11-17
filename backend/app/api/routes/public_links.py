"""Nedladdning av underlaget via signerad länk, utan inloggning (byggkontraktet C.6 och E.2).

Routen har inget beroende av inloggningen: länkens signatur är behörigheten. Den bygger
på export-id, filens sha256 och användarens ``link_epoch`` och gäller en begränsad tid och
ett begränsat antal hämtningar (services/export/links.py).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from sqlalchemy import func, update

from app.api.deps import DbDep, client_ip
from app.api.errors import ApiError
from app.api.routes.exports import XLSX_MEDIA_TYPE, content_disposition
from app.db import utcnow
from app.models import ExportJob
from app.security import ratelimit
from app.services import audit
from app.services.export import links
from app.services.export import service as export_svc
from app.services.export.service import ExportError

router = APIRouter(tags=["Public"])

RATE_LIMIT = 30
RATE_WINDOW_SECONDS = 600

# Länken får inte läcka vidare eller sparas av mellanled och sökmotorer.
NO_STORE_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex",
    "X-Content-Type-Options": "nosniff",
}


# Utanför API-beskrivningen: webbappen anropar aldrig routen, den nås bara via länken i
# månadsnotisen (och listan över routes som kräver inloggning hålls därmed oförändrad).
@router.get("/public/exports/{token}", summary="Hämta underlaget via nedladdningslänk (utan inloggning)",
            response_class=FileResponse, include_in_schema=False)
def download_by_link(token: str, request: Request, db: DbDep) -> FileResponse:
    allowed, _ = ratelimit.hit(f"plink:{client_ip(request)}", RATE_LIMIT, RATE_WINDOW_SECONDS)
    if not allowed:
        raise ApiError(429, "För många försök. Vänta en stund och försök igen.", "rate_limited")
    if len(token) > 300:
        raise ApiError(404, links.INVALID_TEXT, "link_invalid")
    try:
        job = links.verify(db, token, utcnow())
        path = export_svc.export_path(job)
    except links.LinkExpired as exc:
        raise ApiError(410, exc.message, "link_expired") from exc
    except (links.LinkInvalid, ExportError) as exc:
        raise ApiError(404, links.INVALID_TEXT, "link_invalid") from exc
    # Räknas upp i databasen med villkoret i samma sats: samtidiga anrop kan inte alla läsa samma
    # värde och passera taket (verify ovan har redan läst räknaren).
    counted = db.execute(
        update(ExportJob)
        .where(ExportJob.id == job.id, func.coalesce(ExportJob.link_downloads, 0) < links.LINK_MAX_DOWNLOADS)
        .values(link_downloads=func.coalesce(ExportJob.link_downloads, 0) + 1)
        .execution_options(synchronize_session=False)
    )
    if counted.rowcount != 1:
        db.rollback()
        raise ApiError(404, links.INVALID_TEXT, "link_invalid")
    db.expire(job, ["link_downloads"])
    # Själva länken loggas aldrig, bara att filen hämtades.
    audit.record(db, audit.Actor("link", None, job.user_id, client_ip(request)), "export.link_download",
                 "export_job", job.id, after={"downloads": job.link_downloads})
    db.commit()
    filename = job.filename or "Körjournal.xlsx"
    return FileResponse(
        path, media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": content_disposition(filename), **NO_STORE_HEADERS},
    )
