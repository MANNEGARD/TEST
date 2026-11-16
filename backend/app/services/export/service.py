"""Skapar, listar och raderar exporterade körjournaler.

Två layouter finns (inställningen ``export.layout``):

* ``simple`` (standard) – ett blad med periodens tjänsteresor, för milersättning.
* ``detailed`` – den fullständiga arbetsboken med sex blad.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_config
from app.db import utcnow
from app.models import (
    AuditEvent,
    Driver,
    ExportJob,
    ImportedFile,
    ImportRow,
    KnownLocation,
    LogicalTrip,
    User,
    Vehicle,
)
from app.models.enums import ExportOrigin
from app.security import crypto
from app.services import audit
from app.services import settings as settings_svc
from app.services.audit import Actor
from app.services.export import checks as checks_mod
from app.services.export import simple as simple_mod
from app.services.export.simple import SimpleJournal
from app.services.export.workbook import ExportContext, build_workbook
from app.services.stats import common
from app.services.stats import energy as energy_stats
from app.services.stats.common import Period
from app.services.trips import segments as seg

MAX_AUDIT_EVENTS = 5000


class ExportError(Exception):
    def __init__(self, message: str, code: str = "export_error"):
        super().__init__(message)
        self.message = message
        self.code = code


def _driver_name(db: Session, user_id: uuid.UUID) -> str:
    general = settings_svc.get_section(db, user_id, "general")
    if general.get("driver_name"):
        return general["driver_name"]
    driver = db.execute(
        select(Driver).where(Driver.owner_user_id == user_id).order_by(Driver.is_default.desc(), Driver.created_at.asc())
    ).scalars().first()
    if driver is not None and driver.name:
        return driver.name
    user = db.get(User, user_id)
    return (user.display_name or user.email) if user is not None else ""


def resolve_layout(db: Session, user_id: uuid.UUID, layout: str | None = None) -> str:
    """Exportens layout: den angivna, annars inställningen ``export.layout`` (standard ``simple``)."""
    if layout is None:
        layout = settings_svc.get_section(db, user_id, "export").get("layout")
        if layout not in settings_svc.EXPORT_LAYOUTS:
            layout = settings_svc.LAYOUT_SIMPLE
    if layout not in settings_svc.EXPORT_LAYOUTS:
        raise ExportError("Okänd layout. Välj simple eller detailed.", "invalid_layout")
    return layout


def build_simple_journal(
    db: Session,
    *,
    vehicle: Vehicle,
    start: datetime | None,
    end: datetime | None,
    tz: ZoneInfo,
    trip_ids: Iterable[uuid.UUID] | None = None,
) -> tuple[SimpleJournal, list[LogicalTrip]]:
    """Den enkla körjournalens innehåll (ingen fil skrivs) samt alla periodens resor.

    Förhandsvisningen och exportfilen byggs båda härifrån och innehåller därför alltid
    samma rader och summor.
    """
    user_id = vehicle.owner_user_id
    ids = list(trip_ids) if trip_ids is not None else None
    if ids is not None:
        start = end = None
    trips = common.load_trips(db, vehicle.id, start, end, trip_ids=ids)
    rate, show_amount = settings_svc.reimbursement_settings(db, user_id)
    general = settings_svc.get_section(db, user_id, "general")
    journal = simple_mod.build_journal(
        vehicle=vehicle,
        trips=trips,
        start=start,
        end=end,
        tz=tz,
        driver_name=_driver_name(db, user_id),
        employer_name=general.get("employer_name") or "",
        rate_per_mil=rate,
        show_amount=show_amount,
    )
    return journal, trips


def simple_summary(journal: SimpleJournal) -> dict[str, Any]:
    """Kompakt sammanfattning av den enkla exporten: summor och vad som inte togs med.

    ``excluded`` anger hur många resor med tjänstekörning i perioden som inte togs med och
    varför (väntar på godkännande, ej avgjord kategori). Privata resor räknas inte, och
    godkända resor som saknar syfte tas med och redovisas som fel i kontrollerna.
    """
    excluded = {
        key: {k: v for k, v in value.items() if k != "trip_ids"} for key, value in journal.excluded.items()
    }
    excluded["total"] = sum(v["count"] for v in excluded.values())
    return {
        "layout": settings_svc.LAYOUT_SIMPLE,
        **journal.totals(),
        "period_label": journal.period_label,
        "excluded": excluded,
    }


def rows_fingerprint(journal: SimpleJournal) -> str:
    """sha256 över underlagets rader (``SimpleRow.as_dict``) och summor.

    Avtrycket ändras bara när något som står i filen ändras; månadsskiftet skapar aldrig en
    ny fil för ett oförändrat avtryck.
    """
    payload = {"rows": [r.as_dict() for r in journal.rows], "totals": journal.totals()}
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_context(
    db: Session,
    *,
    vehicle: Vehicle,
    period: Period,
    tz: ZoneInfo,
    trip_ids: Iterable[uuid.UUID] | None = None,
    now: datetime | None = None,
) -> ExportContext:
    """Samlar allt underlag för en export (ingen fil skrivs)."""
    user_id = vehicle.owner_user_id
    now = now or utcnow()
    selection = trip_ids is not None
    ids = list(trip_ids) if trip_ids is not None else None
    trips = common.load_trips(db, vehicle.id, period.start, period.end, trip_ids=ids)
    scope = settings_svc.journal_scope(db, user_id)
    checks = checks_mod.run_checks(
        db, vehicle=vehicle, start=period.start, end=period.end, tz=tz, trip_ids=ids, now=now, scope=scope
    )
    export_cfg = settings_svc.get_section(db, user_id, "export")
    tol = float(settings_svc.get_section(db, user_id, "merge")["odometer_tolerance_km"])

    loc_ids = {t.start_location_id for t in trips if t.start_location_id} | {t.end_location_id for t in trips if t.end_location_id}
    locations = (
        {loc.id: loc for loc in db.execute(select(KnownLocation).where(KnownLocation.id.in_(loc_ids))).scalars()}
        if loc_ids
        else {}
    )
    drivers = {d.id: d.name for d in db.execute(select(Driver).where(Driver.owner_user_id == user_id)).scalars()}

    raw_row_ids = {r.import_row_id: r.id for t in trips for r in seg.ordered_raws(t) if r.import_row_id is not None}
    sources: dict[uuid.UUID, dict[str, Any]] = {}
    row_ids = list(raw_row_ids)
    for i in range(0, len(row_ids), 500):
        chunk = row_ids[i : i + 500]
        for row, filename in db.execute(
            select(ImportRow, ImportedFile.filename).join(ImportedFile, ImportedFile.id == ImportRow.file_id).where(
                ImportRow.id.in_(chunk)
            )
        ).all():
            sources[raw_row_ids[row.id]] = {"filename": filename, "row": row.row_number}

    events: list[AuditEvent] = []
    if export_cfg["include_change_history"] and trips:
        trip_keys = [str(t.id) for t in trips]
        for i in range(0, len(trip_keys), 500):
            events += list(
                db.execute(
                    select(AuditEvent).where(
                        AuditEvent.entity_type == "logical_trip", AuditEvent.entity_id.in_(trip_keys[i : i + 500])
                    )
                ).scalars()
            )
        events.sort(key=lambda e: e.at)
        events = events[-MAX_AUDIT_EVENTS:]

    if selection:
        gaps = []
        odo_start = odo_end = common.boundary_odometer(db, vehicle, None)
        starts = [common.trip_time(t) for t in trips if common.trip_time(t) is not None]
        ends = [t.ended_at or common.trip_time(t) for t in trips if (t.ended_at or common.trip_time(t)) is not None]
        fuel_start, fuel_end = (min(starts) if starts else None), (max(ends) if ends else None)
        monthly = []
        fuel_records: list = []
        charging_records: list = []
        if trips:
            monthly = _selection_energy(trips, tz)
        del fuel_start, fuel_end
    else:
        gaps = common.gaps_in_period(db, vehicle.id, period.start, period.end)
        odo_start = common.boundary_odometer(db, vehicle, period.start, tolerance_km=tol, now=now)
        odo_end = common.boundary_odometer(db, vehicle, period.end, tolerance_km=tol, now=now)
        monthly = energy_stats.monthly_breakdown(db, vehicle=vehicle, start=period.start, end=period.end, tz=tz)
        fuel_records = energy_stats.fuel_records(db, vehicle.id, period.start, period.end)
        charging_records = energy_stats.charging_records(db, vehicle.id, period.start, period.end)

    general = settings_svc.get_section(db, user_id, "general")
    return ExportContext(
        vehicle=vehicle,
        tz=tz,
        period=period,
        trips=trips,
        checks=checks,
        is_complete=checks_mod.is_complete(checks),
        odometer_start=odo_start,
        odometer_end=odo_end,
        driver_name=_driver_name(db, user_id),
        employer_name=general.get("employer_name") or "",
        drivers=drivers,
        locations=locations,
        gaps=gaps,
        fuel_records=fuel_records,
        charging_records=charging_records,
        monthly_energy=monthly,
        sources=sources,
        audit_events=events,
        include_private_addresses=bool(export_cfg["include_private_addresses"]),
        include_change_history=bool(export_cfg["include_change_history"]),
        selection=selection,
        generated_at=now,
        scope=scope,
    )


def _selection_energy(trips: list, tz: ZoneInfo) -> list[dict[str, Any]]:
    """Månadsvis energi för ett urval av resor (utan tankningar/laddningar)."""
    from collections import defaultdict

    from app.services.stats.common import Totals

    months: dict[str, Totals] = defaultdict(Totals)
    for t in trips:
        when = common.trip_time(t)
        if when is not None:
            months[when.astimezone(tz).strftime("%Y-%m")].add_trip(t)
    out = []
    for key in sorted(months):
        t = months[key]
        out.append(
            {
                "month": key, "total_km": round(t.total_km, 1),
                "fuel_verified_l": round(t.fuel.verified, 2), "fuel_verified_km": round(t.fuel.verified_km, 1),
                "fuel_coverage": t.fuel.coverage, "fuel_per_100km": t.fuel.per_100km,
                "energy_verified_kwh": round(t.energy.verified, 2), "energy_verified_km": round(t.energy.verified_km, 1),
                "energy_coverage": t.energy.coverage, "energy_per_100km": t.energy.per_100km,
                "regen_kwh": round(t.regen_kwh, 2), "refuels": 0, "refuel_liters": 0.0, "fuel_cost": None,
                "charging_sessions": 0, "charged_kwh": 0.0, "charging_cost": None,
            }
        )
    return out


def download_name(
    vehicle: Vehicle, period: Period, tz: ZoneInfo, *, selection: bool = False, layout: str = settings_svc.LAYOUT_DETAILED
) -> str:
    """Filnamn: Körjournal_<regnr>_<ÅÅÅÅ-MM>.xlsx för en hel kalendermånad, annars <från>_<till>."""
    reg = "".join(ch for ch in (vehicle.registration_number or "utan-regnr") if ch.isalnum() or ch in "-_") or "utan-regnr"
    if selection:
        part = "urval"
    elif layout == settings_svc.LAYOUT_SIMPLE:
        part = simple_mod.filename_part(period.start, period.end, tz)
    else:
        part = common.period_filename_part(period, tz)
    prefix = "DEMO_Körjournal" if vehicle.is_demo else "Körjournal"
    return f"{prefix}_{reg}_{part}.xlsx"


def create_export(
    db: Session,
    *,
    user_id: uuid.UUID,
    vehicle: Vehicle,
    start: datetime | None,
    end: datetime | None,
    trip_ids: Iterable[uuid.UUID] | None = None,
    actor: Actor,
    label: str | None = None,
    tz: ZoneInfo | None = None,
    now: datetime | None = None,
    layout: str | None = None,
    origin: str = ExportOrigin.MANUAL,
) -> ExportJob:
    """Skapar en Excel-körjournal för perioden (eller för valda resor).

    ``layout`` är ``simple`` eller ``detailed``; utelämnas den används inställningen
    ``export.layout``. ``origin`` är ``manual`` eller ``month_close`` (skapad av
    månadsskiftet). Exporten får också underlagets avtryck (``rows_fingerprint``).
    """
    if origin not in {o.value for o in ExportOrigin}:
        raise ExportError("Okänt ursprung för exporten.", "invalid_origin")
    if vehicle.owner_user_id != user_id:
        raise ExportError("Fordonet hittades inte.", "not_found")
    layout = resolve_layout(db, user_id, layout)
    if start is not None and end is not None and end <= start:
        raise ExportError("Periodens slut måste ligga efter dess början.", "invalid_period")
    cfg = get_config()
    tz = tz or settings_svc.user_timezone(db, user_id)
    now = now or utcnow()
    ids = list(trip_ids) if trip_ids is not None else None
    if ids is not None and not ids:
        raise ExportError("Inga resor valda.", "no_trips")

    if label is None:
        if start is not None and end is not None:
            label = common.period_filename_part(Period("custom", start, end, ""), tz).replace("_", " – ")
        else:
            label = "Hela historiken"
    if ids is not None:
        label = f"Urval ({len(ids)} resor)"
    period = Period("export", start if ids is None else None, end if ids is None else None, label)
    if ids is not None:
        period = Period("export", None, None, label)

    job = ExportJob(
        user_id=user_id,
        vehicle_id=vehicle.id,
        params={
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            "trip_ids": [str(i) for i in ids] if ids is not None else None,
            "label": label,
            "layout": layout,
        },
        status="pending",
        origin=str(origin),
    )
    db.add(job)
    db.flush()
    try:
        cfg.exports_dir.mkdir(parents=True, exist_ok=True)
        path = cfg.exports_dir / f"{crypto.random_token(18)}.xlsx"
        if layout == settings_svc.LAYOUT_SIMPLE:
            journal, trips = build_simple_journal(db, vehicle=vehicle, start=start, end=end, tz=tz, trip_ids=ids)
            if ids is not None and len(trips) != len(set(ids)):
                raise ExportError("En eller flera valda resor finns inte eller tillhör ett annat fordon.", "not_found")
            checks = checks_mod.run_checks(
                db, vehicle=vehicle, start=period.start, end=period.end, tz=tz, trip_ids=ids, now=now
            )
            simple_mod.write_workbook(path, journal)
            summary = simple_summary(journal)
            job.rows_fingerprint = rows_fingerprint(journal)
        else:
            ctx = build_context(db, vehicle=vehicle, period=period, tz=tz, trip_ids=ids, now=now)
            if ids is not None and len(ctx.trips) != len(set(ids)):
                raise ExportError("En eller flera valda resor finns inte eller tillhör ett annat fordon.", "not_found")
            checks = ctx.checks
            summary = {
                "layout": settings_svc.LAYOUT_DETAILED,
                **build_workbook(path, ctx),
                "odometer_start": ctx.odometer_start.value,
                "odometer_end": ctx.odometer_end.value,
                "open_gap_km": round(sum(g.km for g in ctx.gaps if g.status == "open"), 1),
            }
            journal, _ = build_simple_journal(db, vehicle=vehicle, start=start, end=end, tz=tz, trip_ids=ids)
            job.rows_fingerprint = rows_fingerprint(journal)
            summary["work_totals"] = journal.totals()  # tjänstesiffrorna till månadsnotisen
        path.chmod(0o600)
        job.file_path = str(path)
        job.sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        naming_period = Period("export", start, end, label)
        job.filename = download_name(vehicle, naming_period, tz, selection=ids is not None, layout=layout)
        job.is_complete = checks_mod.is_complete(checks)
        job.warnings = [{k: c[k] for k in ("code", "severity", "title", "message", "count")} for c in checks]
        job.summary = {
            **summary,
            **checks_mod.summarize(checks),
            "label": label,
            "is_demo": vehicle.is_demo,
            "size_bytes": path.stat().st_size,
        }
        job.status = "done"
        job.finished_at = utcnow()
    except ExportError:
        job.status = "error"
        job.finished_at = utcnow()
        raise
    except Exception as exc:
        job.status = "error"
        job.error = type(exc).__name__
        job.finished_at = utcnow()
        raise
    audit.record(
        db, actor, "export.create", "export_job", job.id,
        after={"label": label, "layout": layout, "trips": job.summary.get("trips"), "is_complete": job.is_complete,
               "origin": job.origin},
        user_id=user_id,
    )
    db.flush()
    return job


def list_exports(db: Session, user_id: uuid.UUID, vehicle_id: uuid.UUID | None = None) -> list[ExportJob]:
    q = select(ExportJob).where(ExportJob.user_id == user_id, ExportJob.deleted_at.is_(None))
    if vehicle_id is not None:
        q = q.where(ExportJob.vehicle_id == vehicle_id)
    return list(db.execute(q.order_by(ExportJob.created_at.desc())).scalars())


def get_export(db: Session, user_id: uuid.UUID, export_id: uuid.UUID) -> ExportJob:
    job = db.get(ExportJob, export_id)
    if job is None or job.user_id != user_id or job.deleted_at is not None:
        raise ExportError("Exporten hittades inte.", "not_found")
    return job


def export_path(job: ExportJob) -> Path:
    """Sökväg till exportfilen – endast inom exportkatalogen."""
    if job.status != "done" or not job.file_path:
        raise ExportError("Exporten har ingen fil.", "no_file")
    base = get_config().exports_dir.resolve()
    path = Path(job.file_path).resolve()
    if base not in path.parents or not path.is_file():
        raise ExportError("Exportfilen finns inte längre.", "file_missing")
    return path


def delete_export(db: Session, job: ExportJob, *, actor: Actor) -> None:
    """Raderar exportfilen och markerar exporten som borttagen."""
    if job.file_path:
        base = get_config().exports_dir.resolve()
        path = Path(job.file_path).resolve()
        if base in path.parents:
            path.unlink(missing_ok=True)
    job.file_path = None
    job.status = "deleted"
    job.deleted_at = utcnow()
    audit.record(db, actor, "export.delete", "export_job", job.id, user_id=job.user_id)
    db.flush()


def delete_all_exports(db: Session, user_id: uuid.UUID, *, actor: Actor) -> int:
    """Raderar alla exporterade filer för användaren (sekretess)."""
    jobs = list_exports(db, user_id)
    for job in jobs:
        delete_export(db, job, actor=actor)
    return len(jobs)
