"""Gemensamma hjälpare för testerna av omgången AI i 1.3.0 (påhittade platser och personer)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import app.models as M
from app.models.enums import LocationType, TripSource
from app.services import audit
from app.services import settings as settings_svc
from app.services.calendar import matching
from app.services.classify import autopilot, engine, llm
from tests.conftest import reload_trip

TZ = ZoneInfo("Europe/Stockholm")
ACTIVE_SINCE = "2020-01-01T00:00:00+00:00"
HOME = "Hemvägen 1, 111 11 Exempelstad"
OFFICE = "Kontorsgatan 2, 111 22 Exempelstad"
JKPG_OFFICE = "Kontorsvägen 9, 553 20 Jönköping"
HBG_OFFICE = "Hamnkontoret 3, 252 25 Helsingborg"
CUSTOMER = "Industrivägen 10, 222 33 Verkstadsby"
HOTEL = "Hotellgatan 8, 553 21 Jönköping"
CAFE = "Fikagatan 4, 111 33 Exempelstad"
OTHER_CITY = "Torget 1, 652 25 Karlstad"
WORK_DOMAIN = "exempelakeri.se"


def local(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=TZ).astimezone(UTC)


class FakeProvider:
    """En AI-leverantör utan nätverk. ``data`` kan vara en funktion av underlaget (dokumentet)."""

    name, model = "anthropic", "fake-1"

    def __init__(self, data: Any = None, *, fail: bool = False):
        self.data = data
        self.fail = fail
        self.documents: list[dict[str, Any]] = []

    def assess(self, document: dict[str, Any]) -> llm.LLMResult | None:
        self.documents.append(document)
        if self.fail:
            raise llm.LLMError("Kunde inte nå Anthropic.", retryable=True)
        data = self.data(document) if callable(self.data) else self.data
        return llm.LLMResult(data=llm._coerce(data), provider=self.name, model=self.model, usage={})


def answer(purpose: str | None, *, category: str = "work", supported: bool = True, confidence: float = 0.8) -> dict:
    return {"category": category, "confidence": confidence, "purpose": purpose, "purpose_supported": supported,
            "evidence": ["kalenderbokning"], "requires_review": False}


def full_context(db, user, *, about_work: str = "") -> None:
    settings_svc.update_section(db, user.id, "ai", {"context_level": "full", "about_work": about_work})
    db.flush()


def self_driving(db, user) -> None:
    settings_svc.update_section(db, user.id, "automation", {"mode": "self_driving"})
    settings_svc.update_section(db, user.id, "automation", {"active_since": ACTIVE_SINCE}, allow_read_only=True)
    db.flush()


def event(db, user, subject: str, start: datetime, minutes: int = 60, **kw: Any) -> M.CalendarEvent:
    ev = M.CalendarEvent(
        user_id=user.id, calendar_id="cal", external_id=f"ev-{subject}-{start.isoformat()}", subject=subject,
        start_at=start, end_at=start + timedelta(minutes=minutes), **kw,
    )
    db.add(ev)
    db.flush()
    return ev


def live(trips, start: str, end: str, **kw: Any) -> M.LogicalTrip:
    return trips.trip(start, end, source=TripSource.VOLVO_API, **kw)


def match(db, user, trip) -> list[M.CalendarMatch]:
    return matching.match_trip(db, trip, user_id=user.id, is_demo=False)


def classify(db, user, trip, provider=None, **kw: Any):
    return engine.classify_trip(db, reload_trip(db, trip.id), user_id=user.id, actor=audit.SYSTEM,
                                provider=provider, force=True, **kw)


def run(db, user, vehicle) -> dict[str, int]:
    out = autopilot.run_vehicle(db, user_id=user.id, vehicle=vehicle, actor=audit.SYSTEM)
    db.flush()
    return out


def decide(db, user, trip, now: datetime | None = None):
    cfg = autopilot.config(db, user.id, now=now)
    return autopilot.decide(db, reload_trip(db, trip.id), user_id=user.id, ctx=None, rules=None, cfg=cfg)


def places(trips) -> dict[str, M.KnownLocation]:
    out = {
        "home": trips.location(HOME, name="Hemmet", type=LocationType.HOME),
        "office": trips.location(OFFICE, name="Kontoret", type=LocationType.REGULAR_WORKPLACE),
        "jkpg": trips.location(JKPG_OFFICE, name="Jönköpingskontoret", type=LocationType.OFFICE),
        "hbg": trips.location(HBG_OFFICE, name="Helsingborgskontoret", type=LocationType.OFFICE),
        "customer": trips.location(CUSTOMER, name="Exempel Åkeri AB", type=LocationType.CUSTOMER,
                                   company="Exempel Åkeri AB"),
        "hotel": trips.location(HOTEL, name="Hotell Exempel", type=LocationType.HOTEL),
    }
    cities = {"home": "Exempelstad", "office": "Exempelstad", "jkpg": "Jönköping", "hbg": "Helsingborg",
              "customer": "Verkstadsby", "hotel": "Jönköping"}
    for key, loc in out.items():
        loc.city = cities[key]
    trips.db.flush()
    return out


def attendee(name: str, email: str, role: str = "attendee") -> dict[str, str]:
    return {"name": name, "email": email, "role": role}


def current_purpose(db, trip) -> M.TripPurpose | None:
    from sqlalchemy import select

    return db.execute(
        select(M.TripPurpose).where(M.TripPurpose.logical_trip_id == trip.id, M.TripPurpose.is_current.is_(True))
    ).scalars().first()
