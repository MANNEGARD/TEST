"""Avvikelsedetektion. Varje fynd blir ett granskningsärende (kind = anomaly).

Sökningen är idempotent: ärendena har stabila nycklar, och ett ärende som användaren
avgjort eller avfärdat öppnas inte igen.

I omfånget ``business_only`` (endast tjänsteresor redovisas) är luckor i mätarställningen
privat körning. Avvikelser som enbart bygger på sådana luckor – stora kilometergap och
"missade resor" mellan journalen och fordonets mätarställning – skapas då inte, och
redan öppna sådana ärenden stängs.

1.3.2: kön innehåller bara det som kan ändra vad som redovisas till arbetsgivaren och som
ägaren kan göra något åt.

* Varje resebundet ärende pekar ut sin resa: ``logical_trip_id`` när det gäller en resa, och
  råresorna i ``data`` (``raw_trip_id`` eller ``first_raw_trip_id``/``second_raw_trip_id``).
  ``trip_refs``/``resolve_trip_ids`` slår upp resan även för äldre ärenden som saknar id.
* Förbrukningen redovisas inte och skapar inga ärenden; öppna sådana ärenden stängs.
* Ingen avvikelse byggs på DriveLogs egna uppskattningar (uppskattad tid eller sträcka).
* När endast tjänsteresor redovisas skapas inga resebundna avvikelser för resor som är
  avgjorda som privata (för två resor räcker det att en är tjänst eller oavgjord).
* Ett ärende vars resa inte längre finns stängs av sig självt.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db import utcnow
from app.models import (
    DataGap,
    IntegrationConnection,
    LogicalTrip,
    RawTrip,
    ReviewItem,
    TripSegment,
    User,
    Vehicle,
    VehicleObservation,
)
from app.models.enums import (
    DistanceQuality,
    GapStatus,
    IntegrationProvider,
    IntegrationStatus,
    LogicalState,
    ReviewKind,
    ReviewState,
    ReviewStatus,
    TimeQuality,
    TripSource,
)
from app.services import integrations
from app.services import settings as settings_svc
from app.services.trips import review
from app.services.trips import segments as seg
from app.services.trips.segments import odometer_slack

LARGE_GAP_KM = 500.0
MAX_KM_PER_DAY = 2500.0
MISSED_TRIP_SETTLE = timedelta(hours=2)

# Körtiden larmar först när källans körtid överstiger tiden mellan källans start och slut med
# minst så här mycket. Körtid, start och slut kommer bara ur importerade körjournaler (Volvo), där
# alla tre står i hela minuter: avrundningen ensam ger upp till ett par minuter, och bilens
# körtidsräknare går även när bilen står påslagen (klimat, laddning, köer vid start). Först vid en
# halvtimme visar klockslagen i underlaget ("08:00–08:20") en resa som tydligt inte stämmer med
# hur länge bilen faktiskt kördes – och det är då ägaren behöver ta ställning.
DURATION_CONFLICT_MINUTES = 30
# Sluttiden före starttiden och resor som överlappar i tid: mer än så här (avrundning till minut).
TIME_OVERLAP_SECONDS = 60

# Tider som kommer från källan (bilen eller den importerade körjournalen) och inte är uträknade.
SOURCE_TIME = (TimeQuality.EXACT, TimeQuality.REPORTED)

# Nyckelprefix för avvikelser som enbart bygger på luckor i mätarställningen.
GAP_ANOMALY_PREFIXES = {"large_gap": "anomaly:large_gap:", "missed_trips": "anomaly:missed:"}

# Resebundna ärenden som bara den här sökningen skapar. Gäller villkoret inte längre (t.ex.
# rättad resa, eller ett fynd som 1.3.2 inte längre larmar för) stängs de av sig själva.
_OWN_TRIP_PREFIXES = (
    "anomaly:odo_negative:",
    "anomaly:odo_mismatch:",
    "anomaly:duplicate:",
    "anomaly:time_negative:",
    "anomaly:duration:",
)

# Orsaker när systemet stänger ett ärende (resolution["reason"]).
CONSUMPTION_REASON = "consumption_not_reported"
CONSUMPTION_NOTE = (
    "Stängt automatiskt: förbrukningen redovisas inte till arbetsgivaren och kräver inget beslut."
)
TRIP_GONE_NOTE = "Stängt automatiskt: resan finns inte längre i journalen (makulerad, ersatt eller borttagen)."
PRIVATE_NOTE = "Stängt automatiskt: resan är privat och redovisas inte när endast tjänsteresor ingår."
NO_LONGER_NOTE = (
    "Stängt automatiskt: uppgifterna motsäger inte längre varandra så mycket att underlaget påverkas "
    "(eller så bygger fyndet på DriveLogs egna uppskattningar)."
)

_DECIDED = (ReviewStatus.APPROVED, ReviewStatus.IMPORTED)


# ----------------------------------------------------------------------------- texter


def sv_number(value: float, decimals: int = 1) -> str:
    """Svensk decimalform: 48.6 → '48,6'."""
    return f"{value:.{decimals}f}".replace(".", ",")


def _km(value: float) -> str:
    """Hela kilometer för mätarställningar: 10010 → '10 010'."""
    return f"{value:,.0f}".replace(",", " ")


def _clock(value: datetime | None, tz: ZoneInfo) -> str:
    return f"{value.astimezone(tz):%H:%M}" if value is not None else "–"


def _when(value: datetime | None, tz: ZoneInfo) -> str:
    return f"{value.astimezone(tz):%Y-%m-%d %H:%M}" if value is not None else "okänd tid"


def _minutes(n: float) -> str:
    n = int(round(n))
    return f"{n} minut" if n == 1 else f"{n} minuter"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def distance_advice(r: RawTrip) -> str:
    """Vad ägaren kan göra åt en sträcka eller mätarställning, efter var resan kommer ifrån.

    En importerad resa ger samma värden vid en ny import, och tiderna från källan går inte att
    ändra i DriveLog – rådet lovar inget som inte går att göra.
    """
    if r.source == TripSource.VOLVO_IMPORT:
        return ("Uppgifterna kommer ur Volvos körjournal, och en ny import ger samma värden. "
                "Stämmer sträckan: välj Avfärda.")
    if r.source == TripSource.MANUAL:
        return ("Du har lagt in resan själv: öppna den och rätta sträckan eller mätarställningen, och "
                "markera sedan ärendet som hanterat. Stämmer sträckan: välj Avfärda.")
    return ("Öppna resan och jämför med kartan. Är sträckan fel: importera Volvos körjournal för dagen "
            "(den ersätter bilens uppgifter om resan) och markera sedan ärendet som hanterat. "
            "Stämmer sträckan: välj Avfärda.")


def time_advice(r: RawTrip) -> str:
    """Vad ägaren kan göra åt klockslag som kommer från källan (se ``distance_advice``)."""
    if r.source == TripSource.MANUAL:
        return ("Du har angett tiderna själv: öppna resan och rätta dem, och markera sedan ärendet som "
                "hanterat. Stämmer de: välj Avfärda.")
    origin = "ur Volvos körjournal" if r.source == TripSource.VOLVO_IMPORT else "från bilen"
    return (f"Klockslagen kommer {origin} och går inte att ändra i DriveLog. "
            "Stämmer resan i övrigt: välj Avfärda.")


def odometer_overlap_content(a: RawTrip, b: RawTrip, tz: ZoneInfo) -> tuple[str, str, dict[str, Any]]:
    """Ärendet "Motstridiga mätarställningar" för två resor: rubrik, klartext och tal.

    Delas av sökningen och ``gaps.recompute``, som skriver till samma nyckel (``odo-overlap:``):
    båda ska ge samma typ, samma tal och samma text, oavsett vem som skrev senast.
    """
    assert a.odometer_end is not None and b.odometer_start is not None
    overlap = a.odometer_end - b.odometer_start
    detail = (
        f"Resan {_when(b.started_at or b.sort_time, tz)} börjar på {_km(b.odometer_start)} km, men resan "
        f"{_when(a.started_at or a.sort_time, tz)} slutade på {_km(a.odometer_end)} km – {_km(overlap)} km "
        "överlapp. Antingen är en mätarställning fel eller så finns samma körning med två gånger. Underlaget "
        "redovisar varje resas körsträcka, så en dubblett räknas två gånger. Öppna resorna och ta bort en "
        "eventuell kopia (det går för importerade och egna resor); stämmer båda: välj Avfärda. "
        "Gör du inget redovisas båda."
    )
    data = {
        "type": "odometer_backwards",
        "first_raw_trip_id": str(a.id),
        "second_raw_trip_id": str(b.id),
        "first_odometer_end": a.odometer_end,
        "second_odometer_start": b.odometer_start,
        "overlap_km": round(overlap, 1),
        "first_started_at": _iso(a.started_at or a.sort_time),
        "second_started_at": _iso(b.started_at or b.sort_time),
    }
    return "Motstridiga mätarställningar", detail, data


def time_overlap_content(a: RawTrip, b: RawTrip, tz: ZoneInfo) -> tuple[str, str, dict[str, Any]]:
    """Ärendet "Överlappande resor" (i tid) för två resor. Delas av sökningen och sammanslagningen,
    som skriver till samma nyckel (``overlap:``)."""
    assert a.ended_at is not None and b.started_at is not None
    overlap_s = (a.ended_at - b.started_at).total_seconds()
    detail = (
        f"Resan {_when(b.started_at, tz)} startar {_minutes(overlap_s / 60)} innan resan före slutade "
        f"({_clock(a.ended_at, tz)}). Två resor kan inte pågå samtidigt: den ena kan vara en kopia av den "
        "andra. Underlaget redovisar båda. Öppna resorna och ta bort en eventuell kopia (det går för "
        "importerade och egna resor). " + time_advice(b) + " Gör du inget redovisas båda."
    )
    data = {
        "type": "overlap",
        "first_raw_trip_id": str(a.id),
        "second_raw_trip_id": str(b.id),
        "first_ended_at": a.ended_at.isoformat(),
        "second_started_at": b.started_at.isoformat(),
        "overlap_minutes": round(overlap_s / 60),
        "first_started_at": _iso(a.started_at or a.sort_time),
    }
    return "Överlappande resor", detail, data


def is_time_overlap(a: RawTrip, b: RawTrip) -> bool:
    """Överlappar resorna i tid enligt källans egna tider (inte DriveLogs uppskattningar)?"""
    if a.ended_at is None or b.started_at is None:
        return False
    if a.end_time_quality not in SOURCE_TIME or b.start_time_quality not in SOURCE_TIME:
        return False
    return (a.ended_at - b.started_at).total_seconds() > TIME_OVERLAP_SECONDS


# ----------------------------------------------------------------------------- resorna bakom ärendet


def trip_refs(item: ReviewItem) -> list[tuple[str, uuid.UUID]]:
    """Resorna som ett avvikelseärende gäller, i ordning: ("raw", id) eller ("trip", id).

    Tom lista när ärendet gäller fordonet och inte en resa (mätarhopp, inaktuella uppgifter …).
    Äldre ärenden (före 1.3.2) saknar ``logical_trip_id`` men har råresorna i ``data``.
    """
    data = item.data or {}

    def as_id(value: Any) -> uuid.UUID | None:
        try:
            return uuid.UUID(str(value)) if value else None
        except ValueError:
            return None

    first, second = as_id(data.get("first_raw_trip_id")), as_id(data.get("second_raw_trip_id"))
    if first and second:
        return [("raw", first), ("raw", second)]
    ids = data.get("raw_trip_ids")
    if isinstance(ids, list) and len(ids) == 2 and all(as_id(i) for i in ids):
        return [("raw", as_id(ids[0])), ("raw", as_id(ids[1]))]  # type: ignore[list-item]
    first_t, second_t = as_id(data.get("first_trip_id")), as_id(data.get("second_trip_id"))
    if first_t and second_t:
        return [("trip", first_t), ("trip", second_t)]
    raw = as_id(data.get("raw_trip_id"))
    if raw:
        return [("raw", raw)]
    if item.logical_trip_id is not None:
        return [("trip", item.logical_trip_id)]
    return []


def active_trips_by_raw(db: Session, raw_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, list[uuid.UUID]]:
    """Råresa → de aktiva journalresor den ingår i (i tidsordning). En fråga.

    En makulerad råresa, eller en som inte ingår i någon aktiv resa, saknas i svaret.
    """
    ids = set(raw_ids)
    if not ids:
        return {}
    out: dict[uuid.UUID, list[uuid.UUID]] = {}
    rows = db.execute(
        select(TripSegment.raw_trip_id, LogicalTrip.id)
        .join(LogicalTrip, LogicalTrip.id == TripSegment.logical_trip_id)
        .join(RawTrip, RawTrip.id == TripSegment.raw_trip_id)
        .where(
            TripSegment.raw_trip_id.in_(ids), LogicalTrip.state == LogicalState.ACTIVE, RawTrip.voided_at.is_(None)
        )
        .order_by(LogicalTrip.sort_time.asc().nulls_last(), TripSegment.seq, TripSegment.part)
    ).all()
    for raw_id, trip_id in rows:
        trips = out.setdefault(raw_id, [])
        if trip_id not in trips:
            trips.append(trip_id)
    return out


def resolve_trip_ids(db: Session, items: Sequence[ReviewItem]) -> dict[uuid.UUID, list[list[uuid.UUID]]]:
    """Ärende → för varje resa det gäller: de aktiva journalresornas id (tom lista = resan finns
    inte längre). Ärenden som inte gäller någon resa saknas. En databasfråga för alla ärenden."""
    refs = {i.id: trip_refs(i) for i in items}
    by_raw = active_trips_by_raw(db, (rid for r in refs.values() for kind, rid in r if kind == "raw"))
    out: dict[uuid.UUID, list[list[uuid.UUID]]] = {}
    for item_id, r in refs.items():
        if r:
            out[item_id] = [by_raw.get(rid, []) if kind == "raw" else [rid] for kind, rid in r]
    return out


def decided_private(trip: LogicalTrip) -> bool:
    """Resan är avgjord som enbart privat (godkänd eller källmärkt) – inte ett förslag."""
    return trip.review_status in _DECIDED and seg.is_private_only(trip)


def odometer_overlap_gone(db: Session, item: ReviewItem, slack: float) -> bool:
    """Ärendet "Motstridiga mätarställningar": motsäger resorna inte längre varandra mer än
    avrundningen, eller är någon av dem makulerad? Används av sökningen och ``gaps.recompute``,
    som hittar paren i var sin ordning (tid respektive mätarställning) – ingen av dem stänger
    ett ärende bara för att den själv inte hittade paret."""
    try:
        first, second = (db.get(RawTrip, uuid.UUID(str(item.data[k]))) for k in ("first_raw_trip_id", "second_raw_trip_id"))
    except (KeyError, TypeError, ValueError):
        return False
    if first is None or second is None or first.voided_at is not None or second.voided_at is not None:
        return True
    if first.odometer_end is None or second.odometer_start is None:
        return True
    return not second.odometer_start < first.odometer_end - slack


# ----------------------------------------------------------------------------- stängning


def _close(db: Session, item: ReviewItem, reason: str, note: str) -> None:
    item.snoozed_until = None
    review.resolve(db, item, by="system", resolution={"auto": True, "reason": reason, "note": note})


def close_noise(db: Session, vehicle_id: uuid.UUID, *, scope: str) -> dict[str, int]:
    """Stänger öppna (och uppskjutna) avvikelser som inte längre hör hemma i kön.

    * Förbrukningsavvikelser (``abnormal_consumption``) – förbrukningen redovisas inte.
    * Avvikelser vars resa inte längre finns (makulerad, ersatt av en import, borttagen).
    * När endast tjänsteresor redovisas: avvikelser som bara gäller resor avgjorda som privata.
      Orsaken är ``journal_scope``, så ärendet öppnas igen om resan blir tjänst.

    Billig nog att köras efter varje omräkning (pipelinen, ägarens beslut) och vid varje sökning.
    Ärenden som ägaren har avgjort eller avfärdat rörs inte.

    Först öppnas ärenden igen som stängdes för att resan var privat, om resan inte längre är det
    (eller om hela journalen redovisas) – så att de syns direkt och inte först efter nattens sökning.
    Ärenden om en resa får dessutom den aktuella resans id (t.ex. efter en ångrad uppdelning).
    """
    reopen_scope_closed(db, vehicle_id, scope=scope)
    items = list(
        db.execute(
            select(ReviewItem).where(
                ReviewItem.vehicle_id == vehicle_id,
                ReviewItem.kind == ReviewKind.ANOMALY,
                ReviewItem.status.in_((ReviewState.OPEN, ReviewState.SNOOZED)),
            )
        ).scalars()
    )
    closed: dict[str, int] = {}

    def count(key: str) -> None:
        closed[key] = closed.get(key, 0) + 1

    rest: list[ReviewItem] = []
    for item in items:
        if (item.data or {}).get("type") == "abnormal_consumption":
            _close(db, item, CONSUMPTION_REASON, CONSUMPTION_NOTE)
            count("abnormal_consumption")
        else:
            rest.append(item)
    resolved = resolve_trip_ids(db, rest)
    trip_ids = {tid for per_item in resolved.values() for ids in per_item for tid in ids}
    trips: dict[uuid.UUID, LogicalTrip] = {}
    if trip_ids:
        trips = {
            t.id: t
            for t in db.execute(
                select(LogicalTrip).where(LogicalTrip.id.in_(trip_ids)).options(selectinload(LogicalTrip.segments))
            ).scalars()
        }
    business_only = scope == settings_svc.SCOPE_BUSINESS_ONLY
    for item in rest:
        per_ref = resolved.get(item.id)
        if not per_ref:
            continue
        active = [[trips[t] for t in ids if t in trips and trips[t].state == LogicalState.ACTIVE] for ids in per_ref]
        if any(not ref for ref in active):
            _close(db, item, review.INACTIVE_REASON, TRIP_GONE_NOTE)
            count("trip_gone")
        elif business_only and all(all(decided_private(t) for t in ref) for ref in active):
            _close(db, item, review.SCOPE_REASON, PRIVATE_NOTE)
            count("private_trip")
        elif len(active) == 1 and trip_refs(item)[0][0] == "raw" and item.logical_trip_id != active[0][0].id:
            item.logical_trip_id = active[0][0].id
    db.flush()
    return closed


def reopen_scope_closed(db: Session, vehicle_id: uuid.UUID, *, scope: str) -> int:
    """Öppnar resebundna avvikelser igen som systemet stängde för att resan var privat
    (``journal_scope``), när någon av resorna inte längre är avgjord privat eller hela journalen
    redovisas. Fordonsavvikelser (luckor, missade resor) rörs inte; de hanteras av sökningen."""
    items = [
        i
        for i in db.execute(
            select(ReviewItem).where(
                ReviewItem.vehicle_id == vehicle_id,
                ReviewItem.kind == ReviewKind.ANOMALY,
                ReviewItem.status == ReviewState.RESOLVED,
                ReviewItem.resolved_by == "system",
            )
        ).scalars()
        if (i.resolution or {}).get("reason") == review.SCOPE_REASON
    ]
    resolved = resolve_trip_ids(db, items)
    if not resolved:
        return 0
    trip_ids = {tid for per_item in resolved.values() for ids in per_item for tid in ids}
    trips = {
        t.id: t
        for t in db.execute(
            select(LogicalTrip).where(LogicalTrip.id.in_(trip_ids)).options(selectinload(LogicalTrip.segments))
        ).scalars()
    } if trip_ids else {}
    business_only = scope == settings_svc.SCOPE_BUSINESS_ONLY
    reopened = 0
    for item in items:
        per_ref = resolved.get(item.id)
        if not per_ref:
            continue
        active = [[trips[t] for t in ids if t in trips and trips[t].state == LogicalState.ACTIVE] for ids in per_ref]
        if any(not ref for ref in active):
            continue  # resan finns inte längre
        if business_only and all(all(decided_private(t) for t in ref) for ref in active):
            continue
        item.status = ReviewState.OPEN
        item.resolved_at = None
        item.resolved_by = None
        item.resolution = {}
        if len(active) == 1:
            item.logical_trip_id = active[0][0].id
        reopened += 1
    if reopened:
        db.flush()
    return reopened


def close_gap_anomalies(db: Session, vehicle: Vehicle) -> dict[str, int]:
    """Stänger öppna (och uppskjutna) avvikelser som enbart bygger på luckor i mätarställningen.

    Används i omfånget ``business_only``. Ärendena stängs med orsaken ``journal_scope`` så
    att de kan öppnas igen om användaren går över till fullständig körjournal.
    """
    closed: dict[str, int] = {}
    for kind, prefix in GAP_ANOMALY_PREFIXES.items():
        items = db.execute(
            select(ReviewItem).where(
                ReviewItem.vehicle_id == vehicle.id,
                ReviewItem.kind == ReviewKind.ANOMALY,
                ReviewItem.status.in_((ReviewState.OPEN, ReviewState.SNOOZED)),
                ReviewItem.dedupe_key.like(f"{prefix}%"),
            )
        ).scalars().all()
        for item in items:
            review.resolve(db, item, by="system", resolution={"auto": True, "reason": review.SCOPE_REASON})
        if items:
            closed[kind] = len(items)
    return closed


def _raws(db: Session, vehicle: Vehicle) -> list[RawTrip]:
    return list(
        db.execute(
            select(RawTrip)
            .where(RawTrip.vehicle_id == vehicle.id, RawTrip.voided_at.is_(None), RawTrip.source != TripSource.GAP_FILL)
            .order_by(RawTrip.sort_time.asc().nulls_last(), RawTrip.odometer_start.asc().nulls_last())
        ).scalars()
    )


def _active_trips(db: Session, vehicle: Vehicle) -> dict[uuid.UUID, list[LogicalTrip]]:
    """Råresa → aktiva journalresor (med delsträckor, för kategorin), i tidsordning."""
    out: dict[uuid.UUID, list[LogicalTrip]] = {}
    for trip in seg.active_trips(db, vehicle.id):
        for s in trip.segments:
            trips = out.setdefault(s.raw_trip_id, [])
            if trip not in trips:
                trips.append(trip)
    return out


# ----------------------------------------------------------------------------- sökningen


def scan_vehicle(db: Session, vehicle: Vehicle, *, user_id: uuid.UUID, now: datetime | None = None) -> dict[str, Any]:
    """Söker efter avvikelser för ett fordon. Returnerar antal nya ärenden per typ."""
    now = now or utcnow()
    scope = settings_svc.journal_scope(db, user_id)
    # Luckor i mätarställningen är privat körning när endast tjänsteresor redovisas.
    gaps_are_expected = scope == settings_svc.SCOPE_BUSINESS_ONLY
    tz = settings_svc.user_timezone(db, user_id)
    # Avrundning till hela kilometer larmar aldrig, oavsett sammanslagningens tolerans.
    tol = odometer_slack(float(settings_svc.get_section(db, user_id, "merge")["odometer_tolerance_km"]))
    volvo_cfg = settings_svc.get_section(db, user_id, "volvo")
    created: dict[str, int] = {}
    found: dict[str, int] = {}

    # Stäng först det som inte längre hör hemma i kön (förbrukning, borttagna och privata resor),
    # så att sökningen nedan inte stänger dem med fel orsak.
    closed = close_noise(db, vehicle.id, scope=scope)

    def hit(kind: str, was_created: bool) -> None:
        found[kind] = found.get(kind, 0) + 1
        if was_created:
            created[kind] = created.get(kind, 0) + 1

    raws = _raws(db, vehicle)
    trips_of = _active_trips(db, vehicle)
    # Nycklar som fortfarande gäller, eller som close_noise äger (borttagen eller privat resa).
    keep: set[str] = set()

    def skip_raw(r: RawTrip) -> bool:
        """Ingen resebunden avvikelse: resan finns inte, eller är privat när bara tjänst redovisas."""
        trips = trips_of.get(r.id)
        return not trips or (gaps_are_expected and all(decided_private(t) for t in trips))

    def skip_pair(a: RawTrip, b: RawTrip) -> bool:
        if not trips_of.get(a.id) or not trips_of.get(b.id):
            return True
        return skip_raw(a) and skip_raw(b)  # det räcker att en av resorna är tjänst eller oavgjord

    def part_of(r: RawTrip) -> dict[str, Any]:
        """Är råresan bara en del av journalresan (sammanslagen eller uppdelad)? Rutan i webben
        visar journalresan, så texten säger då att det gäller en delsträcka och vilken."""
        trips = trips_of.get(r.id) or []
        if not trips:
            return {}
        trip = trips[0]
        if len({s.raw_trip_id for s in trip.segments}) <= 1 and len(trips) <= 1:
            return {}
        return {
            "part_of_trip": True,
            "trip_distance_km": round(trip.distance_km, 1),
            "part_distance_km": round(r.distance_km, 1),
            "part_started_at": _iso(r.started_at),
            "part_ended_at": _iso(r.ended_at),
        }

    def part_text(part: dict[str, Any], r: RawTrip) -> str:
        if not part:
            return ""
        span = f" ({_clock(r.started_at, tz)}–{_clock(r.ended_at, tz)})" if r.started_at and r.ended_at else ""
        return (f"Det gäller en delsträcka av resan{span}, {sv_number(r.distance_km)} km av resans "
                f"{sv_number(part['trip_distance_km'])} km. ")

    def has_own_start(r: RawTrip) -> bool:
        """Har ägaren angett starttiden på journalresan? Då visar underlaget ägarens tid."""
        return any(t.start_time_override is not None for t in trips_of.get(r.id) or [])

    def trip_upsert(kind: str, r: RawTrip, key: str, title: str, detail: str, data: dict[str, Any]) -> None:
        keep.add(key)
        if skip_raw(r):
            return
        trip = trips_of[r.id][0]
        data = {"started_at": _iso(r.started_at or r.sort_time), **data, "raw_trip_id": str(r.id)}
        hit(kind, _upsert(db, vehicle, key, title, detail, data, trip_id=trip.id, reopen_reason=review.SCOPE_REASON))

    def pair_upsert(kind: str, a: RawTrip, b: RawTrip, key: str, title: str, detail: str,
                    data: dict[str, Any], trip_id: uuid.UUID | None = None) -> None:
        keep.add(key)
        if skip_pair(a, b):
            return
        data = {
            "first_started_at": _iso(a.started_at or a.sort_time),
            "second_started_at": _iso(b.started_at or b.sort_time),
            **data,
            "first_raw_trip_id": str(a.id),
            "second_raw_trip_id": str(b.id),
        }
        hit(kind, _upsert(db, vehicle, key, title, detail, data, trip_id=trip_id, reopen_reason=review.SCOPE_REASON))

    # 1. Ovanligt stora kilometergap (inte när luckor är förväntad privat körning)
    large_gaps = [] if gaps_are_expected else db.execute(
        select(DataGap).where(
            DataGap.vehicle_id == vehicle.id, DataGap.is_current.is_(True), DataGap.status == GapStatus.OPEN,
            DataGap.km >= LARGE_GAP_KM,
        )
    ).scalars().all()
    for gap in large_gaps:
        hit(
            "large_gap",
            _upsert(
                db, vehicle, f"{GAP_ANOMALY_PREFIXES['large_gap']}{gap.dedupe_key}",
                f"Ovanligt stort kilometergap: {gap.km:.0f} km",
                f"Mellan mätarställning {gap.odometer_start:.0f} och {gap.odometer_end:.0f} km saknas journalposter.",
                {"type": "large_gap", "gap_id": str(gap.id), "km": gap.km},
                reopen_reason=review.SCOPE_REASON,
            ),
        )

    # 2. Orimliga mätarställningar (underlaget redovisar resans körsträcka, inte mätarens skillnad)
    for r in raws:
        if r.odometer_start is None or r.odometer_end is None:
            continue
        diff = r.odometer_end - r.odometer_start
        part = part_of(r)
        # Det värde som underlaget redovisar: journalresans körsträcka (delsträckornas summa). Är
        # råresan bara en del av resan står delens sträcka i part_distance_km.
        reported_km = part["trip_distance_km"] if part else round(r.distance_km, 1)
        numbers = {
            "odometer_start": r.odometer_start,
            "odometer_end": r.odometer_end,
            "distance_km": r.distance_km,
            "odometer_km": round(diff, 1),
            "reported": "distance_km",
            "reported_km": reported_km,
            **part,
        }
        basis = (
            f"Underlaget redovisar resans körsträcka, {sv_number(reported_km)} km, där delsträckan ingår med "
            f"{sv_number(r.distance_km)} km"
            if part else
            f"Underlaget redovisar körsträckan, {sv_number(r.distance_km)} km"
        )
        if diff < 0:
            trip_upsert(
                "odometer_backwards", r, f"anomaly:odo_negative:{r.id}", "Mätarställningen går bakåt inom en resa",
                part_text(part, r)
                + f"Källan anger att resan börjar på {_km(r.odometer_start)} km och slutar på "
                f"{_km(r.odometer_end)} km, alltså {_km(-diff)} km bakåt. {basis}, men mätarställningarna visas "
                f"som de står. {distance_advice(r)} Gör du inget står mätarställningarna kvar så i underlaget.",
                {"type": "odometer_backwards", **numbers},
            )
        elif r.distance_quality != DistanceQuality.ESTIMATED and abs(diff - r.distance_km) > max(
            3.0, 0.1 * max(r.distance_km, diff)
        ):
            more = diff - r.distance_km
            trip_upsert(
                "odometer_mismatch", r, f"anomaly:odo_mismatch:{r.id}", "Körsträckan stämmer inte med mätarställningen",
                part_text(part, r)
                + f"Resan anger {sv_number(r.distance_km)} km men mätarställningen ändras {_km(diff)} km "
                f"({_km(r.odometer_start)} → {_km(r.odometer_end)} km). {basis}. "
                + (
                    f"Stämmer mätarställningen saknas {sv_number(more)} km i underlaget. "
                    if more > 0 else
                    f"Stämmer mätarställningen redovisas {sv_number(-more)} km för mycket. "
                )
                + f"{distance_advice(r)} Gör du inget redovisas {sv_number(reported_km)} km.",
                {"type": "odometer_mismatch", **numbers, "difference_km": round(more, 1)},
            )
    timed = [r for r in raws if r.sort_time is not None]
    conflicting: set[str] = set()
    for a, b in zip(timed, timed[1:], strict=False):
        if a.odometer_end is not None and b.odometer_start is not None and b.odometer_start < a.odometer_end - tol:
            # Samma nyckel som gaps.recompute använder när ordningen sammanfaller – inga dubbla ärenden.
            if b.odometer_end is not None and a.odometer_start is not None and b.odometer_end <= a.odometer_start + tol:
                key = f"anomaly:odo_order:{a.id}:{b.id}"
            else:
                key = f"odo-overlap:{a.id}:{b.id}"
            conflicting.add(key)
            title, detail, data = odometer_overlap_content(a, b, tz)
            pair_upsert("odometer_backwards", a, b, key, title, detail, data)
    # Tidigare fynd som inte längre gäller (t.ex. bara avrundning) stängs av sig själva. Ett par som
    # bara omräkningen hittar (i mätarordning) stängs bara om överlappet verkligen är borta.
    for data_type in ("odometer_backwards", "odometer_overlap"):
        review.close_stale(
            db, vehicle.id, prefixes=("odo-overlap:", "anomaly:odo_order:"), keep=conflicting,
            data_type=data_type, note=review.OVERLAP_GONE_NOTE,
            when=lambda item: odometer_overlap_gone(db, item, tol),
        )

    # 3. Möjliga dubbletter (samma starttid och sträcka – båda starttiderna från källan)
    for i, a in enumerate(timed):
        for b in timed[i + 1 : i + 6]:
            if a.started_at is None or b.started_at is None:
                continue
            gap_s = abs((b.started_at - a.started_at).total_seconds())
            if gap_s > 120:
                break
            if a.start_time_quality not in SOURCE_TIME or b.start_time_quality not in SOURCE_TIME:
                continue
            if abs(a.distance_km - b.distance_km) <= 0.5:
                # Nyckeln följer id (stabil), "första" och "andra" resan följer starttiden.
                key_ids = sorted((str(a.id), str(b.id)))
                first, second = sorted((a, b), key=lambda r: (r.started_at, str(r.id)))
                ids = [str(first.id), str(second.id)]
                pair_upsert(
                    "possible_duplicate", first, second, f"anomaly:duplicate:{key_ids[0]}:{key_ids[1]}",
                    "Möjlig dubblett",
                    f"Två resor startar {_when(first.started_at, tz)} med nästan samma körsträcka "
                    f"({sv_number(first.distance_km)} km och {sv_number(second.distance_km)} km). Är den ena en "
                    "kopia räknas samma körning två gånger i underlaget. Öppna resorna och ta bort kopian (det går "
                    "för importerade och egna resor); är det två olika resor: välj Avfärda. "
                    "Gör du inget redovisas båda.",
                    {"type": "possible_duplicate", "raw_trip_ids": ids,
                     "first_distance_km": first.distance_km, "second_distance_km": second.distance_km,
                     "start_difference_seconds": int(gap_s)},
                )

    # 4. Motstridiga tidsstämplar – bara när tiderna kommer från källan (inte uträknade)
    for r in raws:
        if r.started_at is None or r.ended_at is None:
            continue
        if r.start_time_quality not in SOURCE_TIME or r.end_time_quality not in SOURCE_TIME:
            continue
        if has_own_start(r):
            continue  # underlaget visar ägarens starttid, inte källans: ärendet stängs nedan
        elapsed = (r.ended_at - r.started_at).total_seconds()
        times = {"started_at": r.started_at.isoformat(), "ended_at": r.ended_at.isoformat()}
        part = part_of(r)
        if elapsed < -TIME_OVERLAP_SECONDS:
            trip_upsert(
                "time_conflict", r, f"anomaly:time_negative:{r.id}", "Sluttiden ligger före starttiden",
                part_text(part, r)
                + f"Källan anger start {_when(r.started_at, tz)} och slut {_clock(r.ended_at, tz)}, "
                f"{_minutes(-elapsed / 60)} före starten. Underlaget visar klockslagen som de står. "
                f"{time_advice(r)} Gör du inget visas tiderna så i underlaget.",
                {"type": "time_conflict", **times, "elapsed_minutes": round(elapsed / 60), **part},
            )
        elif r.duration_seconds is not None and r.duration_seconds - elapsed >= DURATION_CONFLICT_MINUTES * 60:
            driving, span = r.duration_seconds / 60, elapsed / 60
            trip_upsert(
                "time_conflict", r, f"anomaly:duration:{r.id}", "Körtiden är längre än resans längd i tid",
                part_text(part, r)
                + f"Källan anger {_minutes(driving)} körtid, men mellan start {_clock(r.started_at, tz)} och slut "
                f"{_clock(r.ended_at, tz)} går det bara {_minutes(span)}. Underlaget visar klockslagen "
                f"{_clock(r.started_at, tz)}–{_clock(r.ended_at, tz)}, så resan ser {_minutes(driving - span)} "
                f"kortare ut än körtiden. Körtiden står inte i underlaget. {time_advice(r)} "
                "Gör du inget visas klockslagen som de står.",
                {"type": "time_conflict", **times, "driving_minutes": round(driving),
                 "elapsed_minutes": round(span), "difference_minutes": round(driving - span),
                 "limit_minutes": DURATION_CONFLICT_MINUTES, **part},
            )
    for a, b in zip(timed, timed[1:], strict=False):
        if not is_time_overlap(a, b) or has_own_start(b):
            continue
        title, detail, data = time_overlap_content(a, b, tz)
        # Samma innehåll som sammanslagningens fråga om samma par (merge.py), inklusive resornas id.
        trip_a, trip_b = (trips_of.get(a.id) or [None])[0], (trips_of.get(b.id) or [None])[0]
        if trip_a is not None and trip_b is not None:
            data = {**data, "first_trip_id": str(trip_a.id), "second_trip_id": str(trip_b.id)}
        pair_upsert("time_overlap", a, b, f"overlap:{a.id}:{b.id}", title, detail, data,
                    trip_id=trip_b.id if trip_b is not None else None)

    # Egna resebundna fynd som inte längre gäller stängs (resan rättad, eller uppskattade värden).
    # Överlapp i tid delar nyckel och innehåll (time_overlap_content) med sammanslagningens ärende,
    # som skapas på samma villkor. Ärenden i den äldre formen (utan råresorna) stänger merge.py.
    closed_stale = review.close_stale(
        db, vehicle.id, prefixes=_OWN_TRIP_PREFIXES, keep=keep, data_type=None, note=NO_LONGER_NOTE,
    )
    closed_stale += review.close_stale(
        db, vehicle.id, prefixes=("overlap:",), keep=keep, data_type="overlap", note=NO_LONGER_NOTE,
        when=lambda item: "first_raw_trip_id" in (item.data or {}),
    )
    if closed_stale:
        closed["no_longer_applies"] = closed_stale

    # 5. Förbrukningen redovisas inte till arbetsgivaren: inga förbrukningsavvikelser (1.3.2).

    # 6–7. Fordonsrapporter: orimliga hopp, missade resor, avsaknad av nya uppgifter
    observations = list(
        db.execute(
            select(VehicleObservation)
            .where(VehicleObservation.vehicle_id == vehicle.id, VehicleObservation.odometer_km.is_not(None))
            .order_by(VehicleObservation.fetched_at.asc())
        ).scalars()
    )
    for a, b in zip(observations, observations[1:], strict=False):
        delta = b.odometer_km - a.odometer_km  # type: ignore[operator]
        days = max((b.fetched_at - a.fetched_at).total_seconds() / 86400, 1 / 24)
        if delta < -tol:
            hit(
                "odometer_backwards",
                _upsert(
                    db, vehicle, f"anomaly:obs_backwards:{b.id}", "Fordonet rapporterade lägre mätarställning än tidigare",
                    f"Mätarställningen gick från {a.odometer_km:.0f} till {b.odometer_km:.0f} km.",
                    {"type": "odometer_backwards", "observation_id": str(b.id)},
                ),
            )
        elif delta / days > MAX_KM_PER_DAY and delta > 300:
            hit(
                "odometer_jump",
                _upsert(
                    db, vehicle, f"anomaly:obs_jump:{b.id}", "Orimligt hopp i mätarställningen",
                    f"Mätarställningen ökade {delta:.0f} km på {days * 24:.0f} timmar.",
                    {"type": "odometer_jump", "observation_id": str(b.id), "km": delta},
                ),
            )
    if observations and raws and not gaps_are_expected:
        last_obs = observations[-1]
        journal_max = max((r.odometer_end for r in raws if r.odometer_end is not None), default=None)
        seen = last_obs.last_seen_at or last_obs.fetched_at
        if (
            journal_max is not None
            and last_obs.odometer_km - journal_max > max(tol, float(volvo_cfg["min_odometer_delta_km"]))
            and now - last_obs.fetched_at >= MISSED_TRIP_SETTLE
            and now - seen <= timedelta(days=30)
        ):
            km = last_obs.odometer_km - journal_max
            hit(
                "missed_trips",
                _upsert(
                    db, vehicle, f"{GAP_ANOMALY_PREFIXES['missed_trips']}{journal_max:.0f}:{last_obs.odometer_km:.0f}",
                    f"Misstänkt missade resor: {km:.0f} km saknar journalpost",
                    f"Fordonet rapporterar {last_obs.odometer_km:.0f} km men journalen slutar på {journal_max:.0f} km. "
                    "Komplettera med Volvos körjournal eller registrera resorna manuellt.",
                    {"type": "missed_trips", "km": round(km, 1)},
                    reopen_reason=review.SCOPE_REASON,
                ),
            )

    conn: IntegrationConnection | None = None
    if vehicle.volvo_connection_id is not None:
        conn = db.get(IntegrationConnection, vehicle.volvo_connection_id)
    if conn is None and not vehicle.is_demo:
        conn = integrations.get(db, user_id, IntegrationProvider.VOLVO)
    stale_open = [
        i for i in db.execute(
            select(ReviewItem).where(
                ReviewItem.vehicle_id == vehicle.id, ReviewItem.kind == ReviewKind.ANOMALY,
                ReviewItem.status == ReviewState.OPEN, ReviewItem.dedupe_key.like("anomaly:stale:%"),
            )
        ).scalars()
    ]
    is_stale = False
    if conn is not None and conn.status in (IntegrationStatus.CONNECTED, IntegrationStatus.ERROR, IntegrationStatus.DEMO):
        stale_after = timedelta(minutes=float(volvo_cfg["stale_after_minutes"]))
        last_ok = conn.last_success_at
        latest_any = db.execute(
            select(VehicleObservation.last_seen_at)
            .where(VehicleObservation.vehicle_id == vehicle.id)
            .order_by(VehicleObservation.last_seen_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        reference = max([t for t in (last_ok, latest_any) if t is not None], default=None)
        if reference is not None and now - reference > stale_after:
            is_stale = True
            hours = (now - reference).total_seconds() / 3600
            hit(
                "stale_vehicle_data",
                _upsert(
                    db, vehicle, f"anomaly:stale:{reference.date().isoformat()}", "Inga nya fordonsuppgifter",
                    f"Senaste lyckade hämtningen från fordonet är {hours:.0f} timmar gammal. "
                    "Kontrollera Volvo-anslutningen under Integrationer.",
                    {"type": "stale_vehicle_data", "last_success_at": reference.isoformat()},
                ),
            )
    if not is_stale:
        for item in stale_open:  # uppgifterna flödar igen
            review.resolve(db, item, by="system", resolution={"auto": True, "reason": "data_fresh"})

    if gaps_are_expected:
        closed.update(close_gap_anomalies(db, vehicle))

    db.flush()
    return {
        "vehicle_id": str(vehicle.id),
        "scanned_at": now,
        "scope": scope,
        "found": found,
        "created": created,
        "created_total": sum(created.values()),
        "closed": closed,
        "closed_total": sum(closed.values()),
    }


def _upsert(db: Session, vehicle: Vehicle, key: str, title: str, detail: str, data: dict[str, Any],
            trip_id: uuid.UUID | None = None, reopen_reason: str | None = None) -> bool:
    _, created = review.upsert(
        db,
        vehicle_id=vehicle.id,
        kind=ReviewKind.ANOMALY,
        dedupe_key=key,
        title=title,
        detail=detail,
        logical_trip_id=trip_id,
        data=data,
        reopen_reason=reopen_reason,
    )
    return created


def scan_all(db: Session) -> dict[str, int]:
    """Söker igenom alla aktiva användares fordon (nattligt underhåll och första tick efter start)."""
    out = {"vehicles": 0, "created_total": 0, "closed_total": 0}
    for user in db.execute(select(User).where(User.is_active.is_(True))).scalars().all():
        for vehicle in db.execute(select(Vehicle).where(Vehicle.owner_user_id == user.id)).scalars().all():
            result = scan_vehicle(db, vehicle, user_id=user.id)
            out["vehicles"] += 1
            out["created_total"] += int(result["created_total"])
            out["closed_total"] += int(result["closed_total"])
    return out
