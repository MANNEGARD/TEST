"""Om DriveLog: versionen som körs och vad som är nytt i den (ur ändringsloggen)."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.deps import WebPrincipalDep
from app.services import release

router = APIRouter(tags=["Om DriveLog"])


class WhatsNewOut(BaseModel):
    version: str
    items: list[str]


class AboutOut(BaseModel):
    version: str
    whats_new: WhatsNewOut | None


@router.get("/about", response_model=AboutOut, summary="DriveLogs version och nyheterna i den")
def about(_: WebPrincipalDep) -> AboutOut:
    version = release.current_version()
    new = release.whats_new(version)
    return AboutOut(version=version, whats_new=WhatsNewOut.model_validate(new) if new else None)
