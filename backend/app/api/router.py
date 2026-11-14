"""Samlar alla routrar under /api/v1."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.routes import (
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
    notifications,
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
    dashboard,
    energy,
    notifications,
    ha,
    integrations,
    volvo,
    settings,
):
    api_router.include_router(module.router)
