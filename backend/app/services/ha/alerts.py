"""Larm om kopplingarna: Volvo, kalendern, en tyst bil och en omarkerad bostad (byggkontraktet C.5).

``run`` körs i tick men prövar villkoren högst var 15:e minut. Varje fel har en
*episod* (t.ex. tidpunkten för den senaste lyckade hämtningen). Ett larm skickas en
gång per episod och påminns aldrig: tillståndet är själva ``NotificationRequest`` med
dedupe-nyckeln ``alert:{code}:{episode}`` (``notices.queue_alert``), inte något i
``IntegrationConnection.config``. Högst två larm per dygn och användare. Tysta timmar
gäller genom den vanliga leveransen. När felet är borta tas larmet bort från
telefonen utan någon ny push (``notices.resolve_alert``).

Texterna innehåller aldrig platser, VIN eller belopp.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import IntegrationConnection, NotificationRequest, RawTrip, Vehicle, VehicleObservation
from app.models.enums import IntegrationProvider, IntegrationStatus, NotificationKind, TripSource
from app.services import settings as settings_svc
from app.services.ha import notices, notifier

logger = logging.getLogger(__name__)

RUN_INTERVAL = timedelta(minutes=15)
MAX_PER_DAY = 2
REAUTH_SOON_DAYS = (3, 14)  # första larmet vid 14 dygn kvar, ett nytt vid 3
FETCH_FAILING_AFTER = timedelta(hours=6)
VEHICLE_SILENT_AFTER = timedelta(days=4)
CALENDAR_STALE_AFTER = timedelta(hours=24)

VOLVO_REAUTH_SOON = "volvo_reauth_soon"
VOLVO_REAUTH = "volvo_reauth"
VOLVO_FETCH_FAILING = "volvo_fetch_failing"
VEHICLE_SILENT = "vehicle_silent"
CALENDAR_BROKEN = "calendar_broken"
HOME_MISSING = "home_missing"

# Vart "Öppna" leder; övriga larm öppnar inställningarna.
_URIS = {HOME_MISSING: "/"}

# Ordningen avgör vilket larm som går först när dygnsgränsen är nära.
CODES = (VOLVO_REAUTH, VOLVO_FETCH_FAILING, CALENDAR_BROKEN, HOME_MISSING, VOLVO_REAUTH_SOON, VEHICLE_SILENT)

_ACTIVE = (IntegrationStatus.CONNECTED, IntegrationStatus.ERROR, IntegrationStatus.NEEDS_REAUTH)

# Senaste körningen i den här processen (tick körs av en enda arbetare).
_last_run_at: datetime | None = None


@dataclass(frozen=True)
class Alert:
    episode: str
    title: str
    message: str


def _stamp(value: datetime | None) -> str:
    """Episodnyckel ur en tidpunkt (sekundupplösning, alltid UTC)."""
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%S") if value else "never"


def _when(value: datetime, tz) -> str:
    local = value.astimezone(tz)
    return f"{local.day}/{local.month} kl. {local:%H:%M}"


def _day(value: datetime, tz) -> str:
    local = value.astimezone(tz)
    return f"{local.day}/{local.month}"


def _conn(db: Session, user_id: uuid.UUID, provider: str) -> IntegrationConnection | None:
    return db.execute(
        select(IntegrationConnection).where(
            IntegrationConnection.user_id == user_id, IntegrationConnection.provider == provider
        )
    ).scalar_one_or_none()


# ----------------------------------------------------------------------------- villkoren


def _volvo_alerts(db: Session, user_id: uuid.UUID, now: datetime, tz) -> dict[str, list[Alert]]:
    from app.services.volvo import oauth

    out: dict[str, list[Alert]] = {code: [] for code in (VOLVO_REAUTH, VOLVO_FETCH_FAILING, VOLVO_REAUTH_SOON,
                                                          VEHICLE_SILENT)}
    conn = _conn(db, user_id, IntegrationProvider.VOLVO)
    if conn is None or conn.status not in _ACTIVE:
        return out
    if conn.status == IntegrationStatus.NEEDS_REAUTH:
        out[VOLVO_REAUTH].append(Alert(
            _stamp(conn.last_success_at),
            "Volvo behöver ny inloggning",
            "DriveLog kan inte hämta resor från bilen förrän du loggar in på Volvo igen.",
        ))
        return out

    due = oauth.reauth_due_at(conn)
    if due is not None:
        left = due - now
        for days in sorted(REAUTH_SOON_DAYS):
            if left <= timedelta(days=days):
                started = str((conn.config or {}).get("grant_started_at") or "")
                out[VOLVO_REAUTH_SOON].append(Alert(
                    f"{started[:32]}:{days}",
                    "Volvo-inloggningen går snart ut",
                    f"Logga in på Volvo igen senast {_day(due, tz)}, annars slutar resorna att hämtas.",
                ))
                break

    last = conn.last_success_at
    if last is not None and now - last > FETCH_FAILING_AFTER:
        out[VOLVO_FETCH_FAILING].append(Alert(
            _stamp(last),
            "Hämtningen från Volvo fungerar inte",
            f"Ingen lyckad hämtning från bilen sedan {_when(last, tz)}. DriveLog försöker igen av sig självt.",
        ))
    elif last is not None and conn.status == IntegrationStatus.CONNECTED:
        # Hämtningen fungerar – men har bilen skickat något nytt på länge?
        for vehicle in db.execute(
            select(Vehicle).where(
                Vehicle.owner_user_id == user_id, Vehicle.is_demo.is_(False), Vehicle.is_active.is_(True)
            ).order_by(Vehicle.created_at)
        ).scalars():
            newest = _newest_report(db, vehicle.id)
            if newest is not None and now - newest > VEHICLE_SILENT_AFTER:
                out[VEHICLE_SILENT].append(Alert(
                    f"{vehicle.id.hex[:12]}:{newest.astimezone(tz).date().isoformat()}",
                    "Bilen har inte hört av sig",
                    f"Bilen har inte skickat nya uppgifter sedan {_day(newest, tz)}. "
                    "Det är normalt när bilen står still. Har du kört sedan dess bör du öppna DriveLog.",
                ))
    return out


def _newest_report(db: Session, vehicle_id: uuid.UUID) -> datetime | None:
    row = db.execute(
        select(
            func.max(VehicleObservation.odometer_reported_at), func.max(VehicleObservation.position_reported_at)
        ).where(VehicleObservation.vehicle_id == vehicle_id)
    ).one()
    values = [v for v in row if v is not None]
    return max(values) if values else None


def _calendar_alerts(db: Session, user_id: uuid.UUID, now: datetime, tz) -> list[Alert]:
    conn = _conn(db, user_id, IntegrationProvider.MICROSOFT)
    if conn is None or conn.status not in _ACTIVE:
        return []
    if not settings_svc.get_section(db, user_id, "calendar")["enabled"]:
        return []
    last = conn.last_success_at
    broken = conn.status in (IntegrationStatus.NEEDS_REAUTH, IntegrationStatus.ERROR)
    stale = last is None or now - last > CALENDAR_STALE_AFTER
    if not broken and not stale:
        return []
    if conn.status == IntegrationStatus.NEEDS_REAUTH:
        message = "Outlook-kalendern behöver anslutas igen. Utan den saknas förslag på ärenden från dina möten."
    elif last is not None:
        message = f"Outlook-kalendern har inte synkats sedan {_when(last, tz)}. DriveLog försöker igen av sig självt."
    else:
        message = "Outlook-kalendern har inte gått att synka. DriveLog försöker igen av sig självt."
    # Episoden är den senaste lyckade synken – aldrig dagens datum eller felets tidpunkt –
    # så att en kalender som felar i flera dygn ger ett enda larm.
    return [Alert(_stamp(last), "Kalendern synkas inte", message)]


def _home_alerts(db: Session, user_id: uuid.UUID) -> list[Alert]:
    """Bostaden är inte markerad, så inga adresser slås upp (places.geocode._user_provider).

    Bara i självgående läge med adressuppslagningen på, när bilen är ansluten på riktigt och
    har skickat en resa eller avläsning efter att läget slogs på. Episoden är tidpunkten då
    läget slogs på: ett larm per påslag, och larmet tas bort när bostaden är markerad.
    """
    from app.services.places import geocode

    if settings_svc.automation_mode(db, user_id) != settings_svc.MODE_SELF_DRIVING:
        return []
    since = settings_svc.active_since(db, user_id)
    if since is None or not geocode.geocoding_enabled(db, user_id):
        return []
    conn = _conn(db, user_id, IntegrationProvider.VOLVO)
    if conn is None or conn.status != IntegrationStatus.CONNECTED:
        return []
    if geocode._home_has_coordinate(db, user_id):
        return []
    real = select(Vehicle.id).where(Vehicle.owner_user_id == user_id, Vehicle.is_demo.is_(False))
    seen = db.execute(
        select(VehicleObservation.id).where(
            VehicleObservation.vehicle_id.in_(real), VehicleObservation.fetched_at >= since
        ).limit(1)
    ).first() or db.execute(
        select(RawTrip.id).where(
            RawTrip.vehicle_id.in_(real), RawTrip.source == TripSource.VOLVO_API, RawTrip.created_at >= since
        ).limit(1)
    ).first()
    if seen is None:
        return []
    return [Alert(
        _stamp(since),
        "Markera din bostad",
        "DriveLog kan inte slå upp adresser än: markera din bostad i DriveLog (Översikt → Kom igång).",
    )]


# ----------------------------------------------------------------------------- tillståndet


def _open_alerts(db: Session, user_id: uuid.UUID, code: str) -> list[NotificationRequest]:
    return list(
        db.execute(
            select(NotificationRequest).where(
                NotificationRequest.user_id == user_id,
                NotificationRequest.kind == NotificationKind.ALERT,
                NotificationRequest.status.in_(notifier.OPEN_STATUSES),
                NotificationRequest.dedupe_key.like(f"alert:{code}:%"),
            )
        ).scalars()
    )


def _sent_last_day(db: Session, user_id: uuid.UUID, now: datetime) -> int:
    db.flush()
    return int(
        db.scalar(
            select(func.count())
            .select_from(NotificationRequest)
            .where(
                NotificationRequest.user_id == user_id,
                NotificationRequest.kind == NotificationKind.ALERT,
                NotificationRequest.created_at > now - timedelta(days=1),
            )
        )
        or 0
    )


def _sync_code(
    db: Session, user_id: uuid.UUID, code: str, current: list[Alert], now: datetime, budget: Callable[[], bool],
    stats: dict[str, int],
) -> None:
    keys = {f"alert:{code}:{a.episode}"[:160] for a in current}
    if not current:
        resolved = notices.resolve_alert(db, user_id=user_id, code=code)
        stats["resolved"] += resolved
        return
    # Äldre episoder av samma fel (t.ex. "14 dygn kvar" när "3 dygn kvar" gäller) tas bort tyst.
    for req in _open_alerts(db, user_id, code):
        if req.dedupe_key not in keys:
            notifier._cancel(db, req, "resolved")
            stats["resolved"] += 1
    for alert in current:
        key = f"alert:{code}:{alert.episode}"[:160]
        if notifier._exists(db, user_id, key):
            continue  # redan larmat för den här episoden – ingen påminnelse
        if not budget():
            stats["deferred_cap"] += 1
            continue
        req = notices.queue_alert(
            db, user_id=user_id, code=code, episode=alert.episode, title=alert.title, message=alert.message, now=now,
            uri=_URIS.get(code, "/settings"),
        )
        if req is not None:
            stats["queued"] += 1


def run_user(db: Session, user_id: uuid.UUID, now: datetime) -> dict[str, int]:
    """Prövar alla larmvillkor för en användare."""
    stats = {"queued": 0, "resolved": 0, "deferred_cap": 0}
    tz = settings_svc.user_timezone(db, user_id)
    found = _volvo_alerts(db, user_id, now, tz)
    found[CALENDAR_BROKEN] = _calendar_alerts(db, user_id, now, tz)
    found[HOME_MISSING] = _home_alerts(db, user_id)
    used = _sent_last_day(db, user_id, now)

    def budget() -> bool:
        nonlocal used
        if used >= MAX_PER_DAY:
            return False
        used += 1
        return True

    for code in CODES:
        _sync_code(db, user_id, code, found.get(code, []), now, budget, stats)
    db.flush()
    return stats


def run(db: Session, now: datetime | None = None, *, force: bool = False) -> dict[str, Any]:
    """Prövar larmvillkoren för alla användare med Volvo eller kalender (högst var 15:e minut).

    Returnerar statistik, eller ``{}`` när inget prövades (för tidigt eller inga användare).
    """
    global _last_run_at
    now = now or utcnow()
    if not force and _last_run_at is not None and timedelta(0) <= now - _last_run_at < RUN_INTERVAL:
        return {}
    _last_run_at = now
    user_ids = sorted(
        {
            u
            for (u,) in db.execute(
                select(IntegrationConnection.user_id).where(
                    IntegrationConnection.provider.in_((IntegrationProvider.VOLVO, IntegrationProvider.MICROSOFT))
                )
            ).all()
        }
        | {
            u
            for (u,) in db.execute(
                select(NotificationRequest.user_id)
                .where(
                    NotificationRequest.kind == NotificationKind.ALERT,
                    NotificationRequest.status.in_(notifier.OPEN_STATUSES),
                )
                .distinct()
            ).all()
        },
        key=str,
    )
    if not user_ids:
        return {}
    total = {"users": 0, "queued": 0, "resolved": 0, "deferred_cap": 0, "failed": 0}
    for user_id in user_ids:
        try:
            with db.begin_nested():
                for k, v in run_user(db, user_id, now).items():
                    total[k] += v
            total["users"] += 1
        except Exception:
            logger.exception("Larmkontrollen misslyckades för en användare")
            total["failed"] += 1
    db.flush()
    return total
