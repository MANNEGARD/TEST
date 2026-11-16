"""Gemensam efterbearbetning när resor tillkommit eller ändrats.

Ordningen med kategoriregler för sammanslagning (``merge.by_category``, standard från
1.3.0) är: dataluckor → platsstatistik → mönster → kalender → klassificering av oavgjorda
resor → resekedjor → autopiloten (självgående läge) → sammanslagningsregler →
granskningsärenden → ev. notiser. Sammanslagningen kommer efter klassningen eftersom bara
avgjorda resor av samma kategori slås ihop. Med det tidigare regelverket kommer
sammanslagningsreglerna först, som före 1.3.0.

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
from app.models.enums import ApprovalSource, Category, LogicalState, ReviewKind, ReviewStatus
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


_OWNER_APPROVAL_SOURCES = (None, ApprovalSource.USER, ApprovalSource.BULK)


def _private_by_owner(trip: LogicalTrip) -> bool:
    """Är den privata kategorin ägarens eget beslut eller källans (importen)?"""
    if trip.review_status == ReviewStatus.IMPORTED:
        return True
    return trip.review_status == ReviewStatus.APPROVED and trip.approval_source in _OWNER_APPROVAL_SOURCES


def _data_gap_texts(db: Session, trip: LogicalTrip) -> tuple[str, str]:
    """Rubrik och text för DATA_GAP. Ett uppehåll i hämtningen säger när och hur långt."""
    from app.services.volvo import detection

    flags = set(trip.quality_flags or [])
    if "collection_gap" in flags:
        gap = detection.collection_gap_of(trip)
        vehicle = db.get(Vehicle, trip.vehicle_id)
        tz = settings_svc.user_timezone(db, vehicle.owner_user_id) if vehicle is not None else None
        return (
            f"Uppehåll i hämtningen: {_route(trip)}",
            detection.collection_gap_text(gap, tz, form="long"),
        )
    return (
        f"Misstänkt datalucka: {_route(trip)}",
        "Mätarställningen visar längre körsträcka än vad positionerna förklarar. "
        "Flera körningar kan ha skett. Komplettera gärna med Volvos körjournal.",
    )


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
            # En resa som kan vara flera körningar blir bara privat körning utan ärende när
            # ägaren själv (eller källan) har sagt privat – aldrig av ett förslag eller ett
            # automatiskt godkännande (byggkontraktet 3.A.4, F1).
            if private_out_of_scope and _private_by_owner(trip):
                out_of_scope.add(ReviewKind.DATA_GAP)
            else:
                wanted[ReviewKind.DATA_GAP] = _data_gap_texts(db, trip)
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

    by_category = merge.uses_category_rules(merge_cfg)
    merge_result: dict[str, Any] = {"auto_merged": 0, "questions": []}
    if not by_category:
        merge_result = merge.evaluate_vehicle(
            db, vehicle_id=vehicle.id, merge_settings=merge_cfg, actor=actor, apply_auto=apply_merge_rules
        )

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

    # Självgående läge: autopiloten avgör live-resorna efter resekedjorna (så att T ser
    # utresan) och före granskningsärendena (byggkontraktet C.3).
    from app.services.classify import autopilot

    self_driving = autopilot.is_active(db, user_id)
    if self_driving:
        out["autopilot"] = autopilot.run_vehicle(db, user_id=user_id, vehicle=vehicle, actor=actor)
        if out["autopilot"].get("approved"):
            out["journeys"] += journeys.detect(db, user_id=user_id, vehicle=vehicle)

    if by_category:
        # Kategoriregler: först nu är resorna avgjorda (av ägaren, en regel eller importen).
        merge_result = merge.evaluate_vehicle(
            db, vehicle_id=vehicle.id, merge_settings=merge_cfg, actor=actor, apply_auto=apply_merge_rules
        )
    out["auto_merged"] = merge_result["auto_merged"]
    out["merge_questions"] = len(merge_result["questions"])

    new_trip_items: list[tuple[LogicalTrip, list[str]]] = []
    for trip in seg.active_trips(db, vehicle.id):
        created = sync_trip_review_items(db, trip, scope=scope)
        if created:
            new_trip_items.append((trip, created))
    out["new_review_trips"] = len(new_trip_items)
    out["closed_orphaned_items"] = review.close_orphaned(db, vehicle.id)

    if notify:
        from app.services.ha import notifier

        if self_driving:
            # Live-resornas frågor ställs en i taget av notifier.ensure_open_questions.
            new_trip_items = [(t, kinds) for t, kinds in new_trip_items if not autopilot.is_live(t)]
        notifier.enqueue_for_changes(
            db,
            user_id=user_id,
            vehicle=vehicle,
            merge_items=merge_result["questions"],
            trip_items=new_trip_items,
        )
    db.flush()
    return out
