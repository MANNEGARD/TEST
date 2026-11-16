"""Samlar alla routrar under /api/v1."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.routes import (
    about,
    auth,
    calendar,
    dashboard,
    energy,
    exports,
    ha,
    imports,
    integrations,
    learning,
    locations,
    merging,
    month_close,
    notifications,
    public_links,
    review,
    settings,
    trips,
    vehicles,
    volvo,
)

api_router = APIRouter()
for module in (
    auth,
    vehicles,
    trips,
    merging,
    review,
    learning,
    locations,
    calendar,
    imports,
    exports,
    month_close,
    public_links,
    dashboard,
    energy,
    notifications,
    ha,
    integrations,
    volvo,
    settings,
    about,
):
    api_router.include_router(module.router)
