"""Gemensam efterbearbetning när resor tillkommit eller ändrats.

Ordningen är: sammanslagningsregler → dataluckor → platsstatistik → mönster →
klassificering av oavgjorda resor → granskningsärenden → ev. notiser.

Luckor i mätarställningen mellan resorna ger aldrig granskningsärenden här – de
räknas bara fram (``gaps.recompute``). I omfånget ``business_only`` är de privat
körning; då skapas inte heller ärendet "misstänkt datalucka" för resor som är helt
privata, eftersom privat körning inte redovisas.
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


def _trip_scope(db: Session, trip: LogicalTrip) -> str:
    vehicle = db.get(Vehicle, trip.vehicle_id)
    if vehicle is None:
        return settings_svc.SCOPE_COMPLETE
    return settings_svc.journal_scope(db, vehicle.owner_user_id)


def sync_trip_review_items(
    db: Session, trip: LogicalTrip, *, by: str = "system", scope: str | None = None
) -> list[str]:
    """Skapar/stänger granskningsärenden utifrån resans aktuella läge. Idempotent.

    ``scope`` är journalens omfång; utelämnas det slås användarens inställning upp vid behov.
    """
    wanted: dict[str, tuple[str, str | None]] = {}
    out_of_scope: set[str] = set()  # ärenden som inte är aktuella i journalens omfång
    if trip.state == LogicalState.ACTIVE:
        approved = trip.review_status in (ReviewStatus.APPROVED, ReviewStatus.IMPORTED)
        if trip.has_mixed_categories and not approved:
            wanted[ReviewKind.MIXED_ALLOCATION] = (
                f"Fördela privat och tjänst: {_route(trip)}",
                "Resan innehåller både privat körning och tjänstekörning. Kontrollera fördelningen per delsträcka.",
            )
        elif trip.tax_note and not approved:
            wanted[ReviewKind.TAX_UNCERTAIN] = (f"Skattemässig bedömning: {_route(trip)}", trip.tax_note)
        elif trip.review_status in (ReviewStatus.NEEDS_REVIEW, ReviewStatus.PROPOSED) or (
            trip.undecided_km > seg.KM_EPS and trip.category != Category.UNDECIDED
        ):
            # Även en resa som ser avgjord ut behöver granskas om delsträckor saknar kategori.
            if trip.category == Category.UNDECIDED or trip.undecided_km > seg.KM_EPS:
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
        # Privat körning redovisas inte när endast tjänsteresor ingår i journalen.
        private_out_of_scope = (
            ("missing_odometer" in flags or "possible_missed_trips" in flags)
            and seg.is_private_only(trip)
            and (scope or _trip_scope(db, trip)) == settings_svc.SCOPE_BUSINESS_ONLY
        )
        if "missing_odometer" in flags:
            if private_out_of_scope:
                out_of_scope.add(ReviewKind.MISSING_ODOMETER)
            else:
                wanted[ReviewKind.MISSING_ODOMETER] = (f"Mätarställning saknas: {_route(trip)}", None)
        if "possible_missed_trips" in flags:
            if private_out_of_scope:
                out_of_scope.add(ReviewKind.DATA_GAP)
            else:
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
                # Ett ärende som stängdes enbart p.g.a. journalens omfång blir aktuellt igen.
                reopen_reason=review.SCOPE_REASON,
            )
            if was_created:
                created.append(kind)
        elif kind in out_of_scope:
            review.resolve_by_key(
                db, trip.vehicle_id, key, by="system", resolution={"auto": True, "reason": review.SCOPE_REASON}
            )
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
    use_llm: bool = True,
) -> dict[str, Any]:
    """Kör hela efterbearbetningen för ett fordon."""
    from app.services.classify import engine as classify_engine
    from app.services.learning import patterns

    merge_cfg = settings_svc.get_section(db, user_id, "merge")
    scope = settings_svc.journal_scope(db, user_id)
    out: dict[str, Any] = {"origin": origin}

    merge_result = merge.evaluate_vehicle(
        db, vehicle_id=vehicle.id, merge_settings=merge_cfg, actor=actor, apply_auto=apply_merge_rules
    )
    out["auto_merged"] = merge_result["auto_merged"]
    out["merge_questions"] = len(merge_result["questions"])

    out.update(gaps.recompute(db, vehicle.id, float(merge_cfg["odometer_tolerance_km"]), scope=scope))
    places.recount_visits(db, user_id)
    patterns.rebuild(db, user_id)
    if origin in ("import", "import_undo"):
        from app.services.places import suggest

        out["location_suggestions"] = suggest.refresh(db, user_id)

    from app.services.calendar import matching as calendar_matching
    from app.services.trips import journeys

    out["calendar_matched"] = calendar_matching.match_vehicle(db, user_id=user_id, vehicle=vehicle)

    classified = 0
    if classify:
        classified = classify_engine.classify_pending(
            db, user_id=user_id, vehicle=vehicle, actor=actor, use_llm=use_llm
        )
    out["classified"] = classified
    out["journeys"] = journeys.detect(db, user_id=user_id, vehicle=vehicle)

    new_trip_items: list[tuple[LogicalTrip, list[str]]] = []
    for trip in seg.active_trips(db, vehicle.id):
        created = sync_trip_review_items(db, trip, scope=scope)
        if created:
            new_trip_items.append((trip, created))
    out["new_review_trips"] = len(new_trip_items)
    out["closed_orphaned_items"] = review.close_orphaned(db, vehicle.id)

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
