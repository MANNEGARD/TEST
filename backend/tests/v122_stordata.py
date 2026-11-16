"""Påhittad stor körjournal för prestanda- och jämförelsetesterna i 1.2.2.

Ett fordon med ungefär tusen importerade resor från flera år (hem, kontor, kunder och
privata ställen) och ett tjugotal öppna ärenden: oavgjorda importerade resor till nya
kunder och – i självgående läge – resor från bilen som väntar på ett beslut. Alla
adresser och namn är påhittade. Samma frö ger alltid samma data.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import timedelta

import app.models as M
from app.db import utcnow
from app.models.enums import Category, DecisionSource, LocationType, ReviewStatus, TripSource
from app.services import audit
from app.services import settings as settings_svc
from app.services.trips import pipeline
from tests.conftest import BASE_TIME, TripFactory

HOME = "Hemgatan 1, 111 11 Påhittby"
OFFICE = "Kontorsvägen 2, 111 12 Påhittby"
CUSTOMERS = [f"Kundgatan {i}, 222 {i:02d} Exempelstad" for i in range(1, 13)]
PRIVATE = [f"Fritidsvägen {i}, 333 {i:02d} Låtsasköping" for i in range(1, 9)]
NEW_CUSTOMERS = [f"Nyvägen {i}, 444 {i:02d} Testinge" for i in range(1, 5)]


@dataclass
class Fleet:
    open_trip_ids: list = field(default_factory=list)
    live_trip_ids: list = field(default_factory=list)


def build(db, user: M.User, vehicle: M.Vehicle, *, imported: int = 1000, open_imported: int = 8,
          live: int = 6, self_driving: bool = True, seed: int = 122) -> Fleet:
    rng = random.Random(seed)
    trips = TripFactory(db, user, vehicle)
    trips.location(HOME, name="Hemmet", type=LocationType.HOME)
    trips.location(OFFICE, name="Kontoret", type=LocationType.REGULAR_WORKPLACE)
    for i, address in enumerate(CUSTOMERS, 1):
        trips.location(address, name=f"Kund {i}", type=LocationType.CUSTOMER, company=f"Kund {i} AB")
    for i, address in enumerate(PRIVATE, 1):
        trips.location(address, name=f"Fritid {i}", type=LocationType.PRIVATE)

    fleet = Fleet()
    trips._clock = BASE_TIME - timedelta(days=900)
    where = HOME
    for _ in range(imported):
        roll = rng.random()
        if where != HOME and roll < 0.45:
            dest, category, purpose = HOME, Category.PRIVATE, None
        elif roll < 0.65:
            dest, category, purpose = OFFICE, Category.WORK, "Kontorsarbete"
        elif roll < 0.85:
            n = rng.randrange(len(CUSTOMERS))
            dest, category, purpose = CUSTOMERS[n], Category.WORK, f"Kundbesök Kund {n + 1}"
        else:
            dest, category, purpose = rng.choice(PRIVATE), Category.PRIVATE, None
        if dest == where:
            dest, category, purpose = (OFFICE, Category.WORK, "Kontorsarbete") if where == HOME else (HOME, Category.PRIVATE, None)
        trips.trip(where, dest, km=float(rng.randint(3, 60)), minutes=rng.randint(8, 70),
                   category=category, review_status=ReviewStatus.IMPORTED, purpose=purpose,
                   category_source=DecisionSource.IMPORT)
        trips.skip(hours=rng.randint(2, 30))
        where = dest

    # Oavgjorda importerade resor till nya kunder: de blir ärenden i Granskning.
    for i in range(open_imported):
        dest = NEW_CUSTOMERS[i % len(NEW_CUSTOMERS)]
        t = trips.trip(where, dest, km=float(rng.randint(5, 40)), minutes=rng.randint(10, 50))
        fleet.open_trip_ids.append(t.id)
        trips.skip(hours=rng.randint(2, 20))
        where = dest
        t = trips.trip(where, HOME, km=float(rng.randint(5, 40)), minutes=rng.randint(10, 50),
                       category=Category.PRIVATE, review_status=ReviewStatus.IMPORTED,
                       category_source=DecisionSource.IMPORT)
        trips.skip(hours=rng.randint(2, 20))
        where = HOME

    if self_driving:
        settings_svc.update_section(db, user.id, "automation", {"mode": settings_svc.MODE_SELF_DRIVING})
        settings_svc.update_section(
            db, user.id, "automation",
            {"active_since": (utcnow() - timedelta(hours=1)).isoformat(timespec="seconds")}, allow_read_only=True,
        )
    # Resor från bilen (efter att självgående läge började gälla): en utresa på morgonen och
    # hemresan samma dag, en dag i taget.
    first_day = (utcnow() - timedelta(days=live + 1)).replace(hour=6, minute=0, second=0, microsecond=0)
    for i in range(live):
        dest = NEW_CUSTOMERS[i % len(NEW_CUSTOMERS)] if i % 2 == 0 else CUSTOMERS[i % len(CUSTOMERS)]
        km = float(rng.randint(5, 40))
        trips._clock = first_day + timedelta(days=i)
        t = trips.trip(HOME, dest, km=km, minutes=rng.randint(10, 40), source=TripSource.VOLVO_API)
        fleet.live_trip_ids.append(t.id)
        trips.skip(hours=3)
        trips.trip(dest, HOME, km=km, minutes=rng.randint(10, 40), source=TripSource.VOLVO_API)
    db.flush()
    pipeline.after_trips_changed(db, user_id=user.id, vehicle=vehicle, actor=audit.SYSTEM, notify=False,
                                 origin="import", use_llm=False)
    db.commit()
    return fleet
