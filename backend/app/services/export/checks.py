"""Kvalitetskontroller inför export.

En export får bara kallas "Fullständig" när ingen kontroll med allvarlighetsgrad
``error`` slår till. Kontrollerna gör inga antaganden: saknas underlag redovisas det
som en brist, det fylls aldrig i.

Kontrollerna beror på journalens omfång (inställningen ``general.journal_scope``):

* ``complete`` – fullständig körjournal. All körning ska vara journalförd, så luckor i
  mätarställningen och oavstämda perioder är fel.
* ``business_only`` – egen bil med milersättning. Bara godkända tjänsteresor ingår i
  underlaget. Körningen mellan dem är privat och ger inga anmärkningar; det gör inte
  privata resor heller. Fel är sådant som gör de redovisade tjänsteresorna felaktiga.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ImportBatch, KnownLocation, LogicalTrip, Vehicle
from app.models.enums import (
    Category,
    Coverage,
    GapStatus,
    LocationType,
    ReviewStatus,
    TimeQuality,
    TripSource,
)
from app.services import settings as settings_svc
from app.services.places.normalize import clean
from app.services.stats import common
from app.services.stats.reports import purpose_missing, purpose_unconfirmed
from app.services.trips import segments as seg

ERROR, WARNING, INFO = "error", "warning", "info"
MAX_IDS = 200
LIVE_SOURCES = (TripSource.VOLVO_API, TripSource.DEMO)
_UNKNOWN_START = (TimeQuality.BOUNDED, TimeQuality.UNKNOWN)


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


def status_label(checks: list[dict[str, Any]], scope: str) -> str:
    """Kort statusetikett. Ordet "fullständig" används bara för en fullständig körjournal."""
    complete = is_complete(checks)
    if scope == settings_svc.SCOPE_BUSINESS_ONLY:
        return "Klart att redovisa" if complete else "Fel behöver åtgärdas"
    return "Fullständig" if complete else "OFULLSTÄNDIG"


def summarize(checks: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "is_complete": is_complete(checks),
        "errors": sum(1 for c in checks if c["severity"] == ERROR),
        "warnings": sum(1 for c in checks if c["severity"] == WARNING),
        "infos": sum(1 for c in checks if c["severity"] == INFO),
    }


def _is_negative(t: LogicalTrip) -> bool:
    return (
        t.distance_km < 0
        or (t.odometer_start is not None and t.odometer_end is not None and t.odometer_end < t.odometer_start)
        or any(s.distance_km < 0 for s in t.segments)
    )


def _sum_mismatch(t: LogicalTrip) -> bool:
    seg_sum = sum(s.distance_km for s in t.segments)
    raw_sum = sum(r.distance_km for r in seg.ordered_raws(t))
    cached = t.work_km + t.private_km + t.undecided_km
    return (
        abs(seg_sum - t.distance_km) > common.KM_EPS
        or abs(raw_sum - seg_sum) > common.KM_EPS
        or abs(cached - seg_sum) > common.KM_EPS
    )


def _odometer_differs(t: LogicalTrip) -> bool:
    return (
        t.odometer_start is not None and t.odometer_end is not None and not t.has_gap
        and abs((t.odometer_end - t.odometer_start) - t.distance_km) > max(3.0, 0.1 * t.distance_km)
    )


def _uncertain(t: LogicalTrip) -> bool:
    flags = set(t.quality_flags or [])
    return bool(
        not t.is_complete
        or flags & {"possible_missed_trips", "start_time_uncertain", "end_time_uncertain"}
        or t.start_time_quality in (TimeQuality.BOUNDED, TimeQuality.UNKNOWN, TimeQuality.ESTIMATED)
        or t.source == TripSource.VOLVO_API
    )


def _overlap_pairs(physical: list[LogicalTrip]) -> list[tuple[LogicalTrip, LogicalTrip]]:
    """På varandra följande resor som överlappar i tid (mer än en minut)."""
    timed = [t for t in physical if t.started_at is not None and t.ended_at is not None]
    return [
        (a, b) for a, b in zip(timed, timed[1:], strict=False)
        if (b.started_at - a.ended_at).total_seconds() < -60  # type: ignore[operator]
    ]


def _duplicate_pairs(physical: list[LogicalTrip]) -> list[tuple[LogicalTrip, LogicalTrip]]:
    """Resor med samma starttid (minut), mätarställning och körsträcka."""
    seen: dict[tuple[Any, ...], LogicalTrip] = {}
    pairs: list[tuple[LogicalTrip, LogicalTrip]] = []
    for t in physical:
        if t.started_at is None:
            continue
        key = (t.started_at.replace(second=0, microsecond=0), t.odometer_start, t.odometer_end, round(t.distance_km, 1))
        if key in seen:
            pairs.append((seen[key], t))
        else:
            seen[key] = t
    return pairs


def _unique(pairs: Iterable[tuple[LogicalTrip, LogicalTrip]]) -> list[LogicalTrip]:
    return list({t.id: t for pair in pairs for t in pair}.values())


def _has_work(t: LogicalTrip) -> bool:
    """Resan innehåller tjänstekörning (hela resan eller en delsträcka)."""
    return t.work_km > common.KM_EPS or any(seg.segment_category(t, s) == Category.WORK for s in t.segments)


def _business_only_checks(db: Session, vehicle: Vehicle, trips: list[LogicalTrip]) -> list[dict[str, Any]]:
    """Kontroller när endast tjänsteresor redovisas (egen bil, milersättning).

    Underlaget består av tjänsteresor som är godkända eller importerade. Luckor i
    mätarställningen, privata resor och urval av resor ger inga anmärkningar.
    """
    out: list[dict[str, Any]] = []
    physical = [t for t in trips if not common.is_gap_fill(t)]
    business = [t for t in trips if _has_work(t)]
    reported = [t for t in business if t.review_status in common.REPORTED]
    reported_ids = {t.id for t in reported}

    if vehicle.is_demo:
        out.append(
            _check("demo_data", ERROR, "Demonstrationsdata",
                   "Fordonet innehåller påhittade demonstrationsdata. Exporten är inte ett riktigt underlag.", count=1)
        )
    if not reported:
        out.append(
            _check("no_trips", WARNING, "Inga tjänsteresor att redovisa",
                   "Perioden innehåller inga resor." if not trips
                   else "Perioden innehåller inga godkända tjänsteresor.", count=1)
        )

    # --- sådant som ännu inte ingår i underlaget (blockerar inte) ---------------------
    undecided = [t for t in trips if t.undecided_km > common.KM_EPS]
    if undecided:
        km = sum(t.undecided_km for t in undecided)
        out.append(
            _check("undecided_category", WARNING, "Resor utan avgjord kategori",
                   f"{len(undecided)} resor ({km:.1f} km) är varken klassade som arbete eller privat. "
                   "De tas inte med i underlaget förrän kategorin har avgjorts.", undecided, km=round(km, 1))
        )
    preliminary = [
        t for t in business if t.review_status not in common.REPORTED and t.undecided_km <= common.KM_EPS
    ]
    if preliminary:
        km = sum(t.work_km for t in preliminary)
        out.append(
            _check("preliminary_classification", WARNING, "Preliminära klassningar är inte godkända",
                   f"{len(preliminary)} tjänsteresor ({km:.1f} km) har en preliminär klassning som inte godkänts "
                   "av föraren. De tas inte med i underlaget förrän de har godkänts.", preliminary, km=round(km, 1))
        )

    # --- fel i de redovisade tjänsteresorna -------------------------------------------
    missing = [t for t in reported if purpose_missing(t)]
    if missing:
        out.append(
            _check("missing_purpose", ERROR, "Syfte saknas för tjänsteresor",
                   f"{len(missing)} godkända tjänsteresor saknar ärende/syfte.", missing)
        )
    unconfirmed = [t for t in reported if purpose_unconfirmed(t)]
    if unconfirmed:
        out.append(
            _check("unconfirmed_purpose", ERROR, "Föreslagna syften är inte bekräftade",
                   f"{len(unconfirmed)} godkända tjänsteresor har ett automatiskt föreslaget syfte som inte "
                   "bekräftats.", unconfirmed)
        )
    negative = [t for t in reported if _is_negative(t)]
    if negative:
        out.append(
            _check("negative_distance", ERROR, "Negativa körsträckor",
                   f"{len(negative)} tjänsteresor har negativ körsträcka eller mätarställning som går bakåt.",
                   negative)
        )
    mismatch = [t for t in reported if _sum_mismatch(t)]
    if mismatch:
        out.append(
            _check("sum_mismatch", ERROR, "Summeringar stämmer inte",
                   f"{len(mismatch)} tjänsteresor har delsträckor vars summa inte motsvarar resans körsträcka.",
                   mismatch)
        )
    # Överlapp och dubbletter räknas när minst en av resorna ingår i underlaget.
    overlaps = _unique((a, b) for a, b in _overlap_pairs(physical) if a.id in reported_ids or b.id in reported_ids)
    if overlaps:
        out.append(
            _check("overlapping_trips", ERROR, "Överlappande resor",
                   f"{len(overlaps)} resor överlappar varandra i tid, varav minst en redovisad tjänsteresa.",
                   overlaps)
        )
    dups = _unique((a, b) for a, b in _duplicate_pairs(physical) if a.id in reported_ids or b.id in reported_ids)
    if dups:
        out.append(
            _check("duplicates", ERROR, "Dubbletter",
                   f"{len(dups)} resor ser ut att vara dubbletter, varav minst en redovisad tjänsteresa.", dups)
        )

    # --- upplysningar om de redovisade tjänsteresorna ---------------------------------
    no_odo = [t for t in reported if t.odometer_start is None or t.odometer_end is None]
    if no_odo:
        out.append(
            _check("missing_odometer", WARNING, "Mätarställning saknas",
                   f"{len(no_odo)} tjänsteresor saknar mätarställning vid start eller slut. Körsträckan redovisas "
                   "ändå, men mätarställningen gör den lättare att styrka.", no_odo)
        )
    odo_diff = [t for t in reported if not common.is_gap_fill(t) and _odometer_differs(t)]
    if odo_diff:
        out.append(
            _check("odometer_distance_mismatch", WARNING, "Körsträcka avviker från mätarställning",
                   f"{len(odo_diff)} tjänsteresor har en körsträcka som avviker tydligt från skillnaden i "
                   "mätarställning.", odo_diff)
        )
    out += reported_quality_checks(db, reported)
    return out


def reported_quality_checks(db: Session, reported: list[LogicalTrip]) -> list[dict[str, Any]]:
    """Upplysningar och fel om de redovisade raderna (byggkontraktet C.6).

    Ersätter den tidigare samlade anmärkningen om osäker datakvalitet, som flaggade varje
    resa från bilen. Varje kontroll säger nu vad som faktiskt saknas eller är uträknat.
    """
    out: list[dict[str, Any]] = []
    rows = [t for t in reported if not common.is_gap_fill(t)]
    missed = [t for t in rows if not t.is_complete or "possible_missed_trips" in (t.quality_flags or [])]
    if missed:
        out.append(
            _check("possible_missed_trips", WARNING, "Kan vara flera körningar",
                   f"{len(missed)} tjänsteresor kan innehålla fler körningar än en, eller saknar uppgifter från "
                   "bilen under resan.", missed)
        )
    no_start = [t for t in rows if t.started_at is None or t.start_time_quality in _UNKNOWN_START]
    if no_start:
        out.append(
            _check("missing_start_time", WARNING, "Starttid saknas",
                   f"{len(no_start)} tjänsteresor saknar starttid. Underlaget visar då bara sluttiden "
                   "(till exempel –08:52). Du kan ange starttiden på resan.", no_start)
        )
    estimated = [t for t in rows if TimeQuality.ESTIMATED in (t.start_time_quality, t.end_time_quality)]
    if estimated:
        out.append(
            _check("estimated_times", INFO, "Uträknade tider",
                   f"{len(estimated)} tjänsteresor har en tid som är uträknad ur bilens egna uppgifter "
                   "(en okänd starttid uppskattas ur sträckan). "
                   "Den står som \"ca\" i underlaget.", estimated)
        )
    no_address = [t for t in rows if not clean(t.start_address) or not clean(t.end_address)]
    if no_address:
        out.append(
            _check("missing_address", WARNING, "Adress saknas",
                   f"{len(no_address)} tjänsteresor saknar adress för start eller mål.", no_address)
        )
    private = _private_address_trips(db, rows)
    if private:
        out.append(
            _check("private_address_in_report", ERROR, "Privat adress i underlaget",
                   f"{len(private)} tjänsteresor startar eller slutar vid en plats som du markerat som privat. "
                   "Ändra resan eller platsen innan underlaget skapas.", private)
        )
    return out


def _private_address_trips(db: Session, trips: list[LogicalTrip]) -> list[LogicalTrip]:
    """Resor från bilen vars start eller mål är en privat plats (hemmet får stå i underlaget).

    Gäller bara resor från bilen: importerade och manuellt inlagda resor bedöms som i dag.
    """
    live = [t for t in trips if t.source in LIVE_SOURCES]
    ids = {i for t in live for i in (t.start_location_id, t.end_location_id) if i is not None}
    if not ids:
        return []
    private_ids = set(
        db.execute(
            select(KnownLocation.id).where(KnownLocation.id.in_(ids), KnownLocation.type == LocationType.PRIVATE)
        ).scalars()
    )
    return [t for t in live if t.start_location_id in private_ids or t.end_location_id in private_ids]


def _sorted(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    order = {ERROR: 0, WARNING: 1, INFO: 2}
    checks.sort(key=lambda c: order[c["severity"]])
    return checks


def run_checks(
    db: Session,
    *,
    vehicle: Vehicle,
    start: datetime | None,
    end: datetime | None,
    tz: ZoneInfo,
    trip_ids: Iterable[uuid.UUID] | None = None,
    now: datetime | None = None,
    scope: str | None = None,
) -> list[dict[str, Any]]:
    """Kör alla exportkontroller för perioden (eller för ett urval av resor).

    ``scope`` är journalens omfång (``business_only`` eller ``complete``). Utelämnas det
    används användarens inställning ``general.journal_scope``.
    """
    user_id = vehicle.owner_user_id
    if scope is None:
        scope = settings_svc.journal_scope(db, user_id)
    if scope not in settings_svc.JOURNAL_SCOPES:
        raise ValueError(f"Okänt journalomfång: {scope}")
    selection = trip_ids is not None
    trips = common.load_trips(db, vehicle.id, start, end, trip_ids=trip_ids)
    if scope == settings_svc.SCOPE_BUSINESS_ONLY:
        return _sorted(_business_only_checks(db, vehicle, trips))

    tol = float(settings_svc.get_section(db, user_id, "merge")["odometer_tolerance_km"])
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
    negative = [t for t in trips if _is_negative(t)]
    if negative:
        out.append(
            _check("negative_distance", ERROR, "Negativa körsträckor",
                   f"{len(negative)} resor har negativ körsträcka eller mätarställning som går bakåt.", negative)
        )
    mismatch = [t for t in trips if _sum_mismatch(t)]
    if mismatch:
        out.append(
            _check("sum_mismatch", ERROR, "Summeringar stämmer inte",
                   f"{len(mismatch)} resor har delsträckor vars summa inte motsvarar resans körsträcka.", mismatch)
        )
    odo_diff = [t for t in physical if _odometer_differs(t)]
    if odo_diff:
        out.append(
            _check("odometer_distance_mismatch", WARNING, "Körsträcka avviker från mätarställning",
                   f"{len(odo_diff)} resor har en körsträcka som avviker tydligt från skillnaden i mätarställning.",
                   odo_diff)
        )

    # --- överlapp och dubbletter ----------------------------------------------------
    overlaps = _unique(_overlap_pairs(physical))
    if overlaps:
        out.append(
            _check("overlapping_trips", ERROR, "Överlappande resor",
                   f"{len(overlaps)} resor överlappar varandra i tid.", overlaps)
        )
    dups = _unique(_duplicate_pairs(physical))
    if dups:
        out.append(_check("duplicates", ERROR, "Dubbletter", f"{len(dups)} resor ser ut att vara dubbletter.", dups))

    # --- datakvalitet ---------------------------------------------------------------
    uncertain = [t for t in physical if _uncertain(t)]
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

    return _sorted(out)
