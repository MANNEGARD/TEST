"""Dataluckor: kilometer i mätarställningen som saknar redovisad resa.

Luckor beräknas ur de fysiska resorna (RawTrip) och är oberoende av hur resorna
grupperats i journalen – en sammanslagning kan därför aldrig dölja en lucka.
"""

from __future__ import annotations

import uuid
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import DataGap, LogicalTrip, RawTrip, TripSegment, Vehicle
from app.models.enums import (
    Category,
    DecisionSource,
    DistanceQuality,
    GapKind,
    GapStatus,
    LogicalState,
    PositionSource,
    ReviewKind,
    ReviewStatus,
    TimeQuality,
    TripSource,
    ValueQuality,
)
from app.services import audit
from app.services import settings as settings_svc
from app.services.audit import Actor
from app.services.trips import review
from app.services.trips import segments as seg
from app.services.trips.segments import TripError

GAP_FILL_PURPOSE = "Privat körning (sammanfattad, ej detaljredovisad)"


def recompute(
    db: Session, vehicle_id: uuid.UUID, tolerance_km: float = 2.0, *, scope: str | None = None
) -> dict[str, Any]:
    """Räknar om odometerluckor för ett fordon. Idempotent; tidigare beslut bevaras.

    En lucka som intygats som privat körning men inte längre finns (t.ex. för att en
    senare importerad resa ligger i intervallet) får sin sammanfattande privatpost
    makulerad – annars skulle samma mätarsträcka redovisas två gånger. Kvarvarande
    delar av intervallet blir nya luckor. I omfånget ``complete`` skapas även ett
    granskningsärende; med ``business_only`` är luckor privat körning och ger inga ärenden.
    """
    raws = list(
        db.execute(
            select(RawTrip)
            .where(
                RawTrip.vehicle_id == vehicle_id,
                RawTrip.voided_at.is_(None),
                # Sammanfattade luckor är inga fysiska resor; luckan ska finnas kvar med sin status.
                RawTrip.source != TripSource.GAP_FILL,
                RawTrip.odometer_start.is_not(None),
                RawTrip.odometer_end.is_not(None),
            )
            .order_by(RawTrip.odometer_start.asc(), RawTrip.odometer_end.asc())
        ).scalars()
    )
    existing = {
        g.dedupe_key: g
        for g in db.execute(
            select(DataGap).where(DataGap.vehicle_id == vehicle_id, DataGap.kind == GapKind.ODOMETER)
        ).scalars()
    }
    # Avrundning till hela kilometer ger aldrig lucka eller ärende (seg.ODOMETER_ROUNDING_KM).
    slack = seg.odometer_slack(tolerance_km)
    seen: set[str] = set()
    # Luckor som bara försvunnit för att de ryms i avrundningen (de fanns med en lägre tolerans).
    rounding_only: set[str] = set()
    flagged: set[str] = set()
    overlaps = 0
    # Ärendet "Motstridiga mätarställningar" skrivs med samma innehåll som avvikelsesökningen
    # (samma nyckel): typ odometer_backwards, talen och klartexten med vad ägaren gör.
    from app.services.stats import anomalies

    vehicle = db.get(Vehicle, vehicle_id)
    tz = settings_svc.user_timezone(db, vehicle.owner_user_id) if vehicle is not None else None
    # Varje resa jämförs med föregående resa – eller med den resa som hittills når längst i
    # mätarställning, om den slutar närmare där resan börjar. En resa som ligger inuti en annan
    # ger då ett överlapp men ingen falsk lucka mot nästa resa, medan en enda felskriven
    # slutställning bara ger ett ärende och inte döljer senare luckor.
    prev: RawTrip | None = None
    reach: RawTrip | None = None
    for b in raws:
        a = prev
        if a is not None and reach is not None and reach is not a and abs(
            b.odometer_start - reach.odometer_end  # type: ignore[operator]
        ) < abs(b.odometer_start - a.odometer_end):  # type: ignore[operator]
            a = reach
        prev = b
        if reach is None or b.odometer_end > reach.odometer_end:  # type: ignore[operator]
            reach = b
        if a is None:
            continue
        diff = b.odometer_start - a.odometer_end  # type: ignore[operator]
        if diff > slack:
            key = f"odo:{a.odometer_end:.0f}:{b.odometer_start:.0f}"
            seen.add(key)
            gap = existing.get(key)
            if gap is None:
                gap = DataGap(vehicle_id=vehicle_id, kind=GapKind.ODOMETER, dedupe_key=key)
                db.add(gap)
            gap.odometer_start, gap.odometer_end = a.odometer_end, b.odometer_start
            gap.km = round(diff, 1)
            gap.from_time = a.ended_at
            gap.to_time = b.started_at or b.start_earliest
            gap.before_raw_trip_id, gap.after_raw_trip_id = a.id, b.id
            gap.is_current = True
        elif diff > tolerance_km:
            rounding_only.add(f"odo:{a.odometer_end:.0f}:{b.odometer_start:.0f}")
        elif diff < -slack:
            overlaps += 1
            key = f"odo-overlap:{a.id}:{b.id}"
            flagged.add(key)
            title, detail, data = anomalies.odometer_overlap_content(a, b, tz or ZoneInfo("UTC"))
            review.upsert(
                db, vehicle_id=vehicle_id, kind=ReviewKind.ANOMALY, dedupe_key=key, title=title, detail=detail,
                data=data,
            )
    # Öppna ärenden vars överlapp inte längre finns (t.ex. bara avrundning) stängs av sig själva.
    # Äldre ärenden (typ odometer_overlap, före 1.3.2) som fortfarande gäller har skrivits om ovan.
    review.close_stale(
        db, vehicle_id, prefixes=("odo-overlap:",), keep=flagged, data_type="odometer_overlap",
        note=review.OVERLAP_GONE_NOTE,
    )
    # Ett par som bara sökningen hittar (i tidsordning) stängs bara när överlappet är borta.
    review.close_stale(
        db, vehicle_id, prefixes=("odo-overlap:", "anomaly:odo_order:"), keep=flagged,
        data_type="odometer_backwards", note=review.OVERLAP_GONE_NOTE,
        when=lambda item: anomalies.odometer_overlap_gone(db, item, slack),
    )
    for key, gap in existing.items():
        if key in rounding_only and gap.status != GapStatus.OPEN and gap.is_current:
            # Ägaren har redan hanterat luckan (t.ex. intygat den som privat körning med en lägre
            # tolerans före 1.2.2). Den ryms i avrundningen men ingenting har ändrats: beslutet
            # och privatposten står kvar.
            continue
        if key not in seen:
            gap.is_current = False
            if gap.status == GapStatus.RESOLVED_PRIVATE and gap.resolved_raw_trip_id is not None:
                km = gap.km
                reopen(db, gap, actor=audit.SYSTEM, reason="Luckan har ändrats av nya resor")
                if scope != settings_svc.SCOPE_BUSINESS_ONLY:
                    review.upsert(
                        db,
                        vehicle_id=vehicle_id,
                        kind=ReviewKind.ANOMALY,
                        dedupe_key=f"gap-fill-stale:{gap.id}",
                        title="Intygad privat körning har ändrats",
                        detail=(
                            f"Luckan {gap.odometer_start:.0f}–{gap.odometer_end:.0f} km ({km:.0f} km) som intygats "
                            "som privat körning innehåller nu andra resor. Den sammanfattande privatposten har "
                            "makulerats så att ingen sträcka räknas två gånger. Hantera de nya luckorna."
                        ),
                        data={"type": "gap_fill_stale", "gap_id": str(gap.id)},
                    )
    db.flush()
    current = [g for g in db.execute(
        select(DataGap).where(DataGap.vehicle_id == vehicle_id, DataGap.is_current.is_(True))
    ).scalars()]
    open_gaps = [g for g in current if g.status == GapStatus.OPEN]
    return {
        "gaps": len(current),
        "open_gaps": len(open_gaps),
        "open_gap_km": round(sum(g.km for g in open_gaps), 1),
        "odometer_overlaps": overlaps,
    }


def list_gaps(db: Session, vehicle_id: uuid.UUID, *, only_open: bool = False) -> list[DataGap]:
    q = select(DataGap).where(DataGap.vehicle_id == vehicle_id, DataGap.is_current.is_(True))
    if only_open:
        q = q.where(DataGap.status == GapStatus.OPEN)
    return list(db.execute(q.order_by(DataGap.odometer_start.asc().nulls_last())).scalars())


def resolve_as_private(db: Session, gap: DataGap, *, actor: Actor, note: str | None = None) -> LogicalTrip:
    """Användaren intygar att luckan var privat körning. En sammanfattande privat post
    skapas så att mätarställningarna går att stämma av. Starttid hittas inte på."""
    if gap.status != GapStatus.OPEN:
        raise TripError("Luckan är redan hanterad.", "gap_not_open")
    if gap.kind != GapKind.ODOMETER or gap.odometer_start is None or gap.odometer_end is None:
        raise TripError("Endast odometerluckor kan sammanfattas som privat körning.", "gap_kind")
    vehicle = db.get(Vehicle, gap.vehicle_id)
    assert vehicle is not None
    raw = RawTrip(
        vehicle_id=gap.vehicle_id,
        source=TripSource.GAP_FILL,
        source_key=f"gapfill:{gap.dedupe_key}",
        started_at=None,
        ended_at=None,
        start_time_quality=TimeQuality.BOUNDED,
        end_time_quality=TimeQuality.BOUNDED,
        start_earliest=gap.from_time,
        start_latest=gap.to_time,
        odometer_start=gap.odometer_start,
        odometer_end=gap.odometer_end,
        odometer_source="derived",
        distance_km=gap.km,
        distance_quality=DistanceQuality.ODOMETER,
        position_source=PositionSource.NONE,
        fuel_quality=ValueQuality.UNKNOWN,
        energy_quality=ValueQuality.UNKNOWN,
        is_complete=False,
        quality_flags=["summary_of_gap"],
        updated_by=actor.label,
    )
    raw.sort_time = gap.from_time or gap.to_time
    db.add(raw)
    db.flush()
    trip = seg.create_logical_for_raw(
        db,
        raw,
        category=Category.PRIVATE,
        category_source=DecisionSource.USER,
        purpose=GAP_FILL_PURPOSE,
        purpose_source=DecisionSource.USER,
        purpose_confirmed=True,
        review_status=ReviewStatus.APPROVED,
        actor=actor.label,
    )
    trip.notes = note
    gap.status = GapStatus.RESOLVED_PRIVATE
    gap.resolution_note = note
    gap.resolved_raw_trip_id = raw.id
    gap.resolved_by = actor.label
    gap.resolved_at = utcnow()
    audit.record(db, actor, "gap.resolve_private", "data_gap", gap.id, after={"km": gap.km, "trip_id": str(trip.id)})
    db.flush()
    return trip


def ignore(db: Session, gap: DataGap, *, actor: Actor, note: str | None = None) -> DataGap:
    """Markerar luckan som medvetet lämnad. Den redovisas fortsatt som oavstämd i exporten."""
    if gap.status != GapStatus.OPEN:
        raise TripError("Luckan är redan hanterad.", "gap_not_open")
    gap.status = GapStatus.IGNORED
    gap.resolution_note = note
    gap.resolved_by = actor.label
    gap.resolved_at = utcnow()
    audit.record(db, actor, "gap.ignore", "data_gap", gap.id, after={"km": gap.km})
    db.flush()
    return gap


def reopen(db: Session, gap: DataGap, *, actor: Actor, reason: str = "Lucka återöppnad") -> DataGap:
    """Ångrar hanteringen av en lucka; en ev. sammanfattande privatpost makuleras."""
    if gap.status == GapStatus.OPEN:
        return gap
    if gap.resolved_raw_trip_id is not None:
        raw = db.get(RawTrip, gap.resolved_raw_trip_id)
        if raw is not None and raw.voided_at is None:
            raw.voided_at = utcnow()
            raw.void_reason = reason
            for trip in db.execute(
                select(LogicalTrip)
                .join(TripSegment, TripSegment.logical_trip_id == LogicalTrip.id)
                .where(LogicalTrip.state == LogicalState.ACTIVE, TripSegment.raw_trip_id == raw.id)
            ).scalars().unique():
                trip.state = LogicalState.VOIDED
                trip.updated_by = actor.label
    gap.status = GapStatus.OPEN
    gap.resolution_note = None
    gap.resolved_raw_trip_id = None
    gap.resolved_by = None
    gap.resolved_at = None
    audit.record(db, actor, "gap.reopen", "data_gap", gap.id)
    db.flush()
    return gap
