"""Gemensamma byggstenar för statistik, rapporter och export.

All kilometerredovisning utgår från aktiva journalresor och deras segment. Mätvärden
för bränsle och el hämtas per fysisk resa (RawTrip) så att verifierade, uppskattade
och okända värden aldrig blandas ihop.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db import utcnow
from app.models import (
    DataGap,
    LogicalTrip,
    RawTrip,
    TripSegment,
    Vehicle,
    VehicleObservation,
    VehicleOdometerSnapshot,
)
from app.models.enums import Category, GapStatus, LogicalState, ReviewStatus, TripSource, ValueQuality
from app.services.trips import segments as seg

KM_EPS = 0.05
PRELIMINARY = (ReviewStatus.AUTO, ReviewStatus.PROPOSED)
# Resor vars klassning är avgjord: godkänd av föraren eller källmärkt i en importerad körjournal.
REPORTED = (ReviewStatus.APPROVED, ReviewStatus.IMPORTED)
BOUNDARY_WINDOW = timedelta(hours=36)


class PeriodError(ValueError):
    """Ogiltig periodangivelse (svenskt meddelande)."""


@dataclass(frozen=True)
class Period:
    key: str
    start: datetime | None  # UTC, inklusive
    end: datetime | None  # UTC, exklusive
    label: str

    @property
    def bounded(self) -> bool:
        return self.start is not None and self.end is not None

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "start": self.start, "end": self.end, "label": self.label}


def local_midnight(day: date, tz: ZoneInfo) -> datetime:
    """Lokal midnatt som UTC (hanterar sommartidsomställning)."""
    return datetime.combine(day, time.min, tzinfo=tz).astimezone(UTC)


def to_local(dt: datetime | None, tz: ZoneInfo) -> datetime | None:
    return dt.astimezone(tz) if dt is not None else None


def km_to_mil(km: float | None) -> float | None:
    return None if km is None else round(km / 10.0, 2)


def _month_start(day: date) -> date:
    return day.replace(day=1)


def _next_month(day: date) -> date:
    return (day.replace(day=28) + timedelta(days=4)).replace(day=1)


def resolve_period(
    key: str | None,
    *,
    tz: ZoneInfo,
    date_from: date | None = None,
    date_to: date | None = None,
    year: int | None = None,
    now: datetime | None = None,
) -> Period:
    """Tolkar ett periodfilter i användarens tidszon.

    ``custom``: från och med ``date_from`` till och med ``date_to``.
    """
    now = now or utcnow()
    today = now.astimezone(tz).date()
    key = (key or "month").lower()
    if year is not None:
        key = "year"
    if key == "today":
        return Period(key, local_midnight(today, tz), local_midnight(today + timedelta(days=1), tz), today.isoformat())
    if key == "week":
        monday = today - timedelta(days=today.weekday())
        iso = monday.isocalendar()
        return Period(
            key, local_midnight(monday, tz), local_midnight(monday + timedelta(days=7), tz), f"Vecka {iso.week} {iso.year}"
        )
    if key == "month":
        first = _month_start(today)
        return Period(key, local_midnight(first, tz), local_midnight(_next_month(first), tz), first.strftime("%Y-%m"))
    if key == "prev_month":
        first = _month_start(_month_start(today) - timedelta(days=1))
        return Period(key, local_midnight(first, tz), local_midnight(_next_month(first), tz), first.strftime("%Y-%m"))
    if key == "year":
        y = year or today.year
        if not (1990 <= y <= 2200):
            raise PeriodError("Ogiltigt år.")
        return Period("year", local_midnight(date(y, 1, 1), tz), local_midnight(date(y + 1, 1, 1), tz), str(y))
    if key == "all":
        return Period(key, None, None, "Hela historiken")
    if key == "custom":
        if date_from is None and date_to is None:
            raise PeriodError("Ange från- och/eller tilldatum för anpassat intervall.")
        if date_from is not None and date_to is not None and date_to < date_from:
            raise PeriodError("Tilldatum får inte ligga före fråndatum.")
        start = local_midnight(date_from, tz) if date_from else None
        end = local_midnight(date_to + timedelta(days=1), tz) if date_to else None
        label = f"{date_from.isoformat() if date_from else '…'} – {date_to.isoformat() if date_to else '…'}"
        return Period(key, start, end, label)
    raise PeriodError("Okänd period. Använd today, week, month, prev_month, year, all eller custom.")


def period_filename_part(period: Period, tz: ZoneInfo) -> str:
    """Kort periodbeteckning för filnamn: 2026, 2026-06 eller 2026-06-01_2026-06-30."""
    if not period.bounded:
        return "hela-historiken"
    s = period.start.astimezone(tz).date()  # type: ignore[union-attr]
    e = (period.end.astimezone(tz) - timedelta(seconds=1)).date()  # type: ignore[union-attr]
    if s.month == 1 and s.day == 1 and e.month == 12 and e.day == 31 and s.year == e.year:
        return str(s.year)
    if s.day == 1 and _next_month(s) - timedelta(days=1) == e:
        return s.strftime("%Y-%m")
    return f"{s.isoformat()}_{e.isoformat()}"


# ----------------------------------------------------------------------------- resor


def load_trips(
    db: Session,
    vehicle_id: uuid.UUID,
    start: datetime | None = None,
    end: datetime | None = None,
    *,
    trip_ids: Iterable[uuid.UUID] | None = None,
) -> list[LogicalTrip]:
    """Aktiva journalresor för perioden (efter starttid/bästa kända tid), med segment."""
    q = (
        select(LogicalTrip)
        .where(LogicalTrip.vehicle_id == vehicle_id, LogicalTrip.state == LogicalState.ACTIVE)
        .options(selectinload(LogicalTrip.segments).selectinload(TripSegment.raw_trip))
        .order_by(LogicalTrip.sort_time.asc().nulls_last(), LogicalTrip.odometer_start.asc().nulls_last())
    )
    if trip_ids is not None:
        ids = list(trip_ids)
        if not ids:
            return []
        q = q.where(LogicalTrip.id.in_(ids))
    if start is not None:
        q = q.where(LogicalTrip.sort_time >= start)
    if end is not None:
        q = q.where(LogicalTrip.sort_time < end)
    return list(db.execute(q).scalars())


def trip_time(trip: LogicalTrip) -> datetime | None:
    return trip.sort_time or trip.started_at or trip.ended_at


def segment_km(trip: LogicalTrip) -> dict[str, float]:
    """Kilometer per kategori räknat direkt ur segmenten (facit för cachefälten)."""
    out = {Category.WORK: 0.0, Category.PRIVATE: 0.0, Category.UNDECIDED: 0.0}
    for s in trip.segments:
        out[Category(seg.segment_category(trip, s))] += s.distance_km
    return {k.value: round(v, 3) for k, v in out.items()}


def trip_kind(trip: LogicalTrip) -> str:
    """work | private | undecided | mixed – för antal resor per kategori (ur segmenten)."""
    km = segment_km(trip)
    decided = [k for k in ("work", "private", "undecided") if km[k] > KM_EPS]
    if len(decided) > 1:
        return "mixed"
    if decided:
        return decided[0]
    return trip.category


def is_gap_fill(trip: LogicalTrip) -> bool:
    return trip.source == TripSource.GAP_FILL


@dataclass
class EnergyAcc:
    """Summering av ett mätvärde (bensin eller el) med täckningsgrad."""

    verified: float = 0.0
    verified_km: float = 0.0
    estimated: float = 0.0
    estimated_km: float = 0.0
    unknown_km: float = 0.0

    def add(self, value: float | None, quality: str, km: float) -> None:
        if value is not None and quality == ValueQuality.VERIFIED:
            self.verified += value
            self.verified_km += km
        elif value is not None and quality == ValueQuality.ESTIMATED:
            self.estimated += value
            self.estimated_km += km
        else:
            self.unknown_km += km

    @property
    def total_km(self) -> float:
        return self.verified_km + self.estimated_km + self.unknown_km

    @property
    def coverage(self) -> float | None:
        total = self.total_km
        return round(self.verified_km / total, 4) if total > KM_EPS else None

    @property
    def per_100km(self) -> float | None:
        """Snitt per 100 km – endast över sträcka med verifierade värden."""
        return round(self.verified / self.verified_km * 100, 2) if self.verified_km > KM_EPS else None

    def as_dict(self, unit: str) -> dict[str, Any]:
        return {
            "unit": unit,
            "verified": round(self.verified, 2),
            "verified_km": round(self.verified_km, 1),
            "estimated": round(self.estimated, 2),
            "estimated_km": round(self.estimated_km, 1),
            "unknown_km": round(self.unknown_km, 1),
            "coverage": self.coverage,
            "per_100km": self.per_100km,
        }


@dataclass
class Totals:
    trips: int = 0
    total_km: float = 0.0
    work_km: float = 0.0
    private_km: float = 0.0
    undecided_km: float = 0.0
    preliminary_work_km: float = 0.0
    preliminary_private_km: float = 0.0
    preliminary_trips: int = 0
    needs_review_trips: int = 0
    summarized_gap_km: float = 0.0  # privat körning som användaren sammanfattat ur luckor
    by_kind: dict[str, int] = field(default_factory=lambda: {"work": 0, "private": 0, "undecided": 0, "mixed": 0})
    fuel: EnergyAcc = field(default_factory=EnergyAcc)
    energy: EnergyAcc = field(default_factory=EnergyAcc)
    regen_kwh: float = 0.0
    driving_seconds: int = 0

    def add_trip(self, trip: LogicalTrip) -> None:
        # Kilometer räknas direkt ur segmenten – aldrig ur resans cachefält.
        km = segment_km(trip)
        total = km["work"] + km["private"] + km["undecided"]
        self.trips += 1
        self.total_km += total
        self.work_km += km["work"]
        self.private_km += km["private"]
        self.undecided_km += km["undecided"]
        kind = trip_kind(trip)
        self.by_kind[kind] = self.by_kind.get(kind, 0) + 1
        if trip.review_status in PRELIMINARY:
            self.preliminary_trips += 1
            self.preliminary_work_km += km["work"]
            self.preliminary_private_km += km["private"]
        elif trip.review_status == ReviewStatus.NEEDS_REVIEW:
            self.needs_review_trips += 1
        if is_gap_fill(trip):
            self.summarized_gap_km += total
        self.driving_seconds += trip.driving_seconds or 0
        for raw in seg.ordered_raws(trip):
            if raw.source == TripSource.GAP_FILL:
                # En sammanfattad lucka har inga mätvärden; räknas som okänd förbrukning.
                self.fuel.add(None, ValueQuality.UNKNOWN, raw.distance_km)
                self.energy.add(None, ValueQuality.UNKNOWN, raw.distance_km)
                continue
            self.fuel.add(raw.fuel_liters, raw.fuel_quality, raw.distance_km)
            self.energy.add(raw.energy_kwh, raw.energy_quality, raw.distance_km)
            if raw.regen_kwh is not None:
                self.regen_kwh += raw.regen_kwh

    def distance_dict(self) -> dict[str, Any]:
        def pair(km: float) -> dict[str, float]:
            return {"km": round(km, 1), "mil": round(km / 10.0, 2)}

        return {
            "total": pair(self.total_km),
            "work": pair(self.work_km),
            "private": pair(self.private_km),
            "undecided": pair(self.undecided_km),
            "preliminary_work": pair(self.preliminary_work_km),
            "preliminary_private": pair(self.preliminary_private_km),
            "summarized_gap_private": pair(self.summarized_gap_km),
        }


def totals_for(trips: Iterable[LogicalTrip]) -> Totals:
    t = Totals()
    for trip in trips:
        t.add_trip(trip)
    return t


# ----------------------------------------------------------------------------- milersättning


def round_half_up(value: float | Decimal, digits: int) -> float:
    """Avrundar som på en reseräkning och som Excels AVRUNDA: 0,5 går alltid uppåt.

    Pythons ``round`` avrundar halvor till närmaste jämna tal och räknar på talets
    binära form, så 6,105 kan bli 6,10. Här räknas på decimalformen.
    """
    number = value if isinstance(value, Decimal) else Decimal(repr(float(value)))
    return float(number.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP))


def reported_work_km(trip: LogicalTrip) -> float:
    """Tjänste-km som resan redovisas med: per segment, avrundat till en decimal.

    Samma tal står på resans rad i den enkla exporten, så summan av raderna är exakt
    den körsträcka som ersättningen beräknas på.
    """
    return round_half_up(segment_km(trip)["work"], 1)


def is_reported(trip: LogicalTrip) -> bool:
    """Resan ingår i underlaget: avgjord klassning och tjänste-km att redovisa."""
    return trip.review_status in REPORTED and reported_work_km(trip) > 0


def km_to_mil_exact(km: float) -> float:
    """Kilometer → mil med två decimaler (1 mil = 10 km)."""
    return round_half_up(Decimal(repr(float(km))) / 10, 2)


def reimbursement_amount(mil: float, rate_per_mil: float) -> float:
    """Ersättning i kronor: mil × kronor per mil, avrundat till hela ören."""
    return round_half_up(Decimal(repr(float(mil))) * Decimal(repr(float(rate_per_mil))), 2)


def reimbursement(trips: Iterable[LogicalTrip], *, rate_per_mil: float, show_amount: bool = True) -> dict[str, Any]:
    """Underlag för milersättning: tjänstemil × kronor per mil.

    Endast tjänste-km (per segment) från godkända eller importerade resor räknas.
    Tjänste-km från preliminärt klassade resor (auto/förslag) särredovisas och ingår
    aldrig i beloppet. Privat och oavgjord körning räknas inte alls.

    Varje resa räknas med sina tjänste-km avrundade till en decimal (``reported_work_km``),
    precis som i den enkla exporten, så att dashboardens belopp och exportens alltid är lika.
    """
    km = preliminary_km = 0.0
    for trip in trips:
        if trip.review_status in REPORTED:
            km += reported_work_km(trip)
        elif trip.review_status in PRELIMINARY:
            preliminary_km += reported_work_km(trip)
    km, preliminary_km = round(km, 1), round(preliminary_km, 1)
    mil = km_to_mil_exact(km)
    preliminary_mil = km_to_mil_exact(preliminary_km)
    return {
        "rate_per_mil": rate_per_mil,
        "show_amount": show_amount,
        "mil": mil,
        "km": km,
        "amount": reimbursement_amount(mil, rate_per_mil),
        "preliminary_mil": preliminary_mil,
        "preliminary_amount": reimbursement_amount(preliminary_mil, rate_per_mil),
        "currency": "SEK",
        "note": "Tjänstemil × ersättning per mil. Endast godkända och importerade tjänsteresor räknas.",
    }


# ----------------------------------------------------------------------------- luckor


def gaps_in_period(
    db: Session, vehicle_id: uuid.UUID, start: datetime | None, end: datetime | None, *, statuses: tuple[str, ...] | None = None
) -> list[DataGap]:
    """Aktuella dataluckor som överlappar perioden (i tid)."""
    q = select(DataGap).where(DataGap.vehicle_id == vehicle_id, DataGap.is_current.is_(True))
    if statuses is not None:
        q = q.where(DataGap.status.in_(statuses))
    out = []
    for gap in db.execute(q.order_by(DataGap.odometer_start.asc().nulls_last())).scalars():
        g_from, g_to = gap.from_time, gap.to_time or gap.from_time
        if start is not None and g_to is not None and g_to < start:
            continue
        if end is not None and g_from is not None and g_from >= end:
            continue
        out.append(gap)
    return out


def open_gaps(db: Session, vehicle_id: uuid.UUID, start: datetime | None, end: datetime | None) -> list[DataGap]:
    return gaps_in_period(db, vehicle_id, start, end, statuses=(GapStatus.OPEN,))


# ----------------------------------------------------------------------------- mätarställning


@dataclass(frozen=True)
class OdometerReading:
    value: float | None
    at: datetime | None
    source: str | None  # snapshot | vehicle_report | journal | latest_trip | None
    source_label: str
    is_boundary: bool  # True = avläsning som gäller vid själva periodgränsen

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "at": self.at,
            "source": self.source,
            "source_label": self.source_label,
            "is_boundary": self.is_boundary,
        }


_SNAPSHOT_LABELS = {
    "manual": "Manuell avläsning",
    "year_start": "Avläsning vid årets början",
    "year_end": "Avläsning vid årets slut",
    "api": "Fordonsrapport (Volvo API)",
    "import": "Importerad körjournal",
}
_MISSING = OdometerReading(None, None, None, "Uppgift saknas", False)


def _raw_before(db: Session, vehicle_id: uuid.UUID, at: datetime) -> RawTrip | None:
    return db.execute(
        select(RawTrip)
        .where(
            RawTrip.vehicle_id == vehicle_id,
            RawTrip.voided_at.is_(None),
            RawTrip.source != TripSource.GAP_FILL,
            RawTrip.odometer_end.is_not(None),
            RawTrip.sort_time < at,
        )
        .order_by(RawTrip.sort_time.desc())
        .limit(1)
    ).scalar_one_or_none()


def _raw_from(db: Session, vehicle_id: uuid.UUID, at: datetime) -> RawTrip | None:
    return db.execute(
        select(RawTrip)
        .where(
            RawTrip.vehicle_id == vehicle_id,
            RawTrip.voided_at.is_(None),
            RawTrip.source != TripSource.GAP_FILL,
            RawTrip.odometer_start.is_not(None),
            RawTrip.sort_time >= at,
        )
        .order_by(RawTrip.sort_time.asc())
        .limit(1)
    ).scalar_one_or_none()


def boundary_odometer(
    db: Session, vehicle: Vehicle, at: datetime | None, *, tolerance_km: float = 2.0, now: datetime | None = None
) -> OdometerReading:
    """Mätarställning vid en periodgräns – utan att någonsin gissa.

    1. En avläsning (manuell/årsskifte/fordonsrapport) nära gränsen.
    2. Journalen visar att bilen stod stilla över gränsen (föregående resas slut =
       nästa resas start).
    3. Gränsen ligger i framtiden/nu: senast kända mätarställning.
    Annars: uppgift saknas.
    """
    if at is None:
        return _MISSING
    now = now or utcnow()

    best: tuple[timedelta, OdometerReading] | None = None
    for snap in db.execute(
        select(VehicleOdometerSnapshot).where(
            VehicleOdometerSnapshot.vehicle_id == vehicle.id,
            VehicleOdometerSnapshot.at >= at - BOUNDARY_WINDOW,
            VehicleOdometerSnapshot.at <= at + BOUNDARY_WINDOW,
        )
    ).scalars():
        dist = abs(snap.at - at)
        if best is None or dist < best[0]:
            best = (
                dist,
                OdometerReading(snap.odometer_km, snap.at, "snapshot", _SNAPSHOT_LABELS.get(snap.source, "Avläsning"), True),
            )
    if best is not None:
        return best[1]

    obs = db.execute(
        select(VehicleObservation)
        .where(
            VehicleObservation.vehicle_id == vehicle.id,
            VehicleObservation.odometer_km.is_not(None),
            VehicleObservation.fetched_at >= at - BOUNDARY_WINDOW,
            VehicleObservation.fetched_at <= at + BOUNDARY_WINDOW,
        )
        .order_by(VehicleObservation.fetched_at.asc())
    ).scalars().all()
    if obs:
        nearest = min(obs, key=lambda o: abs((o.odometer_reported_at or o.fetched_at) - at))
        return OdometerReading(
            nearest.odometer_km, nearest.odometer_reported_at or nearest.fetched_at, "vehicle_report",
            "Fordonsrapport (Volvo API)", True,
        )

    prev, nxt = _raw_before(db, vehicle.id, at), _raw_from(db, vehicle.id, at)
    if prev is not None and nxt is not None:
        if abs(nxt.odometer_start - prev.odometer_end) <= tolerance_km:  # type: ignore[operator]
            return OdometerReading(
                prev.odometer_end, prev.ended_at, "journal", "Enligt journalen (bilen stod stilla över periodgränsen)", True
            )
        return _MISSING
    if prev is not None and nxt is None and at >= now - timedelta(minutes=1):
        latest = latest_odometer(db, vehicle)
        if latest.value is not None:
            return OdometerReading(latest.value, latest.at, latest.source, f"Senast kända: {latest.source_label}", True)
    return _MISSING


def latest_odometer(db: Session, vehicle: Vehicle) -> OdometerReading:
    """Senast kända mätarställning oavsett källa."""
    candidates: list[OdometerReading] = []
    obs = db.execute(
        select(VehicleObservation)
        .where(VehicleObservation.vehicle_id == vehicle.id, VehicleObservation.odometer_km.is_not(None))
        .order_by(VehicleObservation.fetched_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if obs is not None:
        candidates.append(
            OdometerReading(obs.odometer_km, obs.odometer_reported_at or obs.fetched_at, "vehicle_report",
                            "Fordonsrapport (Volvo API)", False)
        )
    snap = db.execute(
        select(VehicleOdometerSnapshot)
        .where(VehicleOdometerSnapshot.vehicle_id == vehicle.id)
        .order_by(VehicleOdometerSnapshot.at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if snap is not None:
        candidates.append(
            OdometerReading(snap.odometer_km, snap.at, "snapshot", _SNAPSHOT_LABELS.get(snap.source, "Avläsning"), False)
        )
    raw = db.execute(
        select(RawTrip)
        .where(
            RawTrip.vehicle_id == vehicle.id,
            RawTrip.voided_at.is_(None),
            RawTrip.source != TripSource.GAP_FILL,
            RawTrip.odometer_end.is_not(None),
            RawTrip.ended_at.is_not(None),
        )
        .order_by(RawTrip.ended_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if raw is not None:
        label = {
            TripSource.VOLVO_IMPORT: "Senaste resa i importerad körjournal",
            TripSource.VOLVO_API: "Senaste resa rekonstruerad från fordonsrapporter",
            TripSource.MANUAL: "Senaste manuellt inmatade resa",
            TripSource.DEMO: "Demonstrationsdata",
        }.get(raw.source, "Senaste resa")
        candidates.append(OdometerReading(raw.odometer_end, raw.ended_at, "latest_trip", label, False))
    if not candidates:
        return _MISSING
    return max(candidates, key=lambda c: c.at or datetime.min.replace(tzinfo=UTC))


def first_and_last_trip_odometer(trips: list[LogicalTrip]) -> tuple[float | None, float | None]:
    """Lägsta start- och högsta slutmätarställning bland periodens fysiska resor."""
    starts = [t.odometer_start for t in trips if t.odometer_start is not None and not is_gap_fill(t)]
    ends = [t.odometer_end for t in trips if t.odometer_end is not None and not is_gap_fill(t)]
    return (min(starts) if starts else None, max(ends) if ends else None)
