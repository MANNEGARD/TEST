"""Ta bort importerade och manuellt inlagda resor (1.3.0).

Borttagning är makulering med spår, inte radering: originalposten (RawTrip) får
``voided_at`` och skälet ``VOID_REASON_DELETED``, journalresan blir ``voided`` och
borttagningen står i ändringshistoriken (audit). Resor från bilen kan inte tas bort.

Det här görs i anropet (byggkontraktet 4.A.2): spärrarna, makuleringen, resans egna ärenden
och frågor, kalenderträffarna och inlärningen. Resten – luckor och kilometer, mönster,
förslag för andra resor och ärenden som inte längre gäller – räknas om i bakgrunden efter
svaret (``learning.recompute``), som efter ett beslut i Granskning.

Återimport: importen jämför bara mot resor som inte är makulerade, så en borttagen
importerad resa kommer tillbaka om samma fil (eller en annan fil med samma resa) importeras
igen – som den står i filen. Den bedöms på nytt; det tidigare beslutet kommer inte tillbaka.
"""

from __future__ import annotations

import logging
import uuid
from typing import Literal

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import (
    BusinessJourney,
    CalendarMatch,
    ImportedFile,
    ImportRow,
    LearningFeedback,
    LogicalTrip,
    ReviewItem,
)
from app.models.enums import LogicalState, ReviewKind, ReviewState, TripSource
from app.services import audit
from app.services.audit import Actor
from app.services.learning import recompute
from app.services.trips import review
from app.services.trips import segments as seg
from app.services.trips.segments import TripError

logger = logging.getLogger(__name__)

VOID_REASON_DELETED = "Borttagen av användaren"

TEXT_NOT_DELETABLE = (
    "Resor från bilen kan inte tas bort. Bara importerade och manuellt inlagda resor kan tas bort."
)
TEXT_MERGED = (
    "Resan är sammanslagen av flera körningar. Dela upp den först under Sammanslagning på resan, "
    "och ta sedan bort den del du vill."
)
TEXT_NOT_ACTIVE = "Resan finns inte längre i körjournalen."

# Resans egna ärenden, som stängs när resan tas bort.
_TRIP_KINDS = (
    ReviewKind.CATEGORY,
    ReviewKind.PURPOSE,
    ReviewKind.TAX_UNCERTAIN,
    ReviewKind.MIXED_ALLOCATION,
    ReviewKind.MISSING_ODOMETER,
    ReviewKind.DATA_GAP,
    ReviewKind.CALENDAR_CONFLICT,
    ReviewKind.IMPORT_PROBLEM,
)
_OPEN = (ReviewState.OPEN, ReviewState.SNOOZED)


def deletable_kind(trip: LogicalTrip) -> Literal["imported", "manual"] | None:
    """Vilken sorts resa det är, sett till källan: ``imported`` (alla delresor kommer från en
    import), ``manual`` (manuellt inlagd) eller None (resa från bilen, sammanfattad lucka eller
    en blandning). Spärrarna (sammanslagen, tjänsteresa, inte aktiv) prövas i ``delete_trip``."""
    raws = seg.ordered_raws(trip)
    if not raws:
        return None
    if all(r.import_batch_id is not None for r in raws):
        return "imported"
    if all(r.source == TripSource.MANUAL and r.import_batch_id is None for r in raws):
        return "manual"
    return None


def _check(db: Session, trip: LogicalTrip) -> Literal["imported", "manual"]:
    if trip.state != LogicalState.ACTIVE:
        raise TripError(TEXT_NOT_ACTIVE, "not_active")
    kind = deletable_kind(trip)
    if kind is None:
        raise TripError(TEXT_NOT_DELETABLE, "not_deletable")
    if len(seg.ordered_raws(trip)) != 1:
        raise TripError(TEXT_MERGED, "merged_trip")
    if trip.journey_id is not None:
        journey = db.get(BusinessJourney, trip.journey_id)
        name = journey.name if journey is not None and journey.name else "tjänsteresan"
        raise TripError(
            f"Resan ingår i tjänsteresan ”{name}”. Lös upp tjänsteresan först (på resan under "
            "Tjänsteresa), och ta sedan bort resan.",
            "in_journey",
        )
    return kind


def _origin(db: Session, raw) -> tuple[str | None, int | None]:
    if raw.import_row_id is None:
        return None, None
    row = db.get(ImportRow, raw.import_row_id)
    if row is None:
        return None, None
    f = db.get(ImportedFile, row.file_id)
    return (f.filename if f is not None else None), row.row_number


def _cancel_notifications(db: Session, trip_id: uuid.UUID, merge_items: list[ReviewItem]) -> None:
    try:
        from app.services.ha import notifier

        notifier.cancel_for_trip(db, trip_id, force=True)
        for item in merge_items:
            notifier.cancel_for_review_item(db, item.id)
    except Exception:  # notiser får aldrig stoppa en ändring i journalen
        logger.exception("Kunde inte avbryta frågor om en borttagen resa")


def delete_trip(db: Session, trip: LogicalTrip, *, actor: Actor) -> None:
    """Tar bort (makulerar) en importerad eller manuellt inlagd resa. Kastar TripError."""
    kind = _check(db, trip)
    raw = seg.ordered_raws(trip)[0]
    # Beställ omräkningen innan resan ändras (samma låsordning som insamlingen).
    recompute.request(db, trip.vehicle_id)
    now = utcnow()
    filename, row_number = _origin(db, raw)
    before = {
        "started_at": trip.started_at.isoformat() if trip.started_at else None,
        "ended_at": trip.ended_at.isoformat() if trip.ended_at else None,
        "distance_km": trip.distance_km,
        "category": trip.category,
        "purpose": trip.purpose,
        "start_address": trip.start_address,
        "end_address": trip.end_address,
        "import_batch_id": str(raw.import_batch_id) if raw.import_batch_id else None,
        "file": filename,
        "row_number": row_number,
    }

    raw.voided_at = now
    raw.void_reason = VOID_REASON_DELETED
    raw.updated_by = actor.label
    trip.state = LogicalState.VOIDED
    trip.updated_by = actor.label

    # Resans egna ärenden, och frågor om att slå ihop den med grannen.
    review.resolve_for_trip(db, trip.id, _TRIP_KINDS, by=actor.label, resolution={"deleted": True})
    merge_items = [
        item
        for item in db.execute(
            select(ReviewItem).where(
                ReviewItem.vehicle_id == trip.vehicle_id,
                ReviewItem.kind == ReviewKind.MERGE,
                ReviewItem.status.in_(_OPEN),
            )
        ).scalars()
        if str(raw.id) in ((item.data or {}).get("first_raw_trip_id"), (item.data or {}).get("second_raw_trip_id"))
    ]
    for item in merge_items:
        review.resolve(db, item, by=actor.label, resolution={"deleted": True})
    _cancel_notifications(db, trip.id, merge_items)

    # Kalenderträffar är härledda: bokningen kan matchas mot andra resor.
    db.execute(delete(CalendarMatch).where(CalendarMatch.logical_trip_id == trip.id))
    # Inlärningen: resans beslut styr inte längre framtida förslag. Egna regler finns kvar.
    db.execute(
        update(LearningFeedback)
        .where(LearningFeedback.logical_trip_id == trip.id, LearningFeedback.undone_at.is_(None))
        .values(undone_at=now)
    )
    audit.record(
        db, actor, "trip.import_delete" if kind == "imported" else "trip.manual_delete", "logical_trip", trip.id,
        before=before,
    )
    db.flush()


def delete_trips(
    db: Session, trips: list[LogicalTrip], *, user_id: uuid.UUID, actor: Actor
) -> tuple[int, list[dict[str, str]]]:
    """Tar bort flera resor (som anroparen redan kontrollerat tillhör ``user_id``). Varje resa
    prövas för sig; de som inte går ger ett fel var.

    Omräkningen (luckor, mönster, förslag, ärenden) beställs en gång per fordon.
    """
    for vehicle_id in sorted({t.vehicle_id for t in trips}):
        recompute.request(db, vehicle_id)  # före resorna (samma låsordning som insamlingen)
    deleted = 0
    errors: list[dict[str, str]] = []
    for trip in sorted(trips, key=lambda t: (t.sort_time is None, t.sort_time or utcnow())):
        try:
            delete_trip(db, trip, actor=actor)
            deleted += 1
        except TripError as exc:
            errors.append({"trip_id": str(trip.id), "code": exc.code, "message": exc.message})
    return deleted, errors


__all__ = ["VOID_REASON_DELETED", "deletable_kind", "delete_trip", "delete_trips"]
