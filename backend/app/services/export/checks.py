"""Kvalitetskontroller inför export.

En export får bara kallas "Fullständig" när ingen kontroll med allvarlighetsgrad
``error`` slår till. Kontrollerna gör inga antaganden: saknas underlag redovisas det
som en brist, det fylls aldrig i.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ImportBatch, LogicalTrip, Vehicle
from app.models.enums import Coverage, GapStatus, ReviewStatus, TimeQuality, TripSource
from app.services import settings as settings_svc
from app.services.stats import common
from app.services.stats.reports import purpose_missing, purpose_unconfirmed
from app.services.trips import segments as seg

ERROR, WARNING, INFO = "error", "warning", "info"
MAX_IDS = 200


def _check(
    code: str, severity: str, title: str, message: str, trips: Iterable[LogicalTrip] = (), **data: Any
) -> dict[str, Any]:
    ids = [str(t.id) for t in trips]
    return {
        "code": code,
        "severity": severity,
        "title": title,
        "message": message,
        "count": data.pop("count", len(ids)),
        "trip_ids": ids[:MAX_IDS],
        "data": data,
    }


def is_complete(checks: list[dict[str, Any]]) -> bool:
    return not any(c["severity"] == ERROR for c in checks)


def summarize(checks: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "is_complete": is_complete(checks),
        "errors": sum(1 for c in checks if c["severity"] == ERROR),
        "warnings": sum(1 for c in checks if c["severity"] == WARNING),
        "infos": sum(1 for c in checks if c["severity"] == INFO),
    }


def run_checks(
    db: Session,
    *,
    vehicle: Vehicle,
    start: datetime | None,
    end: datetime | None,
    tz: ZoneInfo,
    trip_ids: Iterable[uuid.UUID] | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Kör alla exportkontroller för perioden (eller för ett urval av resor)."""
    user_id = vehicle.owner_user_id
    tol = float(settings_svc.get_section(db, user_id, "merge")["odometer_tolerance_km"])
    selection = trip_ids is not None
    trips = common.load_trips(db, vehicle.id, start, end, trip_ids=trip_ids)
    physical = [t for t in trips if not common.is_gap_fill(t)]
    out: list[dict[str, Any]] = []

    if vehicle.is_demo:
        out.append(
            _check("demo_data", ERROR, "Demonstrationsdata",
                   "Fordonet innehåller påhittade demonstrationsdata. Exporten är inte en riktig körjournal.", count=1)
        )
    if not trips:
        out.append(_check("no_trips", WARNING, "Inga resor", "Perioden innehåller inga journalförda resor.", count=1))

    # --- syften ---------------------------------------------------------------------
    missing = [t for t in trips if purpose_missing(t)]
    if missing:
        out.append(
            _check("missing_purpose", ERROR, "Syfte saknas för tjänsteresor",
                   f"{len(missing)} resor med tjänstekörning saknar ärende/syfte.", missing)
        )
    unconfirmed = [t for t in trips if purpose_unconfirmed(t)]
    if unconfirmed:
        out.append(
            _check("unconfirmed_purpose", ERROR, "Föreslagna syften är inte bekräftade",
                   f"{len(unconfirmed)} resor har ett automatiskt föreslaget syfte som inte bekräftats.", unconfirmed)
        )

    # --- kategorier -----------------------------------------------------------------
    undecided = [t for t in trips if t.undecided_km > common.KM_EPS]
    if undecided:
        km = sum(t.undecided_km for t in undecided)
        out.append(
            _check("undecided_category", ERROR, "Resor utan avgjord kategori",
                   f"{len(undecided)} resor ({km:.1f} km) är varken klassade som arbete eller privat.", undecided,
                   km=round(km, 1))
        )
    preliminary = [
        t for t in trips
        if t.undecided_km <= common.KM_EPS
        and t.review_status in (ReviewStatus.AUTO, ReviewStatus.PROPOSED, ReviewStatus.NEEDS_REVIEW)
    ]
    if preliminary:
        km = sum(t.distance_km for t in preliminary)
        out.append(
            _check("preliminary_classification", ERROR, "Preliminära klassningar är inte godkända",
                   f"{len(preliminary)} resor ({km:.1f} km) har en automatisk klassning som inte godkänts av föraren.",
                   preliminary, km=round(km, 1))
        )

    # --- mätarställningar och sträckor ----------------------------------------------
    no_odo = [t for t in trips if t.odometer_start is None or t.odometer_end is None]
    if no_odo:
        out.append(
            _check("missing_odometer", ERROR, "Mätarställning saknas",
                   f"{len(no_odo)} resor saknar mätarställning vid start eller slut.", no_odo)
        )
    negative = [
        t for t in trips
        if t.distance_km < 0
        or (t.odometer_start is not None and t.odometer_end is not None and t.odometer_end < t.odometer_start)
        or any(s.distance_km < 0 for s in t.segments)
    ]
    if negative:
        out.append(
            _check("negative_distance", ERROR, "Negativa körsträckor",
                   f"{len(negative)} resor har negativ körsträcka eller mätarställning som går bakåt.", negative)
        )
    mismatch = []
    for t in trips:
        seg_sum = sum(s.distance_km for s in t.segments)
        raw_sum = sum(r.distance_km for r in seg.ordered_raws(t))
        cached = t.work_km + t.private_km + t.undecided_km
        if abs(seg_sum - t.distance_km) > common.KM_EPS or abs(raw_sum - seg_sum) > common.KM_EPS or abs(cached - seg_sum) > common.KM_EPS:
            mismatch.append(t)
    if mismatch:
        out.append(
            _check("sum_mismatch", ERROR, "Summeringar stämmer inte",
                   f"{len(mismatch)} resor har delsträckor vars summa inte motsvarar resans körsträcka.", mismatch)
        )
    odo_diff = [
        t for t in physical
        if t.odometer_start is not None and t.odometer_end is not None and not t.has_gap
        and abs((t.odometer_end - t.odometer_start) - t.distance_km) > max(3.0, 0.1 * t.distance_km)
    ]
    if odo_diff:
        out.append(
            _check("odometer_distance_mismatch", WARNING, "Körsträcka avviker från mätarställning",
                   f"{len(odo_diff)} resor har en körsträcka som avviker tydligt från skillnaden i mätarställning.",
                   odo_diff)
        )

    # --- överlapp och dubbletter ----------------------------------------------------
    timed = [t for t in physical if t.started_at is not None and t.ended_at is not None]
    overlaps: list[LogicalTrip] = []
    for a, b in zip(timed, timed[1:], strict=False):
        if (b.started_at - a.ended_at).total_seconds() < -60:  # type: ignore[operator]
            overlaps += [a, b]
    if overlaps:
        uniq = list({t.id: t for t in overlaps}.values())
        out.append(
            _check("overlapping_trips", ERROR, "Överlappande resor",
                   f"{len(uniq)} resor överlappar varandra i tid.", uniq)
        )
    seen: dict[tuple[Any, ...], LogicalTrip] = {}
    dups: list[LogicalTrip] = []
    for t in physical:
        if t.started_at is None:
            continue
        key = (t.started_at.replace(second=0, microsecond=0), t.odometer_start, t.odometer_end, round(t.distance_km, 1))
        if key in seen:
            dups += [seen[key], t]
        else:
            seen[key] = t
    if dups:
        uniq = list({t.id: t for t in dups}.values())
        out.append(_check("duplicates", ERROR, "Dubbletter", f"{len(uniq)} resor ser ut att vara dubbletter.", uniq))

    # --- datakvalitet ---------------------------------------------------------------
    uncertain = []
    for t in physical:
        flags = set(t.quality_flags or [])
        if (
            not t.is_complete
            or flags & {"possible_missed_trips", "start_time_uncertain", "end_time_uncertain"}
            or t.start_time_quality in (TimeQuality.BOUNDED, TimeQuality.UNKNOWN, TimeQuality.ESTIMATED)
            or t.source == TripSource.VOLVO_API
        ):
            uncertain.append(t)
    if uncertain:
        out.append(
            _check("uncertain_quality", WARNING, "Resor med osäker datakvalitet",
                   f"{len(uncertain)} resor är rekonstruerade eller har osäkra tidpunkter/uppgifter.", uncertain)
        )
    summarized = [t for t in trips if common.is_gap_fill(t)]
    if summarized:
        km = sum(t.distance_km for t in summarized)
        out.append(
            _check("summarized_private", INFO, "Sammanfattad privat körning",
                   f"{len(summarized)} poster ({km:.1f} km) är privat körning som föraren sammanfattat ur luckor i "
                   "mätarställningen – utan detaljerade resor.", summarized, km=round(km, 1))
        )
    merged_gap = [t for t in trips if t.has_gap]
    if merged_gap:
        out.append(
            _check("merged_with_gap", WARNING, "Sammanslagna resor med lucka",
                   f"{len(merged_gap)} sammanslagna resor har en lucka i mätarställningen mellan delresorna.", merged_gap)
        )

    # --- avstämning av perioden (endast vid periodexport, inte vid urval) ------------
    if not selection:
        gaps = common.gaps_in_period(db, vehicle.id, start, end)
        open_g = [g for g in gaps if g.status == GapStatus.OPEN]
        ignored = [g for g in gaps if g.status == GapStatus.IGNORED]
        if open_g:
            km = sum(g.km for g in open_g)
            out.append(
                _check("open_gaps", ERROR, "Oavstämda kilometer (dataluckor)",
                       f"{len(open_g)} luckor på totalt {km:.0f} km i mätarställningen saknar journalpost. "
                       "Körjournalen är inte komplett för perioden.", count=len(open_g), km=round(km, 1),
                       gap_ids=[str(g.id) for g in open_g][:MAX_IDS])
            )
        if ignored:
            km = sum(g.km for g in ignored)
            out.append(
                _check("ignored_gaps", WARNING, "Medvetet lämnade luckor",
                       f"{len(ignored)} luckor ({km:.0f} km) har markerats som lämnade utan åtgärd och är fortsatt "
                       "oavstämda.", count=len(ignored), km=round(km, 1))
            )

        odo_start = common.boundary_odometer(db, vehicle, start, tolerance_km=tol, now=now)
        odo_end = common.boundary_odometer(db, vehicle, end, tolerance_km=tol, now=now)
        missing_bounds = []
        if start is None or odo_start.value is None:
            missing_bounds.append("början")
        if end is None or odo_end.value is None:
            missing_bounds.append("slut")
        if missing_bounds:
            out.append(
                _check("incomplete_period", ERROR, "Mätarställning vid periodens gräns saknas",
                       "Mätarställning vid periodens " + " och ".join(missing_bounds) + " är inte känd. "
                       "Registrera en avläsning (t.ex. vid årsskiftet) för att kunna stämma av perioden.",
                       count=len(missing_bounds), missing=missing_bounds)
            )
        elif odo_start.value is not None and odo_end.value is not None:
            delta = odo_end.value - odo_start.value
            journal = sum(t.distance_km for t in trips)
            diff = delta - journal
            open_km = sum(g.km for g in open_g)
            # Luckorna redovisas redan ovan; här fångas det som inte förklaras av dem.
            unexplained = diff - open_km - sum(g.km for g in ignored)
            # Mätarställningen är heltal och resornas sträckor decimaltal – tillåt en liten avvikelse.
            if abs(unexplained) > max(tol, 0.005 * max(delta, 0)) + 1.0:
                out.append(
                    _check("odometer_reconciliation", ERROR, "Mätarställning och journal stämmer inte",
                           f"Mätarställningen ändras {delta:.0f} km under perioden men journalen redovisar "
                           f"{journal:.0f} km. {abs(unexplained):.0f} km är oförklarade.", count=1,
                           odometer_km=round(delta, 1), journal_km=round(journal, 1), unexplained_km=round(unexplained, 1))
                )

        batch_ids = {r.import_batch_id for t in trips for r in seg.ordered_raws(t) if r.import_batch_id is not None}
        if batch_ids:
            coverages = {c for (c,) in db.execute(select(ImportBatch.coverage).where(ImportBatch.id.in_(batch_ids))).all()}
            if coverages & {Coverage.SELECTION, Coverage.UNKNOWN, Coverage.INCOMPLETE}:
                out.append(
                    _check("import_selection", WARNING if open_g else INFO, "Importerat urval",
                           "Hela eller delar av underlaget är ett importerat urval av resor, inte en komplett "
                           "fordonsjournal." + (" Luckorna ovan visar vad som saknas." if open_g else
                                                " Alla luckor i mätarställningen är hanterade."), count=1)
                )
    else:
        out.append(
            # Ett urval kan aldrig stämmas av mot mätarställningen och får därför inte kallas fullständigt.
            _check("selection_export", ERROR, "Export av valda resor",
                   "Exporten innehåller endast ett urval av resor och kan inte stämmas av mot mätarställningen.", count=1)
        )

    order = {ERROR: 0, WARNING: 1, INFO: 2}
    out.sort(key=lambda c: order[c["severity"]])
    return out
