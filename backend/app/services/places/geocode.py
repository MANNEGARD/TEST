"""Adresser för bilens stopp (omvänd geokodning mot OpenStreetMap/Nominatim).

STUBBE (G0): signaturerna följer byggkontraktet (C.1). Modulen beter sig som i dag tills
S1a fyller den: ``place_for_position`` hittar bara kända platser och inget nätverksanrop
görs. ``validate_runtime_url`` gör redan den statiska kontrollen av adressen.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import httpx
from sqlalchemy.orm import Session

from app import __version__
from app.models import KnownLocation
from app.services import settings as settings_svc
from app.services.places import registry as places

USER_AGENT = f"SMGD-DriveLog/{__version__} (self-hosted logbook; reverse geocoding)"

# Transport som används när anroparen inte anger någon. Testerna sätter en "offline"-
# transport här (tests/conftest.py), så att ingen test kan nå nätet.
_default_transport: httpx.BaseTransport | None = None


def set_default_transport(transport: httpx.BaseTransport | None) -> None:
    global _default_transport
    _default_transport = transport


def default_transport() -> httpx.BaseTransport | None:
    return _default_transport


@dataclass(frozen=True)
class GeoAddress:
    label: str  # "Storgatan 5, 553 20 Jönköping", alltid utan POI-namn
    street: str | None
    house_number: str | None
    postcode: str | None
    city: str | None
    poi_name: str | None
    osm_class: str | None
    place_kind: Literal["work", "never", "unclear"]


def place_for_position(
    db: Session, *, user_id: uuid.UUID, lat: float | None, lon: float | None, demo: bool
) -> KnownLocation | None:
    """Känd plats för positionen. (Stubbe: skapar ännu ingen ny plats.)"""
    loc, _ = places.resolve_position(db, user_id, lat, lon, demo=demo)
    return loc


def run_pending(now: datetime | None = None, limit: int = 5) -> dict[str, Any]:
    """Slår upp väntande platser. (Stubbe: gör inget.)"""
    return {}


def reverse(
    *, provider_url: str, lat_key: int, lon_key: int, transport: httpx.BaseTransport | None = None
) -> GeoAddress | None:
    """Själva nätverksanropet. (Stubbe: inget anrop, inget svar.)"""
    return None


def validate_runtime_url(url: str) -> str:
    """Kontrollerar geokodarens adress före ett anrop; kastar ValueError.

    (Stubbe: bara den statiska kontrollen. DNS-svaret prövas när S1a bygger klienten.)
    """
    return settings_svc.validate_geocoder_url(url)
