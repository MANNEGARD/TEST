"""Importflödet: förhandsgranskning → bekräfta → (ångra).

* Förhandsgranskningen lagrar varje rad (original + tolkning) utan att skapa resor.
* Bekräftelsen skapar RawTrip + LogicalTrip i en transaktion och är idempotent.
* En import kan ångras; resorna makuleras då (originalraderna finns kvar i importloggen).
"""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_config
from app.db import utcnow
from app.models import ImportBatch, ImportedFile, ImportRow, LogicalTrip, RawTrip, TripSegment, Vehicle
from app.models.enums import (
    BatchStatus,
    Category,
    Coverage,
    DecisionSource,
    DistanceQuality,
    ImportRowStatus,
    LogicalState,
    PositionSource,
    ReviewStatus,
    TimeQuality,
    TripSource,
    ValueQuality,
)
from app.services import audit
from app.services import settings as settings_svc
from app.services.audit import Actor
from app.services.importer import parse
from app.services.importer.parse import ImportFileError, ParsedFile, natural_key, parse_file
from app.services.places import registry as places
from app.services.trips import segments as seg


class ImportError_(Exception):
    def __init__(self, message: str, code: str = "import_error"):
        super().__init__(message)
        self.message = message
        self.code = code


def _existing_keys(db: Session, vehicle_id: uuid.UUID) -> dict[str, RawTrip]:
    rows = db.execute(
        select(RawTrip).where(
            RawTrip.vehicle_id == vehicle_id, RawTrip.voided_at.is_(None), RawTrip.source_key.is_not(None)
        )
    ).scalars()
    return {r.source_key: r for r in rows}  # type: ignore[misc]


def _deleted_keys(db: Session, vehicle_id: uuid.UUID) -> dict[str, RawTrip]:
    """Importerade resor som ägaren har tagit bort (och som inte finns kvar i en annan form).

    En sådan rad är inte en dubblett: importeras samma resa igen kommer den tillbaka
    ("Om jag importerar igen så vill jag att en raderad resa kommer tillbaka"). Vid flera
    borttagna med samma nyckel gäller den som togs bort sist.
    """
    from app.services.trips.removal import VOID_REASON_DELETED

    active = set(_existing_keys(db, vehicle_id))
    out: dict[str, RawTrip] = {}
    for raw in db.execute(
        select(RawTrip)
        .where(
            RawTrip.vehicle_id == vehicle_id,
            RawTrip.voided_at.is_not(None),
            RawTrip.void_reason == VOID_REASON_DELETED,
            RawTrip.import_batch_id.is_not(None),
            RawTrip.source_key.is_not(None),
        )
        .order_by(RawTrip.voided_at)
    ).scalars():
        if raw.source_key not in active:
            out[raw.source_key] = raw  # type: ignore[index]
    return out


_MONTHS = ("januari", "februari", "mars", "april", "maj", "juni", "juli", "augusti", "september", "oktober",
           "november", "december")


def _restored_message(raw: RawTrip, tz: ZoneInfo) -> str:
    when = raw.started_at or raw.ended_at
    local = when.astimezone(tz) if when is not None else None
    day = f" {local.day} {_MONTHS[local.month - 1]} {local.year}" if local is not None else ""
    return f"Du tog bort resan{day}. Den kommer tillbaka när du bekräftar importen."


def _differs(existing: RawTrip, values: dict[str, Any]) -> list[str]:
    diffs = []
    if (existing.original_notes or "") != (values.get("original_notes") or ""):
        diffs.append("anteckning")
    if abs((existing.distance_km or 0) - (values.get("distance_km") or 0)) > 0.15:
        diffs.append("avstånd")
    if (existing.original_category or "") != (values.get("original_category") or ""):
        diffs.append("kategori")
    return diffs


def _store_file(sha: str, filename: str, data: bytes) -> str:
    cfg = get_config()
    cfg.uploads_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(filename).suffix.lower() if Path(filename).suffix.lower() in {".xlsx", ".csv", ".txt"} else ".bin"
    path = cfg.uploads_dir / f"{sha}{suffix}"
    if not path.exists():
        path.write_bytes(data)
        path.chmod(0o600)
    return str(path)


def _file_already_imported(db: Session, vehicle_id: uuid.UUID, sha: str) -> bool:
    return (
        db.execute(
            select(ImportedFile.id)
            .join(ImportBatch, ImportBatch.id == ImportedFile.batch_id)
            .where(
                ImportBatch.vehicle_id == vehicle_id,
                ImportBatch.status == BatchStatus.COMMITTED,
                ImportedFile.sha256 == sha,
            )
            .limit(1)
        ).first()
        is not None
    )


def create_preview(
    db: Session,
    *,
    user_id: uuid.UUID,
    vehicle: Vehicle,
    files: list[tuple[str, bytes]],
    actor: Actor,
) -> ImportBatch:
    """Tolkar uppladdade filer och bygger en förhandsgranskning. Inga resor skapas."""
    cfg = get_config()
    if not files:
        raise ImportError_("Ingen fil har laddats upp.")
    tz: ZoneInfo = settings_svc.user_timezone(db, user_id)
    import_settings = settings_svc.get_section(db, user_id, "import")
    batch = ImportBatch(user_id=user_id, vehicle_id=vehicle.id, status=BatchStatus.PREVIEW)
    db.add(batch)
    db.flush()

    known = _existing_keys(db, vehicle.id)
    deleted = _deleted_keys(db, vehicle.id)
    seen_in_batch: dict[str, tuple[str, int]] = {}
    seen_files: set[str] = set()

    for filename, data in files:
        safe_name = Path(filename).name[:255] or "fil"
        try:
            parsed: ParsedFile = parse_file(
                safe_name, data, tz, max_bytes=cfg.max_upload_bytes, max_rows=cfg.max_import_rows
            )
        except ImportFileError as exc:
            import hashlib

            db.add(
                ImportedFile(
                    batch_id=batch.id,
                    filename=safe_name,
                    sha256=hashlib.sha256(data).hexdigest(),
                    size_bytes=len(data),
                    status="error",
                    error=str(exc),
                )
            )
            continue

        file_dup = parsed.sha256 in seen_files or _file_already_imported(db, vehicle.id, parsed.sha256)
        seen_files.add(parsed.sha256)
        stored = _store_file(parsed.sha256, safe_name, data) if import_settings["keep_original_files"] else None
        starts = [r.values.get("started_at") for r in parsed.rows if r.values.get("started_at")]
        ends = [r.values.get("ended_at") for r in parsed.rows if r.values.get("ended_at")]
        f = ImportedFile(
            batch_id=batch.id,
            filename=safe_name,
            sha256=parsed.sha256,
            size_bytes=parsed.size_bytes,
            stored_path=stored,
            format=parsed.format,
            sheet_name=parsed.sheet_name,
            row_count=len(parsed.rows),
            status="duplicate_file" if file_dup else "parsed",
            period_start=datetime.fromisoformat(min(starts)) if starts else None,
            period_end=datetime.fromisoformat(max(ends)) if ends else None,
            column_map={
                "columns": parsed.column_map,
                "ignored": parsed.ignored_columns,
                "warnings": parsed.warnings,
            },
        )
        db.add(f)
        db.flush()

        for pr in parsed.rows:
            key = natural_key(vehicle.id, pr.values) if pr.ok else None
            status = ImportRowStatus.OK
            messages = list(pr.messages)
            if not pr.ok:
                status = ImportRowStatus.ERROR
                messages = list(pr.errors) + messages
            elif key and key in known:
                diffs = _differs(known[key], pr.values)
                status = ImportRowStatus.DUPLICATE_DIFFERS if diffs else ImportRowStatus.DUPLICATE
                messages.append(
                    "Resan finns redan"
                    + (f" men skiljer sig i: {', '.join(diffs)}. Befintlig resa ändras inte." if diffs else ".")
                )
            elif key and key in seen_in_batch:
                status = ImportRowStatus.DUPLICATE
                other_file, other_row = seen_in_batch[key]
                messages.append(f"Dubblett av rad {other_row} i {other_file}.")
            elif key:
                seen_in_batch[key] = (safe_name, pr.row_number)
                if key in deleted:
                    # Ägaren har tagit bort resan: den kommer tillbaka (filen kan vara importerad
                    # förut – "duplicate_file" är bara en upplysning och stoppar inga rader).
                    messages.append(_restored_message(deleted[key], tz))
            db.add(
                ImportRow(
                    file_id=f.id,
                    row_number=pr.row_number,
                    raw=pr.raw,
                    parsed=pr.values,
                    natural_key=key,
                    status=status,
                    messages=messages,
                )
            )
    db.flush()
    batch.summary = _summarize(
        db, batch, vehicle, settings_svc.get_section(db, user_id, "merge"), restored_keys=set(deleted)
    )
    batch.coverage = batch.summary["suggested_coverage"]
    audit.record(db, actor, "import.preview", "import_batch", batch.id, after={"files": len(files)})
    db.flush()
    return batch


def _summarize(
    db: Session, batch: ImportBatch, vehicle: Vehicle, merge_settings: dict[str, Any],
    *, restored_keys: set[str] | None = None,
) -> dict[str, Any]:
    files = db.execute(select(ImportedFile).where(ImportedFile.batch_id == batch.id)).scalars().all()
    rows = (
        db.execute(
            select(ImportRow).join(ImportedFile).where(ImportedFile.batch_id == batch.id).order_by(ImportRow.row_number)
        )
        .scalars()
        .all()
    )
    status_counts = Counter(r.status for r in rows)
    new_rows = [r for r in rows if r.status in (ImportRowStatus.OK, ImportRowStatus.IMPORTED)]
    km = sum(r.parsed.get("distance_km") or 0 for r in new_rows)
    fuel = sum(r.parsed.get("fuel_liters") or 0 for r in new_rows)
    energy = sum(r.parsed.get("energy_kwh") or 0 for r in new_rows)
    cats = Counter(r.parsed.get("category", "undecided") for r in new_rows)
    notes = {r.parsed.get("original_notes") for r in new_rows if r.parsed.get("original_notes")}

    # Täckningsanalys: luckor i mätarställning mellan på varandra följande nya resor.
    tol = seg.odometer_slack(float(merge_settings["odometer_tolerance_km"]))  # 1 km är avrundning, ingen lucka
    ordered = sorted(
        (r for r in new_rows if r.parsed.get("odometer_start") is not None and r.parsed.get("started_at")),
        key=lambda r: r.parsed["started_at"],
    )
    gap_count, gap_km = 0, 0.0
    for a, b in zip(ordered, ordered[1:], strict=False):
        diff = b.parsed["odometer_start"] - a.parsed["odometer_end"]
        if diff > tol:
            gap_count += 1
            gap_km += diff
    span_km = (ordered[-1].parsed["odometer_end"] - ordered[0].parsed["odometer_start"]) if ordered else 0.0
    includes_stops = sum(1 for r in new_rows if r.parsed.get("includes_stops"))
    suggested = Coverage.UNKNOWN
    if ordered:
        suggested = Coverage.SELECTION if gap_count > 0 else Coverage.COMPLETE
    only_one_category = len([c for c in cats if cats[c] > 0]) == 1 and len(new_rows) > 5

    starts = [r.parsed["started_at"] for r in new_rows if r.parsed.get("started_at")]
    ends = [r.parsed["ended_at"] for r in new_rows if r.parsed.get("ended_at")]
    scope = settings_svc.journal_scope(db, batch.user_id)
    return {
        "files": [
            {
                "id": str(f.id),
                "filename": f.filename,
                "format": f.format,
                "sheet": f.sheet_name,
                "rows": f.row_count,
                "status": f.status,
                "error": f.error,
                "warnings": (f.column_map or {}).get("warnings", []),
                "ignored_columns": (f.column_map or {}).get("ignored", []),
            }
            for f in files
        ],
        "rows_total": len(rows),
        "rows_new": len(new_rows),
        # Av de nya: resor som ägaren har tagit bort tidigare och som kommer tillbaka.
        "rows_restored": sum(1 for r in new_rows if r.natural_key and r.natural_key in (restored_keys or set())),
        "rows_duplicate": status_counts.get(ImportRowStatus.DUPLICATE, 0),
        "rows_duplicate_differs": status_counts.get(ImportRowStatus.DUPLICATE_DIFFERS, 0),
        "rows_error": status_counts.get(ImportRowStatus.ERROR, 0),
        "distance_km": round(km, 1),
        "fuel_liters": round(fuel, 1),
        "energy_kwh": round(energy, 1),
        "categories": dict(cats),
        "distinct_notes": len(notes),
        "period_start": min(starts) if starts else None,
        "period_end": max(ends) if ends else None,
        "odometer_span_km": round(span_km, 1),
        "odometer_gap_count": gap_count,
        "odometer_gap_km": round(gap_km, 1),
        "rows_with_stops_included": includes_stops,
        "suggested_coverage": suggested,
        "single_category_only": only_one_category,
        "journal_scope": scope,
        "notes": _summary_notes(gap_count, gap_km, only_one_category, includes_stops, scope=scope),
    }


def _count(n: int, singular: str, plural: str) -> str:
    """Antal med rätt numerus: "1 resa", "3 resor"."""
    return f"{n} {singular if n == 1 else plural}"


def _summary_notes(
    gap_count: int,
    gap_km: float,
    single_category: bool,
    includes_stops: int,
    *,
    scope: str = settings_svc.SCOPE_COMPLETE,
) -> list[str]:
    """Anteckningar till importens sammanställning.

    När endast tjänsteresor redovisas (``business_only``) är luckor mellan resorna och
    en enda kategori det förväntade – det beskrivs neutralt respektive inte alls.
    """
    business_only = scope == settings_svc.SCOPE_BUSINESS_ONLY
    out = []
    if gap_count and business_only:
        out.append(f"Mellan resorna finns {gap_km:.0f} km som inte ingår i underlaget (övrig körning).")
    elif gap_count:
        out.append(
            f"Mätarställningarna har {_count(gap_count, 'lucka', 'luckor')} på totalt {gap_km:.0f} km mellan resorna. "
            "Underlaget är därför ett urval och inte en komplett fordonsjournal."
        )
    if single_category and not business_only:
        out.append(
            "Alla resor har samma kategori. Underlaget används som positivt referensmaterial – "
            "det säger inget om hur övrig körning ska klassas."
        )
    if includes_stops:
        out.append(
            f"{_count(includes_stops, 'resa', 'resor')} har kortare körtid än tiden mellan start och slut; "
            "källan har sannolikt redan slagit ihop delsträckor med stopp."
        )
    return out


def file_stored(f: ImportedFile) -> bool:
    """Finns originalfilen kvar på disk?"""
    if not f.stored_path:
        return False
    try:
        return Path(f.stored_path).is_file()
    except OSError:
        return False


def summary_with_storage(db: Session, batch: ImportBatch, summary: dict[str, Any] | None = None) -> dict[str, Any]:
    """Sammanställningen med ``stored`` per fil (om originalfilen finns kvar på disk)."""
    summary = dict((batch.summary or {}) if summary is None else summary)
    stored = {
        str(f.id): file_stored(f)
        for f in db.execute(select(ImportedFile).where(ImportedFile.batch_id == batch.id)).scalars()
    }
    if isinstance(summary.get("files"), list):
        summary["files"] = [
            {**f, "stored": stored.get(str(f.get("id")), False)} if isinstance(f, dict) else f
            for f in summary["files"]
        ]
    return summary


def get_batch(db: Session, user_id: uuid.UUID, batch_id: uuid.UUID) -> ImportBatch:
    batch = db.get(ImportBatch, batch_id)
    if batch is None or batch.user_id != user_id:
        raise ImportError_("Importen hittades inte.", "not_found")
    return batch


def commit(
    db: Session,
    batch: ImportBatch,
    *,
    actor: Actor,
    coverage: str | None = None,
    apply_merge_rules: bool | None = None,
) -> dict[str, Any]:
    """Skapar resor ur förhandsgranskningen. Säker att anropa flera gånger."""
    from app.services.trips import pipeline

    if batch.status == BatchStatus.COMMITTED:
        return {"batch_id": str(batch.id), "imported": 0, "already_committed": True, **(batch.summary or {})}
    if batch.status != BatchStatus.PREVIEW:
        raise ImportError_("Importen kan inte bekräftas i sitt nuvarande läge.", "invalid_state")
    if coverage is not None:
        if coverage not in set(Coverage):
            raise ImportError_("Ogiltig datatäckning.")
        batch.coverage = coverage

    vehicle = db.get(Vehicle, batch.vehicle_id)
    assert vehicle is not None
    known = _existing_keys(db, vehicle.id)
    deleted = _deleted_keys(db, vehicle.id)
    restored = 0
    rows = (
        db.execute(
            select(ImportRow)
            .join(ImportedFile)
            .where(ImportedFile.batch_id == batch.id, ImportRow.status == ImportRowStatus.OK)
            .order_by(ImportRow.row_number)
        )
        .scalars()
        .all()
    )
    rows.sort(key=lambda r: r.parsed.get("started_at") or "")
    tz: ZoneInfo = settings_svc.user_timezone(db, batch.user_id)
    imported = 0
    new_trips: list[LogicalTrip] = []
    location_cache: dict[str, Any] = {}
    created_locations: set[uuid.UUID] = set()

    def loc_for(address: str | None):
        if not address:
            return None
        if address not in location_cache:
            existed = places.find_by_address(db, batch.user_id, address) is not None
            location_cache[address] = places.resolve_address(
                db, batch.user_id, address, created_from="import", is_demo=vehicle.is_demo
            )
            if not existed and location_cache[address] is not None:
                created_locations.add(location_cache[address].id)
        return location_cache[address]

    for row in rows:
        v = row.parsed
        key = row.natural_key
        if key and key in known:  # tillkommit sedan förhandsgranskningen
            row.status = ImportRowStatus.DUPLICATE
            row.messages = [*row.messages, "Resan fanns redan när importen bekräftades."]
            continue
        start_loc, end_loc = loc_for(v.get("start_address")), loc_for(v.get("end_address"))
        has_odo = v.get("odometer_start") is not None and v.get("odometer_end") is not None
        flags: list[str] = []
        if v.get("includes_stops"):
            flags.append("source_includes_stops")
        if not has_odo:
            flags.append("missing_odometer")
        # Endast datum i källan: starttiden är bara känd till dygnet (inget påhittat klockslag).
        start_date_only = v.get("started_kind") in parse.DATE_ONLY_KINDS
        end_date_only = v.get("ended_kind") in parse.DATE_ONLY_KINDS
        day_bounds = _local_day_bounds(v["started_local"], tz) if start_date_only else (None, None)
        raw = RawTrip(
            vehicle_id=vehicle.id,
            source=TripSource.DEMO if vehicle.is_demo else TripSource.VOLVO_IMPORT,
            source_key=key,
            import_row_id=row.id,
            import_batch_id=batch.id,
            started_at=datetime.fromisoformat(v["started_at"]),
            ended_at=datetime.fromisoformat(v["ended_at"]),
            start_time_quality=TimeQuality.BOUNDED if start_date_only else TimeQuality.EXACT,
            end_time_quality=TimeQuality.BOUNDED if end_date_only else TimeQuality.EXACT,
            start_earliest=day_bounds[0] if start_date_only else None,
            start_latest=day_bounds[1] if start_date_only else None,
            odometer_start=v.get("odometer_start"),
            odometer_end=v.get("odometer_end"),
            odometer_source="export" if has_odo else "none",
            distance_km=v["distance_km"],
            distance_quality=DistanceQuality.MEASURED,
            duration_seconds=v.get("duration_seconds"),
            start_address=v.get("start_address"),
            end_address=v.get("end_address"),
            position_source=PositionSource.EXPORT_ADDRESS if v.get("end_address") else PositionSource.NONE,
            start_location_id=start_loc.id if start_loc else None,
            end_location_id=end_loc.id if end_loc else None,
            fuel_liters=v.get("fuel_liters"),
            fuel_quality=ValueQuality.VERIFIED if v.get("fuel_liters") is not None else ValueQuality.UNKNOWN,
            energy_kwh=v.get("energy_kwh"),
            energy_quality=ValueQuality.VERIFIED if v.get("energy_kwh") is not None else ValueQuality.UNKNOWN,
            regen_kwh=v.get("regen_kwh"),
            original_category=v.get("original_category"),
            original_title=v.get("original_title"),
            original_notes=v.get("original_notes"),
            is_complete=has_odo,
            quality_flags=flags,
            updated_by=actor.label,
        )
        seg.set_raw_sort_time(raw)
        db.add(raw)
        db.flush()
        known[key] = raw  # type: ignore[index]

        category = v.get("category", Category.UNDECIDED)
        notes = v.get("original_notes")
        has_purpose = bool(notes and notes.strip())
        # Källmärkt klassning ur originaljournalen. Okänd kategori → granskning.
        review = ReviewStatus.IMPORTED if category != Category.UNDECIDED else ReviewStatus.NEEDS_REVIEW
        trip = seg.create_logical_for_raw(
            db,
            raw,
            category=category,
            category_source=DecisionSource.IMPORT,
            purpose=notes if has_purpose else None,
            purpose_source=DecisionSource.IMPORT if has_purpose else None,
            purpose_confirmed=has_purpose,
            review_status=review,
            actor=actor.label,
        )
        row.status = ImportRowStatus.IMPORTED
        row.raw_trip_id = raw.id
        new_trips.append(trip)
        imported += 1
        if key and key in deleted:
            # En borttagen resa kommer tillbaka – som den står i filen, bedömd på nytt.
            restored += 1
            audit.record(db, actor, "trip.import_restored", "logical_trip", trip.id,
                         after={"previous_raw_trip_id": str(deleted[key].id)})

    batch.status = BatchStatus.COMMITTED
    batch.committed_at = utcnow()
    db.flush()

    # Verifierade originalresor ersätter resor som rekonstruerats ur fordonsrapporter.
    reconciled: dict[str, Any] = {}
    if imported:
        from app.services.volvo import detection

        reconciled = detection.reconcile_with_imports(db, vehicle, actor=actor) or {}

    merge_cfg = settings_svc.get_section(db, batch.user_id, "merge")
    if apply_merge_rules is None:
        apply_merge_rules = bool(merge_cfg["enabled"])
    result = pipeline.after_trips_changed(
        db,
        user_id=batch.user_id,
        vehicle=vehicle,
        actor=actor,
        apply_merge_rules=apply_merge_rules,
        notify=False,  # historisk import ger aldrig pushnotiser
        origin="import",
    )
    summary = dict(batch.summary or {})
    summary.update(
        {
            "imported": imported,
            "restored": restored,
            "auto_merged": result.get("auto_merged", 0),
            "merge_questions": result.get("merge_questions", 0),
            "open_gaps": result.get("open_gaps", 0),
            "open_gap_km": result.get("open_gap_km", 0),
            "locations_created": len([loc for loc in location_cache.values() if loc is not None]),
            # Platser som faktiskt skapades av importen (locations_created räknar alla som användes).
            "locations_new": len(created_locations),
            "reconciled_api_trips": {k: v for k, v in reconciled.items() if isinstance(v, (int, float))},
        }
    )
    batch.summary = summary
    audit.record(
        db, actor, "import.commit", "import_batch", batch.id,
        after={"imported": imported, "restored": restored, "coverage": batch.coverage},
    )
    db.flush()
    return {"batch_id": str(batch.id), "already_committed": False, **summary}


def undo(db: Session, batch: ImportBatch, *, actor: Actor) -> dict[str, Any]:
    """Ångrar en import: resorna makuleras, sammanslagningar som berörs löses upp."""
    from app.services.trips import merge, pipeline

    if batch.status == BatchStatus.UNDONE:
        return {"batch_id": str(batch.id), "voided": 0, "already_undone": True}
    if batch.status == BatchStatus.PREVIEW:
        batch.status = BatchStatus.UNDONE
        batch.undone_at = utcnow()
        return {"batch_id": str(batch.id), "voided": 0, "already_undone": False}
    if batch.status != BatchStatus.COMMITTED:
        raise ImportError_("Importen kan inte ångras i sitt nuvarande läge.", "invalid_state")

    vehicle = db.get(Vehicle, batch.vehicle_id)
    assert vehicle is not None
    raws = (
        db.execute(select(RawTrip).where(RawTrip.import_batch_id == batch.id, RawTrip.voided_at.is_(None)))
        .scalars()
        .all()
    )
    raw_ids = {r.id for r in raws}
    now = utcnow()

    # Lös upp sammanslagningar som innehåller berörda resor (yttersta först).
    for _ in range(50):
        merged = (
            db.execute(
                select(LogicalTrip).where(
                    LogicalTrip.state == LogicalState.ACTIVE,
                    LogicalTrip.segment_count > 1,
                    LogicalTrip.id.in_(
                        select(TripSegment.logical_trip_id).where(TripSegment.raw_trip_id.in_(raw_ids))
                    ),
                )
            )
            .scalars()
            .all()
        )
        if not merged:
            break
        for trip in merged:
            merge.dissolve(db, trip, actor=actor, reason="import_undone", remember=False)
        db.flush()

    trips = (
        db.execute(
            select(LogicalTrip).where(
                LogicalTrip.state != LogicalState.VOIDED,
                LogicalTrip.id.in_(select(TripSegment.logical_trip_id).where(TripSegment.raw_trip_id.in_(raw_ids))),
            )
        )
        .scalars()
        .all()
    )
    for trip in trips:
        trip.state = LogicalState.VOIDED
        trip.updated_by = actor.label
    for raw in raws:
        raw.voided_at = now
        raw.void_reason = "Import ångrad"
        raw.updated_by = actor.label
    for row in db.execute(
        select(ImportRow).join(ImportedFile).where(ImportedFile.batch_id == batch.id, ImportRow.raw_trip_id.is_not(None))
    ).scalars():
        row.status = ImportRowStatus.SKIPPED
        row.messages = [*row.messages, "Importen ångrades."]
    batch.status = BatchStatus.UNDONE
    batch.undone_at = now
    db.flush()
    pipeline.after_trips_changed(
        db, user_id=batch.user_id, vehicle=vehicle, actor=actor, apply_merge_rules=False, notify=False, origin="import_undo"
    )
    audit.record(db, actor, "import.undo", "import_batch", batch.id, after={"voided": len(raws)})
    db.flush()
    return {"batch_id": str(batch.id), "voided": len(raws), "already_undone": False}


def _local_day_bounds(local_iso: str, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Första och sista sekunden (UTC) av det lokala dygn som ``local_iso`` ligger i."""
    from datetime import UTC, time, timedelta

    day = datetime.fromisoformat(local_iso).date()
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz) - timedelta(seconds=1)
    return start.astimezone(UTC), end.astimezone(UTC)


def delete_original_files(db: Session, batch: ImportBatch, *, actor: Actor) -> int:
    """Raderar lagrade originalfiler för en import (resorna påverkas inte)."""
    removed = 0
    for f in db.execute(select(ImportedFile).where(ImportedFile.batch_id == batch.id)).scalars():
        if f.stored_path:
            still_used = db.execute(
                select(ImportedFile.id).where(ImportedFile.stored_path == f.stored_path, ImportedFile.id != f.id).limit(1)
            ).first()
            if not still_used:
                try:
                    Path(f.stored_path).unlink(missing_ok=True)
                except OSError:
                    pass
            f.stored_path = None
            removed += 1
    audit.record(db, actor, "import.delete_files", "import_batch", batch.id, after={"removed": removed})
    return removed
