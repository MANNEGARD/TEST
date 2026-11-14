"""Gemensam efterbearbetning när resor tillkommit eller ändrats.

Ordningen är: sammanslagningsregler → dataluckor → platsstatistik → mönster →
klassificering av oavgjorda resor → granskningsärenden → ev. notiser.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.models import LogicalTrip, Vehicle
from app.models.enums import Category, LogicalState, ReviewKind, ReviewStatus
from app.services import settings as settings_svc
from app.services.audit import Actor
from app.services.places import registry as places
from app.services.trips import gaps, merge, review
from app.services.trips import segments as seg

_TRIP_KINDS = (
    ReviewKind.CATEGORY,
    ReviewKind.PURPOSE,
    ReviewKind.TAX_UNCERTAIN,
    ReviewKind.MIXED_ALLOCATION,
    ReviewKind.MISSING_ODOMETER,
    ReviewKind.DATA_GAP,
)


def _route(trip: LogicalTrip) -> str:
    return f"{trip.start_address or 'Okänd start'} → {trip.end_address or 'okänt mål'}"


def sync_trip_review_items(db: Session, trip: LogicalTrip, *, by: str = "system") -> list[str]:
    """Skapar/stänger granskningsärenden utifrån resans aktuella läge. Idempotent."""
    wanted: dict[str, tuple[str, str | None]] = {}
    if trip.state == LogicalState.ACTIVE:
        approved = trip.review_status in (ReviewStatus.APPROVED, ReviewStatus.IMPORTED)
        if trip.has_mixed_categories and not approved:
            wanted[ReviewKind.MIXED_ALLOCATION] = (
                f"Fördela privat och tjänst: {_route(trip)}",
                "Resan innehåller både privat körning och tjänstekörning. Kontrollera fördelningen per delsträcka.",
            )
        elif trip.tax_note and not approved:
            wanted[ReviewKind.TAX_UNCERTAIN] = (f"Skattemässig bedömning: {_route(trip)}", trip.tax_note)
        elif trip.review_status in (ReviewStatus.NEEDS_REVIEW, ReviewStatus.PROPOSED):
            if trip.category == Category.UNDECIDED:
                title = f"Arbete eller privat? {_route(trip)}"
            else:
                title = f"Godkänn förslag: {_route(trip)}"
            wanted[ReviewKind.CATEGORY] = (title, None)
        if trip.work_km > 0 and (not trip.purpose or not trip.purpose.strip() or not trip.purpose_confirmed):
            if ReviewKind.CATEGORY not in wanted or trip.category == Category.WORK:
                wanted[ReviewKind.PURPOSE] = (
                    f"Syfte saknas eller behöver bekräftas: {_route(trip)}",
                    trip.purpose,
                )
        flags = set(trip.quality_flags or [])
        if "missing_odometer" in flags:
            wanted[ReviewKind.MISSING_ODOMETER] = (f"Mätarställning saknas: {_route(trip)}", None)
        if "possible_missed_trips" in flags:
            wanted[ReviewKind.DATA_GAP] = (
                f"Misstänkt datalucka: {_route(trip)}",
                "Mätarställningen visar längre körsträcka än vad positionerna förklarar. "
                "Flera körningar kan ha skett. Komplettera gärna med Volvos körjournal.",
            )
    created: list[str] = []
    for kind in _TRIP_KINDS:
        key = review.trip_key(kind, trip.id)
        if kind in wanted:
            title, detail = wanted[kind]
            _, was_created = review.upsert(
                db,
                vehicle_id=trip.vehicle_id,
                kind=kind,
                dedupe_key=key,
                title=title,
                detail=detail,
                logical_trip_id=trip.id,
                data={"trip_id": str(trip.id)},
                reopen=kind in (ReviewKind.CATEGORY, ReviewKind.PURPOSE, ReviewKind.TAX_UNCERTAIN, ReviewKind.MIXED_ALLOCATION),
            )
            if was_created:
                created.append(kind)
        else:
            review.resolve_by_key(db, trip.vehicle_id, key, by=by, resolution={"auto": True})
    return created


def after_trips_changed(
    db: Session,
    *,
    user_id: uuid.UUID,
    vehicle: Vehicle,
    actor: Actor,
    apply_merge_rules: bool = True,
    notify: bool = True,
    origin: str = "live",
    classify: bool = True,
) -> dict[str, Any]:
    """Kör hela efterbearbetningen för ett fordon."""
    from app.services.classify import engine as classify_engine
    from app.services.learning import patterns

    merge_cfg = settings_svc.get_section(db, user_id, "merge")
    out: dict[str, Any] = {"origin": origin}

    merge_result = merge.evaluate_vehicle(
        db, vehicle_id=vehicle.id, merge_settings=merge_cfg, actor=actor, apply_auto=apply_merge_rules
    )
    out["auto_merged"] = merge_result["auto_merged"]
    out["merge_questions"] = len(merge_result["questions"])

    out.update(gaps.recompute(db, vehicle.id, float(merge_cfg["odometer_tolerance_km"])))
    places.recount_visits(db, user_id)
    patterns.rebuild(db, user_id)

    classified = 0
    if classify:
        classified = classify_engine.classify_pending(db, user_id=user_id, vehicle=vehicle, actor=actor)
    out["classified"] = classified

    new_trip_items: list[tuple[LogicalTrip, list[str]]] = []
    for trip in seg.active_trips(db, vehicle.id):
        created = sync_trip_review_items(db, trip)
        if created:
            new_trip_items.append((trip, created))
    out["new_review_trips"] = len(new_trip_items)

    if notify:
        from app.services.ha import notifier

        notifier.enqueue_for_changes(
            db,
            user_id=user_id,
            vehicle=vehicle,
            merge_items=merge_result["questions"],
            trip_items=new_trip_items,
        )
    db.flush()
    return out
