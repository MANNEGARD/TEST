"""Månadsskiftet: när månaden är komplett skapas underlaget av sig självt (byggkontraktet C.6).

* ``evaluate`` räknar fram månadens läge, det som väntar (blockerare) och upplysningarna.
  Den skriver ingenting.
* ``run_for`` kontrollerar en månad, sparar läget i ``month_closes`` och skapar underlaget
  (``create_export(origin="month_close")``) när inget väntar.
* ``run_due`` körs i ``tick`` och kontrollerar föregående månad, och de senaste månadernas
  väntande, exporterade och inaktuella underlag, högst en gång i timmen per fordon och
  månad. Bara i självgående läge.

Ett oförändrat avtryck (``rows_fingerprint``) ger aldrig en ny fil. Ändras det efter
exporten blir månaden ``stale``, och vid nästa kontroll ersätts filen av en ny.

Underlaget i förväg (1.3.0): ``run_for(force=True)`` för innevarande månad hoppar bara över
väntan på månadsslutet – övriga kontroller gäller som vanligt. Filen gäller hela
kalendermånaden och visar när den skapades. Vid månadsskiftet kontrolleras månaden igen
som vilken exporterad månad som helst: oförändrat avtryck → ingenting, ändrat → nytt
underlag. Väntar då en fråga om en tjänsteresa (som inte ändrar avtrycket, eftersom bara
redovisade resor ingår) får ägaren den vanliga "väntar"-notisen.
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import (
    ExportJob,
    IntegrationConnection,
    KnownLocation,
    LocationPattern,
    LogicalTrip,
    MonthClose,
    ReviewItem,
    TripPurpose,
    User,
    Vehicle,
    VehicleObservation,
)
from app.models.enums import (
    AUTO_APPROVAL_SOURCES,
    ApprovalSource,
    ExportOrigin,
    GeocodeStatus,
    IntegrationProvider,
    IntegrationStatus,
    MonthCloseState,
    ReviewKind,
    ReviewState,
)
from app.services import audit
from app.services import settings as settings_svc
from app.services.export import checks as checks_mod
from app.services.export import service as export_svc
from app.services.stats import common

logger = logging.getLogger(__name__)

MONTH_GRACE = timedelta(hours=6)
CHECK_INTERVAL = timedelta(hours=1)
ADDRESS_WAIT = timedelta(hours=48)
WAITING_NOTICE_EVERY = timedelta(days=3)
WAITING_NOTICE_MAX = 3
UPDATE_NOTICE_EVERY = timedelta(days=1)
RECHECK_MONTHS = 3  # så långt bakåt run_due följer upp väntande och exporterade månader
# Blockerare som löser sig själva utan ägaren: ger ingen "väntar"-notis för en månad som
# stängts i förväg (filen finns redan; nästa kontroll tar hand om dem).
SELF_RESOLVING = ("waiting_final_fetch", "addresses_pending")

QUESTION_KINDS = (ReviewKind.CATEGORY, ReviewKind.PURPOSE, ReviewKind.TAX_UNCERTAIN, ReviewKind.MIXED_ALLOCATION)
OPEN_ITEM_STATES = (ReviewState.OPEN, ReviewState.SNOOZED)

# Skälen som visas för ägaren (samma ordval som i appen).
SOURCE_LABELS = {
    ApprovalSource.RULE: "Din regel",
    ApprovalSource.HISTORY: "Som de 3 senaste gångerna",
    ApprovalSource.RETURN_TRIP: "Hemresa efter tjänsteresa",
    ApprovalSource.COMMUTE: "Resa till arbetet",
    ApprovalSource.PRIVATE_PLACE: "Privat plats",
    ApprovalSource.ASSUMED: "Antaget kundbesök",
    ApprovalSource.HOME_OFFICE: "Hem ↔ kontor",
    ApprovalSource.LUNCH: "Lunch",
}


@dataclass
class Blocker:
    code: str
    text: str
    count: int = 0
    trip_ids: list[str] = field(default_factory=list)
    web_only: bool = False


@dataclass
class MonthStatus:
    vehicle_id: uuid.UUID
    month: str  # "2026-10"
    state: Literal["open", "waiting", "exported", "stale", "empty"]
    blockers: list[Blocker]
    notes: list[Blocker]
    export_id: uuid.UUID | None
    # Underlaget skapades innan perioden var slut (månaden stängdes i förväg).
    closed_early: bool = False
    export_created_at: datetime | None = None


@dataclass
class _Facts:
    """Det evaluate räknat fram och run_for behöver (avtrycket och periodens gränser)."""

    status: MonthStatus
    fingerprint: str
    rows: int
    start: datetime
    end: datetime
    record: MonthClose | None


# ----------------------------------------------------------------------------- hjälpare


def month_key(month: date) -> str:
    return f"{month.year:04d}-{month.month:02d}"


def _first(month: date) -> date:
    return month.replace(day=1)


def _next_month(month: date) -> date:
    return date(month.year + 1, 1, 1) if month.month == 12 else date(month.year, month.month + 1, 1)


def _previous_month(month: date) -> date:
    return date(month.year - 1, 12, 1) if month.month == 1 else date(month.year, month.month - 1, 1)


def _parse_month(key: str) -> date | None:
    try:
        return date(int(key[:4]), int(key[5:7]), 1)
    except (ValueError, TypeError):
        return None


def _bounds(month: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    first = _first(month)
    return common.local_midnight(first, tz), common.local_midnight(_next_month(first), tz)


def _n(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _ids(trips: list[LogicalTrip]) -> list[str]:
    return [str(t.id) for t in trips][: checks_mod.MAX_IDS]


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else None


def _record(db: Session, vehicle_id: uuid.UUID, key: str) -> MonthClose | None:
    return db.execute(
        select(MonthClose).where(MonthClose.vehicle_id == vehicle_id, MonthClose.month == key)
    ).scalar_one_or_none()


def rows_fingerprint(journal: Any) -> str:
    """sha256 över underlagets rader (``SimpleRow.as_dict``) och summor."""
    return export_svc.rows_fingerprint(journal)


def _has_live_data(db: Session, vehicle: Vehicle) -> bool:
    return db.execute(
        select(VehicleObservation.id).where(VehicleObservation.vehicle_id == vehicle.id).limit(1)
    ).first() is not None


# ----------------------------------------------------------------------------- blockerare


def _final_fetch_blocker(db: Session, vehicle: Vehicle, end: datetime) -> Blocker | None:
    """Väntar på en lyckad hämtning efter månadsslutet som visar bilens läge då.

    Hämtningen räknas när bilens mätarställning är rapporterad efter månadsslutet, eller
    när mätarställningen är densamma som vid sista resans slut (bilen har stått still).
    Obearbetade avläsningar från före månadsslutet betyder att resor kan saknas.
    """
    if not _has_live_data(db, vehicle):
        return None  # fordon utan uppgifter från bilen (bara import) väntar inte på bilen
    waiting = Blocker("waiting_final_fetch", "Väntar på bilens sista uppgifter", count=1)
    unprocessed = db.execute(
        select(VehicleObservation.id).where(
            VehicleObservation.vehicle_id == vehicle.id,
            VehicleObservation.processed_at.is_(None),
            VehicleObservation.fetched_at < end,
        ).limit(1)
    ).first()
    if unprocessed is not None:
        return waiting
    last_odo = db.execute(
        select(LogicalTrip.odometer_end).where(
            LogicalTrip.vehicle_id == vehicle.id, LogicalTrip.state == "active",
            LogicalTrip.sort_time < end, LogicalTrip.odometer_end.is_not(None),
        ).order_by(LogicalTrip.sort_time.desc()).limit(1)
    ).scalar_one_or_none()
    for obs in db.execute(
        select(VehicleObservation).where(
            VehicleObservation.vehicle_id == vehicle.id, VehicleObservation.last_seen_at >= end
        ).order_by(VehicleObservation.last_seen_at.desc()).limit(50)
    ).scalars():
        if obs.odometer_reported_at is not None and obs.odometer_reported_at >= end:
            return None
        if obs.odometer_km is not None and last_odo is not None and abs(obs.odometer_km - last_odo) < 1.0:
            return None
    return waiting


def _volvo_blocker(db: Session, vehicle: Vehicle) -> Blocker | None:
    if vehicle.is_demo:
        return None
    conn = db.get(IntegrationConnection, vehicle.volvo_connection_id) if vehicle.volvo_connection_id else None
    if conn is None:
        conn = db.execute(
            select(IntegrationConnection).where(
                IntegrationConnection.user_id == vehicle.owner_user_id,
                IntegrationConnection.provider == IntegrationProvider.VOLVO,
            )
        ).scalar_one_or_none()
    if conn is not None and conn.status == IntegrationStatus.NEEDS_REAUTH:
        return Blocker("volvo_disconnected", "Volvo behöver ny inloggning", count=1)
    return None


def _question_blockers(db: Session, vehicle: Vehicle, trips: list[LogicalTrip]) -> tuple[list[Blocker], set[Any]]:
    """Obesvarade frågor (också uppskjutna) och oavgjorda km på resor som inte är godkända."""
    pending = {t.id: t for t in trips if t.review_status not in common.REPORTED}
    phone: dict[Any, LogicalTrip] = {}
    web: dict[Any, LogicalTrip] = {}
    if pending:
        ids = list(pending)
        for i in range(0, len(ids), 500):
            for item in db.execute(
                select(ReviewItem).where(
                    ReviewItem.vehicle_id == vehicle.id,
                    ReviewItem.status.in_(OPEN_ITEM_STATES),
                    ReviewItem.kind.in_(QUESTION_KINDS),
                    ReviewItem.logical_trip_id.in_(ids[i : i + 500]),
                )
            ).scalars():
                trip = pending[item.logical_trip_id]
                (web if item.kind == ReviewKind.MIXED_ALLOCATION else phone)[trip.id] = trip
    for t in trips:
        if not common.is_gap_fill(t) and t.undecided_km > common.KM_EPS and t.id not in web:
            phone.setdefault(t.id, t)
    # Självgående läge: telefonen frågar bara om resor från bilen efter uppgraderingen
    # (notifier.ensure_open_questions). Övriga oavgjorda resor hänvisas till webben.
    from app.services.ha import notifier

    since = notifier._self_driving_since(db, vehicle.owner_user_id)
    if since is not None:
        for tid, t in list(phone.items()):
            if not notifier.is_live_since(db, t, since):
                web[tid] = phone.pop(tid)
    out: list[Blocker] = []
    only_phone = [t for tid, t in phone.items() if tid not in web]
    if only_phone:
        out.append(Blocker("open_questions", f"{_n(len(only_phone), 'resa väntar', 'resor väntar')} på ditt svar",
                           count=len(only_phone), trip_ids=_ids(only_phone)))
    if web:
        w = list(web.values())
        out.append(Blocker("open_questions",
                           f"{_n(len(w), 'resa väntar', 'resor väntar')} på ditt svar – öppna webben",
                           count=len(w), trip_ids=_ids(w), web_only=True))
    return out, set(phone) | set(web)


def _work_km(t: LogicalTrip) -> float:
    return common.segment_km(t)["work"]


def _is_private(t: LogicalTrip) -> bool:
    km = common.segment_km(t)
    return km["work"] <= common.KM_EPS and km["private"] > common.KM_EPS


def _veto_blocker(reported: list[LogicalTrip], now: datetime, tz: ZoneInfo) -> Blocker | None:
    open_until: list[tuple[datetime, LogicalTrip]] = []
    for t in reported:
        if t.approval_source != ApprovalSource.ASSUMED:
            continue
        basis = t.approval_basis if isinstance(t.approval_basis, dict) else {}
        until = _parse_dt(basis.get("veto_until"))
        if until is not None and until > now:
            open_until.append((until, t))
    if not open_until:
        return None
    last = max(u for u, _ in open_until).astimezone(tz)
    trips = [t for _, t in open_until]
    return Blocker("assumption_veto_open", f"Väntar till {last.day}/{last.month} kl. {last:%H:%M} på ditt veto",
                   count=len(trips), trip_ids=_ids(trips))


def _locations(db: Session, trips: list[LogicalTrip]) -> dict[Any, KnownLocation]:
    ids = {i for t in trips for i in (t.start_location_id, t.end_location_id) if i is not None}
    if not ids:
        return {}
    return {loc.id: loc for loc in db.execute(select(KnownLocation).where(KnownLocation.id.in_(ids))).scalars()}


HOME_MISSING_TEXT = "Adresser slås inte upp förrän du markerat din bostad (Översikt → Kom igång)"


def _home_missing_note(
    db: Session, user_id: uuid.UUID, vehicle: Vehicle, trips: list[LogicalTrip], locs: dict[Any, KnownLocation]
) -> Blocker | None:
    """Resor vars plats från bilen väntar på adress för att bostaden inte är markerad
    (places.geocode._user_provider). En upplysning, ingen blockerare: den säger bara varför."""
    from app.services.places import geocode

    if vehicle.is_demo or not geocode.geocoding_enabled(db, user_id) or geocode._home_has_coordinate(db, user_id):
        return None
    waiting = [
        t for t in trips
        if any(locs.get(i) is not None and locs[i].created_from == "api"
               and locs[i].geocode_status == GeocodeStatus.PENDING
               for i in (t.start_location_id, t.end_location_id))
    ]
    if not waiting:
        return None
    return Blocker("home_missing", HOME_MISSING_TEXT, count=len(waiting), trip_ids=_ids(waiting))


def _address_blocker(reported: list[LogicalTrip], locs: dict[Any, KnownLocation]) -> Blocker | None:
    waiting = (GeocodeStatus.PENDING, GeocodeStatus.FAILED)
    trips = [
        t for t in reported
        if any(locs.get(i) is not None and locs[i].geocode_status in waiting
               for i in (t.start_location_id, t.end_location_id))
    ]
    if not trips:
        return None
    pending = any(
        locs.get(i) is not None and locs[i].geocode_status == GeocodeStatus.PENDING
        for t in trips for i in (t.start_location_id, t.end_location_id)
    )
    text = "Adresser slås upp" if pending else "Adressen kunde inte slås upp än – nytt försök görs varje dygn"
    return Blocker("addresses_pending", text, count=len(trips), trip_ids=_ids(trips))


# ----------------------------------------------------------------------------- upplysningar


_NOTE_TEXTS = {
    "possible_missed_trips": ("resa kan vara flera körningar", "resor kan vara flera körningar"),
    "missing_start_time": ("resa saknar starttid", "resor saknar starttid"),
    "estimated_times": ("resa har uträknad tid (ca)", "resor har uträknad tid (ca)"),
    "missing_address": ("resa saknar adress", "resor saknar adress"),
}


def _notes(
    db: Session, *, vehicle: Vehicle, trips: list[LogicalTrip], reported: list[LogicalTrip],
    quality: list[dict[str, Any]], locs: dict[Any, KnownLocation], start: datetime, end: datetime,
) -> list[Blocker]:
    out: list[Blocker] = []
    for check in quality:
        if check["severity"] == checks_mod.ERROR or check["code"] not in _NOTE_TEXTS:
            continue
        one, many = _NOTE_TEXTS[check["code"]]
        out.append(Blocker(check["code"], _n(int(check["count"]), one, many), count=int(check["count"]),
                           trip_ids=list(check["trip_ids"])))

    by_source: dict[str, list[LogicalTrip]] = defaultdict(list)
    for t in reported:
        if t.approval_source in AUTO_APPROVAL_SOURCES and t.approval_source != ApprovalSource.ASSUMED:
            by_source[str(t.approval_source)].append(t)
    for source, group in sorted(by_source.items()):
        out.append(Blocker("auto_approved", f"{SOURCE_LABELS.get(source, source)}: {_n(len(group), 'resa', 'resor')}",
                           count=len(group), trip_ids=_ids(group)))

    from_calendar = _from_calendar(db, reported)
    if from_calendar:
        # Ägarens beslut A: ärendet sattes ur kalendern utan fråga – ägaren kan rätta med ett tryck.
        out.append(Blocker("purpose_from_calendar",
                           f"Ärende ur kalendern: {_n(len(from_calendar), 'resa', 'resor')}",
                           count=len(from_calendar), trip_ids=_ids(from_calendar)))

    assumed = [t for t in reported if t.approval_source == ApprovalSource.ASSUMED]
    if assumed:
        out.append(Blocker("assumed", f"{_n(len(assumed), 'antagen rad', 'antagna rader')} – se dem innan du skickar",
                           count=len(assumed), trip_ids=_ids(assumed)))

    quiet: dict[str, list[LogicalTrip]] = defaultdict(list)
    for t in trips:
        if (t.review_status in common.REPORTED and t.approval_source in AUTO_APPROVAL_SOURCES
                and _is_private(t)):
            quiet[str(t.approval_source)].append(t)
    for source, group in sorted(quiet.items()):
        out.append(Blocker("quiet_private",
                           f"Privat utan fråga, {SOURCE_LABELS.get(source, source).lower()}: "
                           f"{_n(len(group), 'resa', 'resor')}", count=len(group), trip_ids=_ids(group)))

    looked_up = [loc for loc in locs.values() if loc.created_from == "api" and loc.geocode_status == GeocodeStatus.OK]
    if looked_up:
        out.append(Blocker("addresses_auto", f"{_n(len(looked_up), 'adress', 'adresser')} uppslagna automatiskt",
                           count=len(looked_up)))

    rules = int(db.execute(
        select(func.count(LocationPattern.id)).where(
            LocationPattern.owner_user_id == vehicle.owner_user_id,
            LocationPattern.is_manual_rule.is_(True),
            LocationPattern.updated_at >= start,
            LocationPattern.updated_at < end,
        )
    ).scalar_one())
    if rules:
        out.append(Blocker("new_rules", f"{_n(rules, 'egen regel', 'egna regler')} skapade eller ändrade i månaden",
                           count=rules))
    return out


def _from_calendar(db: Session, trips: list[LogicalTrip]) -> list[LogicalTrip]:
    """Redovisade resor vars aktuella ärende sattes ur kalendern av automatiken (beslut A)."""
    ids = [t.id for t in trips if t.approval_source in AUTO_APPROVAL_SOURCES]
    if not ids:
        return []
    flagged = {
        tid for tid, based in db.execute(
            select(TripPurpose.logical_trip_id, TripPurpose.based_on).where(
                TripPurpose.logical_trip_id.in_(ids), TripPurpose.is_current.is_(True)
            )
        ).all()
        if isinstance(based, dict) and based.get("from_calendar")
    }
    return [t for t in trips if t.id in flagged]


# ----------------------------------------------------------------------------- evaluate


def _evaluate(
    db: Session, *, user_id: uuid.UUID, vehicle: Vehicle, month: date, now: datetime, force: bool = False
) -> _Facts:
    tz = settings_svc.user_timezone(db, user_id)
    start, end = _bounds(month, tz)
    key = month_key(month)
    record = _record(db, vehicle.id, key)

    journal, trips = export_svc.build_simple_journal(db, vehicle=vehicle, start=start, end=end, tz=tz)
    fingerprint = rows_fingerprint(journal)
    reported = [t for t in trips if common.is_reported(t)]
    locs = _locations(db, trips)

    blockers: list[Blocker] = []
    month_over = now >= end + MONTH_GRACE
    if not month_over and not force:
        blockers.append(Blocker("month_not_over", "Månaden är inte slut än", count=1))
    if now >= end:
        fetch = _final_fetch_blocker(db, vehicle, end)
        if fetch is not None:
            blockers.append(fetch)
    volvo = _volvo_blocker(db, vehicle)
    if volvo is not None:
        blockers.append(volvo)
    questions, question_ids = _question_blockers(db, vehicle, trips)
    blockers += questions
    unapproved = [
        t for t in trips
        if t.review_status not in common.REPORTED and t.id not in question_ids and _work_km(t) > common.KM_EPS
    ]
    if unapproved:
        text = ("1 tjänsteresa är inte godkänd" if len(unapproved) == 1
                else f"{len(unapproved)} tjänsteresor är inte godkända")
        blockers.append(Blocker("unapproved_work", text, count=len(unapproved), trip_ids=_ids(unapproved)))
    veto = _veto_blocker(reported, now, tz)
    if veto is not None:
        blockers.append(veto)
    if now < end + ADDRESS_WAIT:
        addresses = _address_blocker(reported, locs)
        if addresses is not None:
            blockers.append(addresses)

    quality = checks_mod.reported_quality_checks(db, reported)
    errors = [
        c for c in checks_mod.run_checks(db, vehicle=vehicle, start=start, end=end, tz=tz, now=now)
        # Demodata märks redan i filen (rubrik och filnamn); den stoppar inte demons månadsskifte.
        if c["severity"] == checks_mod.ERROR and c["code"] != "demo_data"
    ]
    seen = {c["code"] for c in errors}
    errors += [c for c in quality if c["severity"] == checks_mod.ERROR and c["code"] not in seen]
    if errors:
        blockers.append(Blocker("export_errors", str(errors[0]["title"]), count=len(errors),
                                trip_ids=list(errors[0].get("trip_ids") or [])))

    notes = _notes(db, vehicle=vehicle, trips=trips, reported=reported, quality=quality, locs=locs,
                   start=start, end=end)
    home_note = _home_missing_note(db, user_id, vehicle, trips, locs)
    if home_note is not None:
        notes.append(home_note)

    has_export = record is not None and record.export_id is not None
    export = db.get(ExportJob, record.export_id) if has_export else None  # type: ignore[union-attr]
    state: Literal["open", "waiting", "exported", "stale", "empty"]
    # En fil som finns avgör läget, även innan månaden är slut (underlaget skapat i förväg).
    if has_export:
        changed = record.rows_fingerprint != fingerprint  # type: ignore[union-attr]
        state = "stale" if changed or record.state == MonthCloseState.STALE else "exported"  # type: ignore[union-attr]
    elif any(b.code == "month_not_over" for b in blockers):
        state = "open"
    elif blockers:
        state = "waiting"
    elif not journal.rows:
        state = "empty"
    else:
        state = "open"  # komplett och klar att skapa underlaget
    created_at = export.created_at if export is not None else None
    status = MonthStatus(vehicle_id=vehicle.id, month=key, state=state, blockers=blockers, notes=notes,
                         export_id=record.export_id if record is not None else None,
                         closed_early=created_at is not None and created_at < end,
                         export_created_at=created_at)
    return _Facts(status=status, fingerprint=fingerprint, rows=len(journal.rows), start=start, end=end,
                  record=record)


def evaluate(db: Session, *, user_id: uuid.UUID, vehicle: Vehicle, month: date, now: datetime) -> MonthStatus:
    """Månadens läge, blockerare och upplysningar; skriver inget."""
    return _evaluate(db, user_id=user_id, vehicle=vehicle, month=month, now=now).status


# ----------------------------------------------------------------------------- run_for


def _save(record: MonthClose, status: MonthStatus, now: datetime) -> None:
    record.blockers = [asdict(b) for b in status.blockers]
    record.notes = [asdict(b) for b in status.notes]
    record.last_checked_at = now


def _create_export(
    db: Session, *, user_id: uuid.UUID, vehicle: Vehicle, facts: _Facts, now: datetime
) -> ExportJob:
    tz = settings_svc.user_timezone(db, user_id)
    return export_svc.create_export(
        db, user_id=user_id, vehicle=vehicle, start=facts.start, end=facts.end,
        actor=audit.Actor("system", "month_close"), tz=tz, now=now, origin=ExportOrigin.MONTH_CLOSE,
    )


def _notify_ready(
    db: Session, record: MonthClose, status: MonthStatus, export: ExportJob | None, now: datetime, *, update: bool
) -> None:
    """"Klart"/"uppdaterad"/"inga tjänsteresor". En gång per fil, uppdateringar högst 1 per dygn."""
    from app.services.ha import notices, notifier

    key = f"monthly:{status.vehicle_id}:{status.month}:ready:{export.id if export is not None else 'empty'}"
    if notifier._exists(db, record.user_id, key):
        return
    if update and _ready_sent_since(db, record, now - UPDATE_NOTICE_EVERY):
        return  # nästa kontroll skickar notisen när dygnet har gått
    req = notices.queue_month_ready(db, user_id=record.user_id, status=status, export=export, now=now)
    if req is not None:
        record.notified_at = now


def _ready_sent_since(db: Session, record: MonthClose, since: datetime) -> bool:
    """Har en "klart"/"uppdaterad"-notis för månaden skapats sedan ``since``? (En "väntar"-notis
    räknas inte: den får inte skjuta upp beskedet om det nya underlaget ett dygn.)"""
    from app.models import NotificationRequest

    prefix = f"monthly:{record.vehicle_id}:{record.month}:ready:"
    return db.execute(
        select(NotificationRequest.id).where(
            NotificationRequest.user_id == record.user_id,
            NotificationRequest.dedupe_key.like(f"{prefix}%"),
            NotificationRequest.created_at >= since,
        ).limit(1)
    ).first() is not None


def _notify_waiting(db: Session, record: MonthClose, status: MonthStatus, now: datetime) -> None:
    """"Oktober väntar": första gången och sedan var 3:e dygn, högst 3 gånger."""
    from app.services.ha import notices

    count = int(record.notice_count or 0)
    if count >= WAITING_NOTICE_MAX:
        return
    if count > 0 and record.notified_at is not None and now - record.notified_at < WAITING_NOTICE_EVERY:
        return
    req = notices.queue_month_waiting(db, user_id=record.user_id, status=status, now=now)
    if req is not None:
        record.notice_count = count + 1
        record.notified_at = now


def run_for(
    db: Session, *, user_id: uuid.UUID, vehicle: Vehicle, month: date, now: datetime, force: bool = False,
    notify_waiting: bool = False,
) -> MonthStatus:
    """Kontrollerar en månad och skapar underlaget när den är komplett.

    ``force`` hoppar bara över väntan på månadsslutet (och, när en fil redan finns och något
    har ändrats, steget att först markera månaden: den nya filen skapas direkt om inget
    väntar). ``notify_waiting`` (från ``run_due``) skickar notisen om vad som väntar; en
    kontroll från webben gör det inte.
    """
    facts = _evaluate(db, user_id=user_id, vehicle=vehicle, month=month, now=now, force=force)
    status = facts.status
    if any(b.code == "month_not_over" for b in status.blockers):
        return status  # inget sparas förrän månaden är slut

    record = facts.record
    if record is None:
        record = MonthClose(user_id=user_id, vehicle_id=vehicle.id, month=status.month,
                            state=MonthCloseState.OPEN, notice_count=0)
        db.add(record)
        db.flush()
    _save(record, status, now)
    real_blockers = [b for b in status.blockers if b.code != "month_not_over"]

    if status.state == "open" and not real_blockers:
        job = _create_export(db, user_id=user_id, vehicle=vehicle, facts=facts, now=now)
        record.state, record.export_id, record.rows_fingerprint = MonthCloseState.EXPORTED, job.id, facts.fingerprint
        audit.record(db, audit.Actor("system", "month_close"), "month_close.exported", "month_close", record.id,
                     after={"month": status.month, "export_id": str(job.id), "closed_early": now < facts.end},
                     user_id=user_id)
        _set_export(status, job, facts)
        _notify_ready(db, record, status, job, now, update=False)
    elif status.state == "empty":
        record.state = MonthCloseState.EMPTY
        _notify_ready(db, record, status, None, now, update=False)
    elif status.state == "waiting":
        record.state = MonthCloseState.WAITING
        if notify_waiting:
            _notify_waiting(db, record, status, now)
    elif status.state == "stale":
        changed = record.rows_fingerprint != facts.fingerprint
        # Ägarens tryck (force) på en ändrad månad skapar den nya filen direkt (F5).
        direct = force and changed and not real_blockers
        if record.state != MonthCloseState.STALE and not direct:
            # Första gången ändringen syns: månaden markeras, nästa kontroll skapar filen.
            record.state = MonthCloseState.STALE
            audit.record(db, audit.Actor("system", "month_close"), "month_close.stale", "month_close", record.id,
                         after={"month": status.month, "reason": "rows_changed"}, user_id=user_id)
        elif changed and not real_blockers and facts.rows:
            old = db.get(ExportJob, record.export_id) if record.export_id else None
            job = _create_export(db, user_id=user_id, vehicle=vehicle, facts=facts, now=now)
            if old is not None:
                old.superseded_by_id = job.id
                db.flush()  # notisen hittar den ersatta filen ("uppdaterad"), sessionen flushar inte själv
            record.state, record.export_id, record.rows_fingerprint = (
                MonthCloseState.EXPORTED, job.id, facts.fingerprint)
            audit.record(db, audit.Actor("system", "month_close"), "month_close.superseded", "month_close",
                         record.id, after={"month": status.month, "export_id": str(job.id),
                                           "previous_export_id": str(old.id) if old else None}, user_id=user_id)
            _set_export(status, job, facts)
            _notify_ready(db, record, status, job, now, update=old is not None)
        elif changed and not real_blockers:
            # Inga tjänsteresor kvar: den gamla filen återkallas (länken ger 410) och
            # månaden blir tom. En återkallad fil pekar på sig själv i superseded_by_id.
            old = db.get(ExportJob, record.export_id) if record.export_id else None
            if old is not None and old.superseded_by_id is None:
                old.superseded_by_id = old.id
            record.state, record.export_id, record.rows_fingerprint = (
                MonthCloseState.EMPTY, None, facts.fingerprint)
            audit.record(db, audit.Actor("system", "month_close"), "month_close.revoked", "month_close",
                         record.id, after={"month": status.month,
                                           "previous_export_id": str(old.id) if old else None}, user_id=user_id)
            status.state, status.export_id = "empty", None
            status.closed_early, status.export_created_at = False, None
            _notify_ready(db, record, status, None, now, update=False)
    elif status.state == "exported":
        record.state = MonthCloseState.EXPORTED
        job = db.get(ExportJob, record.export_id) if record.export_id else None
        if job is not None:
            _notify_ready(db, record, status, job, now, update=_replaces_older(db, job))
    if notify_waiting and status.state in ("exported", "stale") and status.closed_early:
        # Stängd i förväg: en tjänsteresa som väntar på svar ändrar inte avtrycket (bara
        # redovisade resor ingår). Ägaren får den vanliga "väntar"-notisen; filen finns kvar.
        waiting = [b for b in real_blockers if b.code not in SELF_RESOLVING]
        if waiting:
            _notify_waiting(db, record, replace(status, blockers=waiting), now)
    db.flush()
    return status


def _set_export(status: MonthStatus, job: ExportJob, facts: _Facts) -> None:
    status.state, status.export_id = "exported", job.id
    status.export_created_at = job.created_at
    status.closed_early = job.created_at is not None and job.created_at < facts.end


def _replaces_older(db: Session, job: ExportJob) -> bool:
    """Ersätter filen en äldre fil för månaden? Då är notisen en uppdatering."""
    return db.execute(select(ExportJob.id).where(ExportJob.superseded_by_id == job.id).limit(1)).first() is not None


# ----------------------------------------------------------------------------- run_due


def _due_months(db: Session, vehicle: Vehicle, *, now: datetime, tz: ZoneInfo, active_since: datetime) -> list[date]:
    current = _first(now.astimezone(tz).date())
    previous = _previous_month(current)
    months: set[date] = set()
    if now >= common.local_midnight(current, tz) + MONTH_GRACE:
        months.add(previous)
    oldest = current
    for _ in range(RECHECK_MONTHS):
        oldest = _previous_month(oldest)
    for (key,) in db.execute(
        select(MonthClose.month).where(
            MonthClose.vehicle_id == vehicle.id,
            MonthClose.state.in_((MonthCloseState.WAITING, MonthCloseState.STALE, MonthCloseState.EXPORTED)),
            MonthClose.month >= month_key(oldest),
        )
    ).all():
        parsed = _parse_month(key)
        if parsed is not None and parsed < current:
            months.add(parsed)
    # Månader som var slut innan självgående läge började gälla sköts som förut, av ägaren.
    return sorted(m for m in months if _bounds(m, tz)[1] > active_since)


def run_due(db: Session, now: datetime | None = None) -> dict[str, Any]:
    """Kontrollerar månadsskiftet för alla fordon i självgående läge (högst 1 gång/h per månad)."""
    from app.services.classify import autopilot

    now = now or utcnow()
    counts: Counter[str] = Counter()
    users = list(db.execute(select(User).where(User.is_active.is_(True))).scalars())
    for user in users:
        if not autopilot.is_active(db, user.id):
            continue
        active_since = settings_svc.active_since(db, user.id)
        if active_since is None:
            continue
        tz = settings_svc.user_timezone(db, user.id)
        vehicles = list(db.execute(
            select(Vehicle).where(Vehicle.owner_user_id == user.id, Vehicle.is_active.is_(True))
        ).scalars())
        for vehicle in vehicles:
            if not _has_live_data(db, vehicle):
                continue
            for month in _due_months(db, vehicle, now=now, tz=tz, active_since=active_since):
                record = _record(db, vehicle.id, month_key(month))
                if record is not None and record.last_checked_at is not None and \
                        now - record.last_checked_at < CHECK_INTERVAL:
                    continue
                try:
                    with db.begin_nested():
                        status = run_for(db, user_id=user.id, vehicle=vehicle, month=month, now=now,
                                         notify_waiting=True)
                except Exception:
                    logger.exception("Månadsskiftet misslyckades för ett fordon")
                    counts["errors"] += 1
                    continue
                counts["checked"] += 1
                counts[status.state] += 1
    return dict(counts)


__all__ = [
    "MONTH_GRACE", "Blocker", "MonthStatus", "evaluate", "month_key", "rows_fingerprint", "run_due", "run_for",
]
