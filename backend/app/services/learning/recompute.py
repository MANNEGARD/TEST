"""Omräkning efter ägarens beslut – efter svaret, i korta steg.

Ett beslut i Granskning (Arbete, Privat, Godkänn, syfte), massredigering, ett svar på en
fråga och "Ångra" kan påverka förslagen för andra resor: mönstren räknas om, oavgjorda
resor bedöms på nytt, autopiloten prövar väntande resor i självgående läge och ärendena
stäms av. Med många hundra resor tar det flera sekunder, och ägaren ska inte behöva vänta
på det innan nästa ärende går att besvara.

Därför görs i anropet bara själva beslutet och resans egna ärenden. Resten beställs här:

* ``request`` räknar upp ``Vehicle.recompute_requested`` i samma transaktion som beslutet.
  Beställningen sparas alltså bara om beslutet sparas, och ligger kvar om processen startas
  om innan omräkningen hunnit göras.
* Efter commit köas jobbet ``learning.recompute`` med en kort fördröjning. Flera beslut i
  rad slås ihop till en omräkning (enprocessläget: jobs.enqueue med countdown; Compose:
  Celery). Ticken kör dessutom ``run_due`` varje minut för beställningar som blivit kvar.
* ``run_vehicle`` tar ett lås med utgångstid på fordonet, räknar om och sätter
  ``recompute_done`` till det värde räknaren hade när omräkningen började. Kom ett nytt
  beslut under tiden görs en omräkning till.

Själva omräkningen är densamma som ``feedback.after_decisions`` (samma förslag, samma
automatiska godkännanden, samma ärenden), men görs i korta transaktioner om högst
``CHUNK`` resor. Varje steg låser först det det ska skriva (SQLite: skrivlåset tas direkt;
PostgreSQL: resorna låses med SELECT … FOR UPDATE), så att ett beslut som kommer under
tiden aldrig skrivs över av en äldre bild – och nästa klick väntar som mest ett kort steg.

En skillnad i spåret: ärenden för andra resor som omräkningen stänger får ``resolved_by``
"system" (den gamla vägen skrev ägaren, eftersom det skedde i ägarens anrop). Det är
omräkningen som stänger dem, och fältet visas inte i webbappen.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

from sqlalchemy import event, or_, select, update
from sqlalchemy.orm import Session, selectinload

from app.db import _is_sqlite_memory, session_scope, utcnow
from app.models import LogicalTrip, TripSegment, Vehicle
from app.models.enums import LogicalState
from app.services import audit

logger = logging.getLogger(__name__)

JOB = "learning.recompute"
# Väntetid efter ett beslut innan omräkningen startar (nya beslut under tiden slås ihop).
DEBOUNCE_SECONDS = 1.5
# Så många resor räknas om per transaktion.
CHUNK = 50
# En omräkning som inte blivit klar på så här lång tid räknas som avbruten.
LEASE = timedelta(minutes=15)
# SQLite: paus mellan stegen, så att ett anrop som väntar på skrivlåset alltid kommer före.
SQLITE_PAUSE_SECONDS = 0.05
# Högst så många omräkningar i följd per jobb (nya beslut under tiden); resten tar ticken.
MAX_ROUNDS = 5

_DEFER_KEY = "drivelog_recompute_defer"
_PENDING_KEY = "drivelog_recompute_vehicles"


# ----------------------------------------------------------------------------- beställning


def defer(db: Session) -> None:
    """Anropet svarar ägaren direkt: beslut i den här sessionen räknas om efter svaret."""
    db.info[_DEFER_KEY] = True


def is_deferred(db: Session) -> bool:
    return bool(db.info.get(_DEFER_KEY))


def request(db: Session, vehicle_id: uuid.UUID) -> None:
    """Beställer en omräkning för fordonet (en gång per transaktion).

    Anropas innan resan ändras: på PostgreSQL låses fordonsraden då före resan, i samma
    ordning som fordonsinsamlingen (som låser fordonet och sedan skriver resor).
    """
    pending: set[uuid.UUID] = db.info.setdefault(_PENDING_KEY, set())
    if vehicle_id in pending:
        return
    db.execute(
        update(Vehicle)
        .where(Vehicle.id == vehicle_id)
        .values(recompute_requested=Vehicle.recompute_requested + 1, updated_at=Vehicle.updated_at)
        .execution_options(synchronize_session=False)
    )
    pending.add(vehicle_id)


@event.listens_for(Session, "after_commit")
def _enqueue_after_commit(session: Session) -> None:
    from app import jobs

    for vehicle_id in session.info.pop(_PENDING_KEY, set()):
        jobs.enqueue(JOB, countdown=DEBOUNCE_SECONDS, vehicle_id=str(vehicle_id))


@event.listens_for(Session, "after_rollback")
def _forget_after_rollback(session: Session) -> None:
    session.info.pop(_PENDING_KEY, None)


def is_pending(db: Session, vehicle_id: uuid.UUID) -> bool:
    row = db.execute(
        select(Vehicle.recompute_requested, Vehicle.recompute_done).where(Vehicle.id == vehicle_id)
    ).first()
    return row is not None and row[0] > row[1]


def release_leases() -> None:
    """Enprocessläget vid start: ingen omräkning kan pågå, så låsen släpps direkt och
    beställningar som blev kvar vid en omstart görs vid nästa tick."""
    with session_scope() as db:
        db.execute(
            update(Vehicle)
            .where(Vehicle.recompute_started_at.is_not(None))
            .values(recompute_started_at=None, updated_at=Vehicle.updated_at)
        )


# ----------------------------------------------------------------------------- körning


def run_due() -> dict[str, int]:
    """Ticken: räknar om alla fordon med en beställning som inte är gjord (och inte pågår)."""
    with session_scope() as db:
        ids = list(
            db.execute(
                select(Vehicle.id).where(
                    Vehicle.recompute_requested > Vehicle.recompute_done,
                    or_(Vehicle.recompute_started_at.is_(None), Vehicle.recompute_started_at < utcnow() - LEASE),
                )
            ).scalars()
        )
    done = 0
    for vehicle_id in ids:
        if run_vehicle(vehicle_id).get("rounds"):
            done += 1
    return {"vehicles": done}


def run_vehicle(vehicle_id: uuid.UUID) -> dict[str, Any]:
    """Gör beställda omräkningar för fordonet. Hoppar över om en annan körning pågår."""
    rounds = 0
    while rounds < MAX_ROUNDS:
        with session_scope() as db:
            seq = _claim(db, vehicle_id)
        if seq is None:
            break
        try:
            _recompute(vehicle_id)
        except Exception:
            with session_scope() as db:
                _release(db, vehicle_id, done=None)
            raise
        with session_scope() as db:
            _release(db, vehicle_id, done=seq)
        rounds += 1
    return {"rounds": rounds}


def _claim(db: Session, vehicle_id: uuid.UUID) -> int | None:
    now = utcnow()
    claimed = db.execute(
        update(Vehicle)
        .where(
            Vehicle.id == vehicle_id,
            Vehicle.recompute_requested > Vehicle.recompute_done,
            or_(Vehicle.recompute_started_at.is_(None), Vehicle.recompute_started_at < now - LEASE),
        )
        .values(recompute_started_at=now, updated_at=Vehicle.updated_at)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        return None
    return db.execute(select(Vehicle.recompute_requested).where(Vehicle.id == vehicle_id)).scalar_one()


def _release(db: Session, vehicle_id: uuid.UUID, *, done: int | None) -> None:
    values: dict[str, Any] = {"recompute_started_at": None, "updated_at": Vehicle.updated_at}
    if done is not None:
        values["recompute_done"] = done
    db.execute(update(Vehicle).where(Vehicle.id == vehicle_id).values(**values))


@contextmanager
def _step(vehicle_id: uuid.UUID, trip_ids: list[uuid.UUID] | None = None) -> Iterator[Session]:
    """En kort transaktion som först låser det den ska skriva.

    SQLite: en skrivning direkt tar databasens skrivlås, så att allt som läses i steget är
    aktuellt och ingen annan hinner skriva emellan. PostgreSQL: stegets resor (eller alla
    fordonets aktiva resor) låses; ett samtidigt beslut väntar tills steget är klart och
    skrivs sedan ovanpå.
    """
    with session_scope() as db:
        sqlite = db.get_bind().dialect.name == "sqlite"
        if sqlite:
            db.execute(
                update(Vehicle)
                .where(Vehicle.id == vehicle_id)
                .values(recompute_started_at=Vehicle.recompute_started_at, updated_at=Vehicle.updated_at)
            )
        else:
            q = select(LogicalTrip.id).where(LogicalTrip.vehicle_id == vehicle_id)
            q = q.where(LogicalTrip.id.in_(trip_ids)) if trip_ids is not None else q.where(
                LogicalTrip.state == LogicalState.ACTIVE)
            db.execute(q.order_by(LogicalTrip.id).with_for_update()).all()
        yield db
    if sqlite and not _is_sqlite_memory(str(db.get_bind().url)):
        # SQLite-fil: ett anrop som väntar på skrivlåset sover i upp till 100 ms åt gången mellan
        # försöken. Utan paus skulle nästa steg hinna ta låset först varje gång.
        time.sleep(SQLITE_PAUSE_SECONDS)


def _active_ids(vehicle_id: uuid.UUID) -> list[uuid.UUID]:
    from app.services.trips import segments as seg

    with session_scope() as db:
        q = seg.active_trips_query(vehicle_id).with_only_columns(LogicalTrip.id)
        return list(db.execute(q).scalars())


def _load(db: Session, ids: list[uuid.UUID]) -> list[LogicalTrip]:
    rows = db.execute(
        select(LogicalTrip)
        .where(LogicalTrip.id.in_(ids), LogicalTrip.state == LogicalState.ACTIVE)
        .options(selectinload(LogicalTrip.segments).selectinload(TripSegment.raw_trip))
    ).scalars()
    by_id = {t.id: t for t in rows}
    return [by_id[i] for i in ids if i in by_id]  # samma ordning som feedback.after_decisions


def _chunks(ids: list[uuid.UUID]) -> Iterator[list[uuid.UUID]]:
    for i in range(0, len(ids), CHUNK):
        yield ids[i : i + CHUNK]


def _recompute(vehicle_id: uuid.UUID) -> None:
    """Samma steg som feedback.after_decisions, i korta transaktioner, följt av luckorna."""
    from app.services import settings as settings_svc
    from app.services.classify import autopilot
    from app.services.learning import feedback, patterns
    from app.services.trips import gaps, merge, pipeline, review

    with _step(vehicle_id, []) as db:
        vehicle = db.get(Vehicle, vehicle_id)
        if vehicle is None:
            return
        user_id = vehicle.owner_user_id
        patterns.rebuild(db, user_id)

    for ids in _chunks(_active_ids(vehicle_id)):
        with _step(vehicle_id, ids) as db:
            feedback.reclassify(db, user_id=user_id, vehicle_id=vehicle_id, trips=_load(db, ids), cache={})

    with _step(vehicle_id) as db:
        vehicle = db.get(Vehicle, vehicle_id)
        if vehicle is not None:
            autopilot.run_vehicle(db, user_id=user_id, vehicle=vehicle, actor=audit.SYSTEM)

    # Kategoriregler för sammanslagning: resor som blivit avgjorda kan slås ihop med grannen
    # av samma kategori; frågorna går till telefonen som i pipelinen (byggkontraktet 2.A.3).
    with _step(vehicle_id) as db:
        vehicle = db.get(Vehicle, vehicle_id)
        if vehicle is not None:
            questions = feedback.merge_after_decisions(db, user_id=user_id, vehicle=vehicle)
            feedback.notify_merge_questions(db, user_id=user_id, vehicle=vehicle, questions=questions)

    scope: str | None = None
    for ids in _chunks(_active_ids(vehicle_id)):
        with _step(vehicle_id, ids) as db:
            scope = scope or settings_svc.journal_scope(db, user_id)
            for trip in _load(db, ids):
                pipeline.sync_trip_review_items(db, trip, scope=scope)

    # Mätarställningen: luckor och överlapp (ärenden som bara var avrundning stängs).
    with _step(vehicle_id, []) as db:
        tol = float(settings_svc.get_section(db, user_id, "merge")["odometer_tolerance_km"])
        gaps.recompute(db, vehicle_id, tol, scope=settings_svc.journal_scope(db, user_id))
        merge.close_stale_overlaps(db, vehicle_id, odometer_tolerance=tol)
        # Ärenden för resor som inte längre är aktiva (t.ex. en borttagen resa).
        review.close_orphaned(db, vehicle_id)
        # Avvikelser för borttagna resor, och för resor som ägaren just avgjort som privata.
        from app.services.stats import anomalies

        anomalies.close_noise(db, vehicle_id, scope=settings_svc.journal_scope(db, user_id))
