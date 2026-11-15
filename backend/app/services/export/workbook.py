"""Excel-körjournal (XlsxWriter).

Bladen följer Skatteverkets rekommenderade uppgifter för körjournal:
registreringsnummer, år, mätarställning vid årets början och slut, datum och
mätarställning vid resans start och ankomst, reslängd, start- och slutadress,
ärende/syfte, besökta platser/företag/kontaktpersoner samt anteckningar om förare
och tankning.

Summeringar skrivs som riktiga Excel-formler MED förberäknade värden, så att filen
visar rätt även i läsare som inte räknar om formler. All text från användare och
importerade filer skrivs som ren text (skydd mot formula injection).
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import xlsxwriter
from xlsxwriter.utility import xl_rowcol_to_cell

from app.models import (
    AuditEvent,
    ChargingRecord,
    DataGap,
    FuelRecord,
    KnownLocation,
    LogicalTrip,
    Vehicle,
)
from app.models.enums import (
    TRIP_SOURCE_LABELS_SV,
    GapStatus,
    LocationType,
    ReviewStatus,
    TimeQuality,
    TripSource,
)
from app.services.places.normalize import parse_address
from app.services.stats import common
from app.services.stats.common import EnergyAcc, OdometerReading, Period
from app.services.trips import segments as seg

SHEET_OVERVIEW = "Översikt"
SHEET_JOURNAL = "Körjournal"
SHEET_MONTHLY = "Månadssammanställning"
SHEET_ENERGY = "Bränsle och laddning"
SHEET_QUALITY = "Datakvalitet och avvikelser"
SHEET_SOURCES = "Källa och ändringshistorik"

STATUS_COMPLETE = "Fullständig"
STATUS_INCOMPLETE = "OFULLSTÄNDIG – se bladet Datakvalitet och avvikelser"
# Omfånget business_only (endast tjänsteresor redovisas): ordet "fullständig" används inte, och
# luckor i mätarställningen är privat körning – aldrig en brist.
STATUS_READY = "Klart att redovisa"
STATUS_NEEDS_FIX = "Fel behöver åtgärdas – se bladet Datakvalitet och avvikelser"
SCOPE_COMPLETE = "complete"
SCOPE_BUSINESS_ONLY = "business_only"
DEMO_WATERMARK = "DEMODATA – påhittade uppgifter, inte en riktig körjournal"
MISSING = "Uppgift saknas"

JOURNAL_HEADER_ROW = 3  # 0-baserat (rad 4 i Excel)
JOURNAL_COLUMNS: list[tuple[str, str, float]] = [
    ("date", "Datum", 11),
    ("start", "Starttid", 9),
    ("end", "Sluttid", 9),
    ("start_address", "Resans start, adress", 30),
    ("end_address", "Resans slut, adress", 30),
    ("odo_start", "Mätarställning start (km)", 13),
    ("odo_end", "Mätarställning ankomst (km)", 13),
    ("km", "Reslängd (km)", 10),
    ("category", "Kategori", 11),
    ("work_km", "Tjänst (km)", 10),
    ("private_km", "Privat (km)", 10),
    ("undecided_km", "Ej avgjort (km)", 10),
    ("purpose", "Ärende/syfte", 38),
    ("contacts", "Plats/företag/kontaktperson", 28),
    ("driver", "Förare", 16),
    ("notes", "Anteckningar (bilförare, tankning m.m.)", 42),
    ("segments", "Delresor", 8),
    ("source", "Datakälla", 26),
    ("review", "Granskningsstatus", 24),
    ("fuel", "Bensin (l)", 9),
    ("energy", "El (kWh)", 9),
    ("month", "Månad", 9),
    ("trip_id", "Rese-ID", 10),
]
COL = {key: i for i, (key, _, _) in enumerate(JOURNAL_COLUMNS)}

CATEGORY_LABELS = {"work": "Arbete", "private": "Privat", "undecided": "Ej avgjord", "mixed": "Blandad"}
REVIEW_LABELS = {
    ReviewStatus.APPROVED: "Godkänd",
    ReviewStatus.IMPORTED: "Importerad klassning",
    ReviewStatus.AUTO: "Automatiskt klassad – ej godkänd",
    ReviewStatus.PROPOSED: "Förslag – ej godkänt",
    ReviewStatus.NEEDS_REVIEW: "Behöver granskas",
}
SOURCE_LABELS = {**{str(k): v for k, v in TRIP_SOURCE_LABELS_SV.items()}, "mixed": "Flera datakällor"}
GAP_STATUS_LABELS = {
    GapStatus.OPEN: "Öppen – saknar journalpost",
    GapStatus.RESOLVED_PRIVATE: "Sammanfattad som privat körning",
    GapStatus.RESOLVED_FILLED: "Kompletterad med resor",
    GapStatus.IGNORED: "Lämnad utan åtgärd (oavstämd)",
}
SEVERITY_LABELS = {"error": "Fel", "warning": "Varning", "info": "Information"}
AUDIT_LABELS = {
    "trip.update": "Ändrad",
    "trip.merge": "Sammanslagen",
    "trip.unmerge": "Sammanslagning ångrad",
    "trip.split": "Uppdelad",
    "trip.split_undo": "Uppdelning ångrad",
    "trip.undo_change": "Ändring ångrad",
    "trip.merge_declined": "Sammanslagning avböjd",
}
_INJECTION_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def safe_text(value: Any) -> str:
    """Text som aldrig kan tolkas som formel: riskabla inledningstecken neutraliseras."""
    if value is None:
        return ""
    text = str(value).replace("\x00", "")
    if text.startswith(_INJECTION_PREFIXES):
        return "'" + text
    return text


@dataclass
class ExportContext:
    vehicle: Vehicle
    tz: ZoneInfo
    period: Period
    trips: list[LogicalTrip]
    checks: list[dict[str, Any]]
    is_complete: bool
    odometer_start: OdometerReading
    odometer_end: OdometerReading
    driver_name: str = ""
    employer_name: str = ""
    drivers: dict[uuid.UUID, str] = field(default_factory=dict)
    locations: dict[uuid.UUID, KnownLocation] = field(default_factory=dict)
    gaps: list[DataGap] = field(default_factory=list)
    fuel_records: list[FuelRecord] = field(default_factory=list)
    charging_records: list[ChargingRecord] = field(default_factory=list)
    monthly_energy: list[dict[str, Any]] = field(default_factory=list)
    sources: dict[uuid.UUID, dict[str, Any]] = field(default_factory=dict)  # raw_trip_id → {filename, row}
    audit_events: list[AuditEvent] = field(default_factory=list)
    include_private_addresses: bool = False
    include_change_history: bool = True
    selection: bool = False
    generated_at: datetime | None = None
    scope: str = SCOPE_COMPLETE  # general.journal_scope: complete | business_only

    @property
    def business_only(self) -> bool:
        return self.scope == SCOPE_BUSINESS_ONLY


def status_text(ctx: ExportContext, *, short: bool = False) -> str:
    """Statusrutans text enligt de omfångsmedvetna kontrollerna (``ctx.is_complete``)."""
    if ctx.business_only:
        if ctx.is_complete:
            return STATUS_READY
        return "Fel behöver åtgärdas" if short else STATUS_NEEDS_FIX
    if ctx.is_complete:
        return STATUS_COMPLETE
    return "OFULLSTÄNDIG" if short else STATUS_INCOMPLETE


# ----------------------------------------------------------------------------- radmodell


def short_id(value: uuid.UUID | str) -> str:
    return str(value)[:8]


def _naive_local(dt: datetime | None, tz: ZoneInfo) -> datetime | None:
    return dt.astimezone(tz).replace(tzinfo=None) if dt is not None else None


def _private_only(trip: LogicalTrip) -> bool:
    return trip.private_km > common.KM_EPS and trip.work_km <= common.KM_EPS and trip.undecided_km <= common.KM_EPS


def _address(ctx: ExportContext, trip: LogicalTrip, address: str | None, loc_id: uuid.UUID | None) -> str:
    if common.is_gap_fill(trip):
        return ""
    if _private_only(trip) and not ctx.include_private_addresses:
        loc = ctx.locations.get(loc_id) if loc_id else None
        city = (loc.city if loc is not None else None) or parse_address(address).city
        return f"Privat resa ({city})" if city else "Privat resa"
    return address or ""


def _contacts(ctx: ExportContext, trip: LogicalTrip) -> str:
    """Besökta platser/företag/kontaktpersoner – behövs inte vid privata resor."""
    if trip.work_km <= common.KM_EPS:
        return ""
    if trip.contacts:
        return trip.contacts
    loc = ctx.locations.get(trip.end_location_id) if trip.end_location_id else None
    if loc is not None and loc.confirmed and loc.type in (LocationType.CUSTOMER, LocationType.OFFICE, LocationType.REGULAR_WORKPLACE):
        return loc.company or loc.name or ""
    return ""


def _notes(ctx: ExportContext, trip: LogicalTrip, refuels: list[FuelRecord]) -> str:
    tz = ctx.tz
    parts: list[str] = []
    if trip.notes:
        parts.append(trip.notes)
    raws = seg.ordered_raws(trip)
    if common.is_gap_fill(trip):
        raw = raws[0] if raws else None
        lo = _naive_local(raw.start_earliest, tz) if raw else None
        hi = _naive_local(raw.start_latest, tz) if raw else None
        if lo and hi:
            parts.append(f"Tidpunkt okänd – mellan {lo:%Y-%m-%d %H:%M} och {hi:%Y-%m-%d %H:%M}")
        else:
            parts.append("Tidpunkt okänd")
        parts.append("Sammanfattad post utan detaljerade resor")
    else:
        if trip.started_at is None:
            first = raws[0] if raws else None
            lo = _naive_local(first.start_earliest, tz) if first else None
            hi = _naive_local(first.start_latest, tz) if first else None
            if lo and hi:
                parts.append(f"Starttid okänd – mellan {lo:%Y-%m-%d %H:%M} och {hi:%Y-%m-%d %H:%M}")
            else:
                parts.append("Starttid okänd")
        elif trip.start_time_quality in (TimeQuality.ESTIMATED, TimeQuality.BOUNDED):
            parts.append("Starttiden är uppskattad")
        if trip.ended_at is None:
            parts.append("Sluttid okänd")
    if len(raws) > 1:
        vias = []
        for stop in trip.via or []:
            if _private_only(trip) and not ctx.include_private_addresses:
                continue
            label = stop.get("address") or "okänd plats"
            minutes = stop.get("stop_minutes")
            vias.append(f"{label} ({minutes:.0f} min)" if minutes is not None else label)
        parts.append(f"Sammanslagen resa, {len(raws)} delresor" + (": via " + ", ".join(vias) if vias else ""))
    if trip.has_gap:
        parts.append(f"OBS: {trip.gap_km:.0f} km lucka i mätarställningen mellan delresorna")
    if trip.has_mixed_categories:
        parts.append("Blandad privat/tjänst – se delsträckor i bladet Källa och ändringshistorik")
    flags = set(trip.quality_flags or [])
    if "possible_missed_trips" in flags:
        parts.append("Flera körningar kan ha skett – uppgifterna är ofullständiga")
    if "source_includes_stops" in flags:
        parts.append("Källan har slagit ihop delsträckor med stopp")
    for r in refuels:
        liters = f"{r.liters:.1f} l".replace(".", ",") if r.liters is not None else "okänd volym"
        cost = f", {r.total_cost:.0f} kr" if r.total_cost is not None else ""
        est = " (uppskattad volym)" if r.liters_quality != "verified" else ""
        parts.append(f"Tankning {liters}{cost}{est}")
    return "; ".join(p for p in parts if p)


def trip_energy(trip: LogicalTrip) -> tuple[EnergyAcc, EnergyAcc]:
    fuel, energy = EnergyAcc(), EnergyAcc()
    for raw in seg.ordered_raws(trip):
        if raw.source == TripSource.GAP_FILL:
            fuel.add(None, "unknown", raw.distance_km)
            energy.add(None, "unknown", raw.distance_km)
        else:
            fuel.add(raw.fuel_liters, raw.fuel_quality, raw.distance_km)
            energy.add(raw.energy_kwh, raw.energy_quality, raw.distance_km)
    return fuel, energy


def journal_rows(ctx: ExportContext) -> list[dict[str, Any]]:
    """En rad per journalresa – samma värden skrivs till bladet och används för summor."""
    tz = ctx.tz
    refuels_by_trip: dict[uuid.UUID, list[FuelRecord]] = defaultdict(list)
    timed = [t for t in ctx.trips if t.started_at is not None and t.ended_at is not None]
    for r in ctx.fuel_records:
        for t in timed:
            if t.started_at <= r.at <= t.ended_at + timedelta(minutes=30):  # type: ignore[operator]
                refuels_by_trip[t.id].append(r)
                break
    rows = []
    for t in ctx.trips:
        start, end = _naive_local(t.started_at, tz), _naive_local(t.ended_at, tz)
        when = _naive_local(common.trip_time(t), tz)
        fuel, energy = trip_energy(t)
        kind = common.trip_kind(t)
        km = common.segment_km(t)  # kilometer per delsträcka – facit för summorna
        rows.append(
            {
                "trip": t,
                "date": (start or end).date() if (start or end) else None,
                "start": start,
                "end": end,
                "end_other_day": bool(start and end and end.date() != start.date()),
                "start_address": _address(ctx, t, t.start_address, t.start_location_id),
                "end_address": _address(ctx, t, t.end_address, t.end_location_id),
                "odo_start": t.odometer_start,
                "odo_end": t.odometer_end,
                "km": round(km["work"] + km["private"] + km["undecided"], 3),
                "category": CATEGORY_LABELS.get(kind, CATEGORY_LABELS["undecided"]),
                "kind": kind,
                "work_km": km["work"],
                "private_km": km["private"],
                "undecided_km": km["undecided"],
                "purpose": t.purpose or "",
                "contacts": _contacts(ctx, t),
                "driver": ctx.drivers.get(t.driver_id, ctx.driver_name) if t.driver_id else ctx.driver_name,
                "notes": _notes(ctx, t, refuels_by_trip.get(t.id, [])),
                "segments": t.segment_count,
                "source": SOURCE_LABELS.get(str(t.source), str(t.source)),
                "review": REVIEW_LABELS.get(t.review_status, str(t.review_status)),
                "fuel": round(fuel.verified, 3) if fuel.verified_km > 0 else None,
                "energy": round(energy.verified, 3) if energy.verified_km > 0 else None,
                "month": when.strftime("%Y-%m") if when else "okänd",
                "trip_id": short_id(t.id),
            }
        )
    return rows


def month_keys(ctx: ExportContext, rows: list[dict[str, Any]]) -> list[str]:
    """Alla månader i perioden (även tomma), plus 'okänd' om någon post saknar tid."""
    keys: list[str] = []
    if ctx.period.bounded:
        cur = ctx.period.start.astimezone(ctx.tz).date().replace(day=1)  # type: ignore[union-attr]
        last = (ctx.period.end.astimezone(ctx.tz) - timedelta(seconds=1)).date()  # type: ignore[union-attr]
        while cur <= last and len(keys) < 240:
            keys.append(cur.strftime("%Y-%m"))
            cur = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
    for k in sorted({r["month"] for r in rows if r["month"] != "okänd"}):
        if k not in keys:
            keys.append(k)
    keys.sort()
    if any(r["month"] == "okänd" for r in rows):
        keys.append("okänd")
    return keys


def python_totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Facit som formlernas cachevärden jämförs mot."""
    by_month: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in rows:
        m = by_month[r["month"]]
        m["trips"] += 1
        for k in ("work_km", "private_km", "undecided_km", "km"):
            m[k] += r[k]
        m["fuel"] += r["fuel"] or 0
        m["energy"] += r["energy"] or 0
    total: dict[str, float] = defaultdict(float)
    for m in by_month.values():
        for k, v in m.items():
            total[k] += v
    return {"by_month": {k: dict(v) for k, v in by_month.items()}, "total": dict(total)}


# ----------------------------------------------------------------------------- skrivare


class _Book:
    def __init__(self, path: Path, ctx: ExportContext):
        self.ctx = ctx
        self.wb = xlsxwriter.Workbook(
            str(path),
            {"strings_to_formulas": False, "strings_to_urls": False, "strings_to_numbers": False, "remove_timezone": True},
        )
        f = self.wb.add_format
        base = {"font_name": "Calibri", "font_size": 10, "valign": "top"}
        self.fmt = {
            "text": f({**base, "text_wrap": True}),
            "plain": f({**base}),
            "bold": f({**base, "bold": True}),
            "title": f({"font_name": "Calibri", "font_size": 16, "bold": True}),
            "section": f({**base, "bold": True, "font_size": 12, "bottom": 1, "font_color": "#1F3A5F"}),
            "header": f({**base, "bold": True, "text_wrap": True, "bg_color": "#1F3A5F", "font_color": "#FFFFFF",
                         "border": 1, "valign": "vcenter"}),
            "date": f({**base, "num_format": "yyyy-mm-dd"}),
            "time": f({**base, "num_format": "hh:mm"}),
            "datetime": f({**base, "num_format": "yyyy-mm-dd hh:mm"}),
            "km": f({**base, "num_format": "#,##0.0"}),
            "km_bold": f({**base, "num_format": "#,##0.0", "bold": True, "top": 1}),
            "mil_bold": f({**base, "num_format": "#,##0.00", "bold": True, "top": 1}),
            "odo": f({**base, "num_format": "#,##0"}),
            "int": f({**base, "num_format": "0"}),
            "int_bold": f({**base, "num_format": "0", "bold": True, "top": 1}),
            "pct": f({**base, "num_format": "0 %"}),
            "money": f({**base, "num_format": "#,##0.00"}),
            "num2": f({**base, "num_format": "#,##0.00"}),
            "total_label": f({**base, "bold": True, "top": 1}),
            "ok": f({**base, "bold": True, "font_size": 12, "bg_color": "#D8F0DC", "font_color": "#145A21",
                     "border": 1, "text_wrap": True, "valign": "vcenter"}),
            "bad": f({**base, "bold": True, "font_size": 12, "bg_color": "#F8D7DA", "font_color": "#8A1C24",
                      "border": 1, "text_wrap": True, "valign": "vcenter"}),
            "demo": f({**base, "bold": True, "font_size": 12, "font_color": "#C00000"}),
            "sev_error": f({**base, "bold": True, "font_color": "#8A1C24", "bg_color": "#F8D7DA"}),
            "sev_warning": f({**base, "bold": True, "font_color": "#7A5200", "bg_color": "#FFF3CD"}),
            "sev_info": f({**base, "font_color": "#1F3A5F", "bg_color": "#E3EDF7"}),
            "muted": f({**base, "italic": True, "font_color": "#555555", "text_wrap": True}),
        }

    # -- cellhjälpare -----------------------------------------------------------------
    def text(self, ws, r: int, c: int, value: Any, fmt: str = "text") -> None:
        ws.write_string(r, c, safe_text(value), self.fmt[fmt])

    def number(self, ws, r: int, c: int, value: float | None, fmt: str = "km") -> None:
        if value is None:
            ws.write_blank(r, c, None, self.fmt[fmt])
        else:
            ws.write_number(r, c, float(value), self.fmt[fmt])

    def formula(self, ws, r: int, c: int, formula: str, value: float, fmt: str = "km") -> None:
        ws.write_formula(r, c, formula, self.fmt[fmt], round(float(value), 6))

    def page_setup(self, ws, header_row: int | None, title: str) -> None:
        ctx = self.ctx
        ws.set_landscape()
        ws.set_paper(9)  # A4
        ws.fit_to_pages(1, 0)
        ws.set_margins(left=0.4, right=0.4, top=0.7, bottom=0.6)
        if header_row is not None:
            ws.repeat_rows(header_row)
        reg = (ctx.vehicle.registration_number or "regnr saknas").replace("&", "och")
        label = ctx.period.label.replace("&", "och")
        status = status_text(ctx, short=True)
        demo = "  DEMODATA" if ctx.vehicle.is_demo else ""
        ws.set_header(f"&L{title} – {reg} – {label}&R{status}{demo}"[:250])
        ws.set_footer("&LSMGD DriveLog&CSida &P av &N&R&D")


def _journal_sheet(book: _Book, rows: list[dict[str, Any]]) -> dict[str, Any]:
    ctx, wb = book.ctx, book.wb
    ws = wb.add_worksheet(SHEET_JOURNAL)
    ws.write_string(0, 0, "Körjournal", book.fmt["title"])
    reg = ctx.vehicle.registration_number or MISSING
    book.text(ws, 1, 0, f"Bilens registreringsnummer: {reg}", "bold")
    book.text(ws, 1, 3, f"År/period: {ctx.period.label}", "bold")
    book.text(ws, 1, 4, f"Förare: {ctx.driver_name or MISSING}", "bold")
    book.text(ws, 2, 0, f"Status: {status_text(ctx)}", "bold")
    if ctx.vehicle.is_demo:
        book.text(ws, 2, 4, DEMO_WATERMARK, "demo")

    hr = JOURNAL_HEADER_ROW
    for c, (_, title, width) in enumerate(JOURNAL_COLUMNS):
        ws.write_string(hr, c, title, book.fmt["header"])
        ws.set_column(c, c, width)
    ws.set_row(hr, 32)
    first = hr + 1
    r = first
    for row in rows:
        if row["date"] is not None:
            ws.write_datetime(r, COL["date"], row["date"], book.fmt["date"])
        if row["start"] is not None:
            ws.write_datetime(r, COL["start"], row["start"], book.fmt["time"])
        if row["end"] is not None:
            ws.write_datetime(r, COL["end"], row["end"], book.fmt["datetime" if row["end_other_day"] else "time"])
        for key in ("start_address", "end_address", "category", "purpose", "contacts", "driver", "notes", "source",
                    "review", "month", "trip_id"):
            book.text(ws, r, COL[key], row[key])
        book.number(ws, r, COL["odo_start"], row["odo_start"], "odo")
        book.number(ws, r, COL["odo_end"], row["odo_end"], "odo")
        for key in ("km", "work_km", "private_km", "undecided_km"):
            book.number(ws, r, COL[key], row[key], "km")
        book.number(ws, r, COL["segments"], row["segments"], "int")
        book.number(ws, r, COL["fuel"], row["fuel"], "num2")
        book.number(ws, r, COL["energy"], row["energy"], "num2")
        r += 1
    last = max(first, r - 1)  # minst en (ev. tom) datarad så att formelområdena är giltiga

    total_row = last + 2
    book.text(ws, total_row, COL["end_address"], "Summa (visade rader)", "total_label")
    sums = {k: sum(row[k] or 0 for row in rows) for k in ("km", "work_km", "private_km", "undecided_km", "fuel", "energy")}
    for key in ("km", "work_km", "private_km", "undecided_km", "fuel", "energy"):
        c = COL[key]
        rng = f"{xl_rowcol_to_cell(first, c)}:{xl_rowcol_to_cell(last, c)}"
        book.formula(ws, total_row, c, f"=SUBTOTAL(109,{rng})", sums[key], "km_bold")
    ws.autofilter(hr, 0, last, len(JOURNAL_COLUMNS) - 1)
    ws.freeze_panes(first, 3)
    book.page_setup(ws, hr, "Körjournal")
    # Utskriften omfattar Skatteverkets kolumner (t.o.m. Anteckningar); tekniska hjälpkolumner
    # finns kvar i bladet men skrivs inte ut, så att texten blir läsbar på papper.
    ws.print_area(0, 0, total_row, COL["notes"])
    return {"first": first, "last": last, "total_row": total_row, "sums": sums}


def _abs_range(sheet: str, col: int, first: int, last: int) -> str:
    return f"'{sheet}'!{xl_rowcol_to_cell(first, col, True, True)}:{xl_rowcol_to_cell(last, col, True, True)}"


def _monthly_sheet(book: _Book, rows: list[dict[str, Any]], jr: dict[str, Any]) -> dict[str, Any]:
    ctx, wb = book.ctx, book.wb
    ws = wb.add_worksheet(SHEET_MONTHLY)
    ws.write_string(0, 0, "Månadssammanställning", book.fmt["title"])
    book.text(ws, 1, 0, "Summorna är formler som räknar direkt på bladet Körjournal. 1 mil = 10 km.", "muted")
    headers = ["Månad", "Antal resor", "Tjänst (km)", "Privat (km)", "Ej avgjort (km)", "Totalt (km)", "Tjänst (mil)",
               "Privat (mil)", "Totalt (mil)", "Bensin (l, verifierad)", "El (kWh, verifierad)"]
    hr = 3
    for c, h in enumerate(headers):
        ws.write_string(hr, c, h, book.fmt["header"])
    ws.set_row(hr, 30)
    ws.set_column(0, 0, 12)
    ws.set_column(1, len(headers) - 1, 14)

    first, last = jr["first"], jr["last"]
    month_rng = _abs_range(SHEET_JOURNAL, COL["month"], first, last)
    facit = python_totals(rows)
    keys = month_keys(ctx, rows)
    r = hr + 1
    first_month_row = r
    for key in keys:
        m = facit["by_month"].get(key, {})
        book.text(ws, r, 0, key)
        a = xl_rowcol_to_cell(r, 0, False, True)
        # Jokertecknet tvingar textjämförelse, så att "2026-09" aldrig tolkas som ett datum.
        crit = f'{a}&"*"'
        book.formula(ws, r, 1, f"=COUNTIFS({month_rng},{crit})", m.get("trips", 0), "int")
        for c, col_key in ((2, "work_km"), (3, "private_km"), (4, "undecided_km")):
            rng = _abs_range(SHEET_JOURNAL, COL[col_key], first, last)
            book.formula(ws, r, c, f"=SUMIFS({rng},{month_rng},{crit})", m.get(col_key, 0))
        row_total = m.get("work_km", 0) + m.get("private_km", 0) + m.get("undecided_km", 0)
        c2, c3, c4, c5 = (xl_rowcol_to_cell(r, i) for i in (2, 3, 4, 5))
        book.formula(ws, r, 5, f"={c2}+{c3}+{c4}", row_total)
        book.formula(ws, r, 6, f"={c2}/10", m.get("work_km", 0) / 10, "num2")
        book.formula(ws, r, 7, f"={c3}/10", m.get("private_km", 0) / 10, "num2")
        book.formula(ws, r, 8, f"={c5}/10", row_total / 10, "num2")
        for c, col_key in ((9, "fuel"), (10, "energy")):
            rng = _abs_range(SHEET_JOURNAL, COL[col_key], first, last)
            book.formula(ws, r, c, f"=SUMIFS({rng},{month_rng},{crit})", m.get(col_key, 0), "num2")
        r += 1
    last_month_row = max(first_month_row, r - 1)
    total_row = last_month_row + 1
    tot = facit["total"]
    book.text(ws, total_row, 0, "Summa", "total_label")
    grand = tot.get("work_km", 0) + tot.get("private_km", 0) + tot.get("undecided_km", 0)
    values = [tot.get("trips", 0), tot.get("work_km", 0), tot.get("private_km", 0), tot.get("undecided_km", 0), grand,
              tot.get("work_km", 0) / 10, tot.get("private_km", 0) / 10, grand / 10, tot.get("fuel", 0), tot.get("energy", 0)]
    for i, value in enumerate(values, start=1):
        rng = f"{xl_rowcol_to_cell(first_month_row, i)}:{xl_rowcol_to_cell(last_month_row, i)}"
        book.formula(ws, total_row, i, f"=SUM({rng})", value, "int_bold" if i == 1 else "km_bold")
    ws.freeze_panes(hr + 1, 1)
    ws.autofilter(hr, 0, last_month_row, len(headers) - 1)
    book.page_setup(ws, hr, "Månadssammanställning")
    return {"total_row": total_row, "facit": facit, "grand_total": grand}


def _overview_sheet(book: _Book, ws, rows: list[dict[str, Any]], jr: dict[str, Any], mr: dict[str, Any]) -> None:
    ctx = book.ctx
    tz = ctx.tz
    ws.set_column(0, 0, 46)
    ws.set_column(1, 1, 22)
    ws.set_column(2, 2, 16)
    ws.set_column(3, 3, 60)
    ws.write_string(0, 0, "Körjournal – översikt", book.fmt["title"])
    r = 1
    if ctx.vehicle.is_demo:
        ws.merge_range(r, 0, r, 3, DEMO_WATERMARK, book.fmt["demo"])
        r += 1
    book.text(ws, r, 0, "Status", "bold")
    ws.merge_range(r, 1, r, 3, status_text(ctx), book.fmt["ok" if ctx.is_complete else "bad"])
    ws.set_row(r, 24)
    r += 1
    errors = [c for c in ctx.checks if c["severity"] == "error"]
    if errors:
        book.text(ws, r, 0, "Fel som behöver åtgärdas" if ctx.business_only else "Brister som gör journalen ofullständig",
                  "bold")
        for c in errors[:12]:
            ws.merge_range(r, 1, r, 3, safe_text(f"• {c['title']}: {c['message']}"), book.fmt["text"])
            ws.set_row(r, 28)
            r += 1
        if len(errors) > 12:
            ws.merge_range(r, 1, r, 3, f"… och {len(errors) - 12} till, se bladet {SHEET_QUALITY}.", book.fmt["text"])
            r += 1
    elif not ctx.is_complete:
        r += 1
    r += 1

    def label_value(label: str, value: Any, note: str = "", fmt: str = "plain") -> int:
        nonlocal r
        book.text(ws, r, 0, label, "plain")
        if isinstance(value, datetime):
            ws.write_datetime(r, 1, value, book.fmt["datetime"])
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            book.number(ws, r, 1, value, fmt)
        else:
            book.text(ws, r, 1, value if value not in (None, "") else MISSING, "plain")
        if note:
            book.text(ws, r, 3, note, "muted")
        r += 1
        return r - 1

    v = ctx.vehicle
    ws.write_string(r, 0, "Fordon och period", book.fmt["section"])
    r += 1
    label_value("Bilens registreringsnummer", v.registration_number)
    desc = " ".join(str(p) for p in (v.make, v.model, v.model_year) if p)
    label_value("Fordon", desc or v.name)
    label_value("År / period", ctx.period.label)
    if ctx.period.start is not None:
        label_value("Period från", _naive_local(ctx.period.start, tz))
    if ctx.period.end is not None:
        label_value("Period till (exklusive)", _naive_local(ctx.period.end, tz))
    label_value("Förare", ctx.driver_name)
    if ctx.employer_name:
        label_value("Arbetsgivare", ctx.employer_name)
    label_value("Tidszon", str(tz))
    label_value("Exporten skapad", _naive_local(ctx.generated_at, tz))
    if ctx.selection:
        label_value("Omfattning", "Urval av resor", "Exporten innehåller endast valda resor och kan inte stämmas av.")
    r += 1

    def odo_note(reading: OdometerReading) -> str:
        if reading.value is None:
            return "Registrera en avläsning för att kunna stämma av perioden."
        when = _naive_local(reading.at, tz)
        return reading.source_label + (f" ({when:%Y-%m-%d %H:%M})" if when else "")

    ws.write_string(r, 0, "Mätarställning", book.fmt["section"])
    r += 1
    rs = label_value("Mätarställning vid periodens början (km)", ctx.odometer_start.value, odo_note(ctx.odometer_start), "odo")
    re_ = label_value("Mätarställning vid periodens slut (km)", ctx.odometer_end.value, odo_note(ctx.odometer_end), "odo")
    journal_km = mr["grand_total"]
    monthly_total = f"'{SHEET_MONTHLY}'!{xl_rowcol_to_cell(mr['total_row'], 5, True, True)}"
    both = ctx.odometer_start.value is not None and ctx.odometer_end.value is not None
    book.text(ws, r, 0, "Körsträcka enligt mätarställning (km)", "plain")
    if both:
        delta = ctx.odometer_end.value - ctx.odometer_start.value  # type: ignore[operator]
        book.formula(ws, r, 1, f"={xl_rowcol_to_cell(re_, 1)}-{xl_rowcol_to_cell(rs, 1)}", delta)
    else:
        book.text(ws, r, 1, "Kan inte beräknas", "plain")
        book.text(ws, r, 3, "Mätarställning saknas vid periodens början eller slut.", "muted")
    delta_row = r
    r += 1
    book.text(ws, r, 0, "Journalförd körsträcka (km)", "plain")
    book.formula(ws, r, 1, f"={monthly_total}", journal_km)
    journal_row = r
    r += 1
    book.text(ws, r, 0, "Differens mätarställning − journal (km)", "plain")
    if both:
        book.formula(ws, r, 1, f"={xl_rowcol_to_cell(delta_row, 1)}-{xl_rowcol_to_cell(journal_row, 1)}", delta - journal_km)
        book.text(ws, r, 3, "Körning mellan tjänsteresorna är privat och redovisas inte." if ctx.business_only
                  else "Kilometer som inte är journalförda. Ska vara nära noll för en komplett journal.", "muted")
    else:
        book.text(ws, r, 1, "Kan inte beräknas", "plain")
    r += 1
    open_gaps = [g for g in ctx.gaps if g.status == GapStatus.OPEN]
    if ctx.business_only:
        label_value("Körning mellan resorna (privat)", round(sum(g.km for g in open_gaps), 1),
                    "Luckor i mätarställningen är privat körning och ingen brist.", "km")
    else:
        label_value("Ej journalförda km (öppna luckor)", round(sum(g.km for g in open_gaps), 1),
                    f"{len(open_gaps)} luckor i mätarställningen saknar journalpost." if open_gaps else "", "km")
    first_odo, last_odo = common.first_and_last_trip_odometer(ctx.trips)
    label_value("Första journalförda resans mätarställning (km)", first_odo, "", "odo")
    label_value("Sista journalförda resans mätarställning (km)", last_odo, "", "odo")
    r += 1

    ws.write_string(r, 0, "Körsträcka per kategori", book.fmt["section"])
    r += 1
    for c, h in enumerate(["Kategori", "Kilometer", "Mil", "Antal resor"]):
        ws.write_string(r, c, h, book.fmt["header"])
    r += 1
    cat_rng = _abs_range(SHEET_JOURNAL, COL["category"], jr["first"], jr["last"])
    tot = mr["facit"]["total"]
    kinds = defaultdict(int)
    for row in rows:
        kinds[row["kind"]] += 1
    cat_rows = []
    for label, mcol, key, kind in (
        ("Tjänst (arbete)", 2, "work_km", "work"), ("Privat", 3, "private_km", "private"), ("Ej avgjort", 4, "undecided_km", "undecided"),
    ):
        ref = f"'{SHEET_MONTHLY}'!{xl_rowcol_to_cell(mr['total_row'], mcol, True, True)}"
        book.text(ws, r, 0, label, "plain")
        book.formula(ws, r, 1, f"={ref}", tot.get(key, 0))
        book.formula(ws, r, 2, f"={xl_rowcol_to_cell(r, 1)}/10", tot.get(key, 0) / 10, "num2")
        book.formula(ws, r, 3, f'=COUNTIFS({cat_rng},"{CATEGORY_LABELS[kind]}")', kinds.get(kind, 0), "int")
        cat_rows.append(r)
        r += 1
    book.text(ws, r, 0, "Blandade resor (privat och tjänst)", "plain")
    book.text(ws, r, 1, "ingår ovan", "plain")
    book.formula(ws, r, 3, f'=COUNTIFS({cat_rng},"{CATEGORY_LABELS["mixed"]}")', kinds.get("mixed", 0), "int")
    mixed_row = r
    r += 1
    book.text(ws, r, 0, "Totalt", "total_label")
    b1, b2 = xl_rowcol_to_cell(cat_rows[0], 1), xl_rowcol_to_cell(cat_rows[-1], 1)
    book.formula(ws, r, 1, f"=SUM({b1}:{b2})", journal_km, "km_bold")
    book.formula(ws, r, 2, f"={xl_rowcol_to_cell(r, 1)}/10", journal_km / 10, "mil_bold")
    d1, d2 = xl_rowcol_to_cell(cat_rows[0], 3), xl_rowcol_to_cell(mixed_row, 3)
    book.formula(ws, r, 3, f"=SUM({d1}:{d2})", len(rows), "int_bold")
    r += 1
    prelim = [row for row in rows if row["trip"].review_status in common.PRELIMINARY]
    label_value("varav preliminärt klassat – ej godkänt (km)", round(sum(row["km"] for row in prelim), 1),
                f"{len(prelim)} resor har automatisk klassning som inte godkänts." if prelim else "", "km")
    summarized = [row for row in rows if common.is_gap_fill(row["trip"])]
    label_value("varav sammanfattad privat körning ur luckor (km)", round(sum(row["km"] for row in summarized), 1),
                "Privat körning som föraren intygat utan detaljerade resor." if summarized else "", "km")
    r += 1

    fuel, energy = EnergyAcc(), EnergyAcc()
    regen = 0.0
    for t in ctx.trips:
        f_acc, e_acc = trip_energy(t)
        for acc, src in ((fuel, f_acc), (energy, e_acc)):
            acc.verified += src.verified
            acc.verified_km += src.verified_km
            acc.estimated += src.estimated
            acc.estimated_km += src.estimated_km
            acc.unknown_km += src.unknown_km
        regen += t.regen_kwh or 0
    ws.write_string(r, 0, "Bränsle och el", book.fmt["section"])
    r += 1
    note = "Endast uppmätta värden per resa räknas som verifierade."
    label_value("Bensin, verifierad (liter)", round(fuel.verified, 2), note, "num2")
    label_value("Sträcka med verifierad bensinförbrukning (km)", round(fuel.verified_km, 1), "", "km")
    label_value("Täckningsgrad bensin", fuel.coverage if fuel.coverage is not None else None,
                "Andel av körsträckan som har uppmätt förbrukning.", "pct")
    label_value("Bensin (l/100 km, verifierad sträcka)", fuel.per_100km, "", "num2")
    label_value("El, verifierad (kWh)", round(energy.verified, 2), "", "num2")
    label_value("Sträcka med verifierad elförbrukning (km)", round(energy.verified_km, 1), "", "km")
    label_value("Täckningsgrad el", energy.coverage if energy.coverage is not None else None, "", "pct")
    label_value("El (kWh/100 km, verifierad sträcka)", energy.per_100km, "", "num2")
    label_value("Regenererad energi (kWh)", round(regen, 2), "", "num2")
    fr, cr = ctx.fuel_records, ctx.charging_records
    label_value("Tankningar (antal)", len(fr), "", "int")
    label_value("Tankat (liter)", round(sum(x.liters or 0 for x in fr), 2), "", "num2")
    fuel_cost = [x.total_cost for x in fr if x.total_cost is not None]
    label_value("Drivmedelskostnad (kr)", round(sum(fuel_cost), 2) if fuel_cost else None, "", "money")
    label_value("Laddningar (antal)", len(cr), "", "int")
    label_value("Laddat (kWh)", round(sum(x.kwh or 0 for x in cr), 2), "", "num2")
    charge_cost = [x.total_cost for x in cr if x.total_cost is not None]
    label_value("Laddkostnad (kr)", round(sum(charge_cost), 2) if charge_cost else None, "", "money")
    r += 1
    ws.merge_range(
        r, 0, r, 3,
        "Körjournalen är framställd ur lagrade uppgifter. Värden som saknas i underlaget anges som "
        f"”{MISSING}” och fylls aldrig i med antaganden. Detaljer per delresa och ändringshistorik finns i bladet "
        f"{SHEET_SOURCES}.",
        book.fmt["muted"],
    )
    ws.set_row(r, 42)
    book.page_setup(ws, None, "Översikt")


def _energy_sheet(book: _Book) -> None:
    ctx, wb = book.ctx, book.wb
    tz = ctx.tz
    ws = wb.add_worksheet(SHEET_ENERGY)
    ws.write_string(0, 0, "Bränsle och laddning", book.fmt["title"])
    book.text(
        ws, 1, 0,
        "Verifierad = uppmätt per resa i bilens körjournal. Snitt per 100 km beräknas endast över sträcka med "
        "verifierade värden. Bilens tanknivå och genomsnitt från API används inte som förbrukning.", "muted",
    )
    headers = ["Månad", "Körsträcka (km)", "Bensin verifierad (l)", "Sträcka med verifierad bensin (km)",
               "Täckningsgrad bensin", "l/100 km", "El verifierad (kWh)", "Sträcka med verifierad el (km)",
               "Täckningsgrad el", "kWh/100 km", "Regenererat (kWh)", "Tankningar (antal)", "Tankat (l)",
               "Drivmedelskostnad (kr)", "Laddningar (antal)", "Laddat (kWh)", "Laddkostnad (kr)"]
    hr = 3
    for c, h in enumerate(headers):
        ws.write_string(hr, c, h, book.fmt["header"])
    ws.set_row(hr, 44)
    ws.set_column(0, 0, 11)
    ws.set_column(1, len(headers) - 1, 13)
    r = hr + 1
    first = r
    for m in ctx.monthly_energy:
        book.text(ws, r, 0, m["month"])
        book.number(ws, r, 1, m["total_km"], "km")
        book.number(ws, r, 2, m["fuel_verified_l"], "num2")
        book.number(ws, r, 3, m["fuel_verified_km"], "km")
        book.number(ws, r, 4, m["fuel_coverage"], "pct")
        book.number(ws, r, 5, m["fuel_per_100km"], "num2")
        book.number(ws, r, 6, m["energy_verified_kwh"], "num2")
        book.number(ws, r, 7, m["energy_verified_km"], "km")
        book.number(ws, r, 8, m["energy_coverage"], "pct")
        book.number(ws, r, 9, m["energy_per_100km"], "num2")
        book.number(ws, r, 10, m["regen_kwh"], "num2")
        book.number(ws, r, 11, m["refuels"], "int")
        book.number(ws, r, 12, m["refuel_liters"], "num2")
        book.number(ws, r, 13, m["fuel_cost"], "money")
        book.number(ws, r, 14, m["charging_sessions"], "int")
        book.number(ws, r, 15, m["charged_kwh"], "num2")
        book.number(ws, r, 16, m["charging_cost"], "money")
        r += 1
    last = max(first, r - 1)
    book.text(ws, last + 1, 0, "Summa", "total_label")
    for c, key in ((1, "total_km"), (2, "fuel_verified_l"), (3, "fuel_verified_km"), (6, "energy_verified_kwh"),
                   (7, "energy_verified_km"), (10, "regen_kwh"), (11, "refuels"), (12, "refuel_liters"),
                   (13, "fuel_cost"), (14, "charging_sessions"), (15, "charged_kwh"), (16, "charging_cost")):
        value = sum((m.get(key) or 0) for m in ctx.monthly_energy)
        rng = f"{xl_rowcol_to_cell(first, c)}:{xl_rowcol_to_cell(last, c)}"
        book.formula(ws, last + 1, c, f"=SUM({rng})", value, "km_bold")
    r = last + 4

    ws.write_string(r, 0, "Tankningar", book.fmt["section"])
    r += 1
    for c, h in enumerate(["Tidpunkt", "Liter", "Datakvalitet", "Kostnad (kr)", "Pris per liter", "Mätarställning (km)",
                           "Station", "Full tank", "Källa", "Anteckning"]):
        ws.write_string(r, c, h, book.fmt["header"])
    r += 1
    if not ctx.fuel_records:
        book.text(ws, r, 0, "Inga tankningar registrerade i perioden.", "muted")
        r += 1
    for x in ctx.fuel_records:
        ws.write_datetime(r, 0, _naive_local(x.at, tz), book.fmt["datetime"])
        book.number(ws, r, 1, x.liters, "num2")
        book.text(ws, r, 2, "Verifierad" if x.liters_quality == "verified" else "Uppskattad")
        book.number(ws, r, 3, x.total_cost, "money")
        book.number(ws, r, 4, x.price_per_liter, "money")
        book.number(ws, r, 5, x.odometer_km, "odo")
        book.text(ws, r, 6, x.station)
        book.text(ws, r, 7, "Ja" if x.full_tank else "Nej")
        book.text(ws, r, 8, "Manuellt registrerad" if x.source == "manual" else "Förslag ur tanknivå")
        book.text(ws, r, 9, x.note)
        r += 1
    r += 2
    ws.write_string(r, 0, "Laddningar", book.fmt["section"])
    r += 1
    for c, h in enumerate(["Start", "Slut", "kWh", "Datakvalitet", "Kostnad (kr)", "Plats", "Källa", "Anteckning"]):
        ws.write_string(r, c, h, book.fmt["header"])
    r += 1
    if not ctx.charging_records:
        book.text(ws, r, 0, "Inga laddningar registrerade i perioden.", "muted")
        r += 1
    for y in ctx.charging_records:
        ws.write_datetime(r, 0, _naive_local(y.started_at, tz), book.fmt["datetime"])
        if y.ended_at is not None:
            ws.write_datetime(r, 1, _naive_local(y.ended_at, tz), book.fmt["datetime"])
        book.number(ws, r, 2, y.kwh, "num2")
        book.text(ws, r, 3, "Verifierad" if y.kwh_quality == "verified" else "Uppskattad")
        book.number(ws, r, 4, y.total_cost, "money")
        book.text(ws, r, 5, y.location)
        book.text(ws, r, 6, "Manuellt registrerad" if y.source == "manual" else "Detekterad")
        book.text(ws, r, 7, y.note)
        r += 1
    ws.freeze_panes(hr + 1, 1)
    book.page_setup(ws, hr, "Bränsle och laddning")


def _quality_sheet(book: _Book, rows: list[dict[str, Any]]) -> None:
    ctx, wb = book.ctx, book.wb
    tz = ctx.tz
    ws = wb.add_worksheet(SHEET_QUALITY)
    ws.write_string(0, 0, "Datakvalitet och avvikelser", book.fmt["title"])
    if ctx.business_only:
        status = STATUS_READY if ctx.is_complete else "Fel behöver åtgärdas – se felen nedan"
    else:
        status = STATUS_COMPLETE if ctx.is_complete else "OFULLSTÄNDIG – bristerna nedan måste åtgärdas"
    ws.merge_range(1, 0, 1, 4, status, book.fmt["ok" if ctx.is_complete else "bad"])
    hr = 3
    for c, h in enumerate(["Allvarlighetsgrad", "Kontroll", "Beskrivning", "Antal", "Berörda resor (Rese-ID)"]):
        ws.write_string(hr, c, h, book.fmt["header"])
    ws.set_column(0, 0, 16)
    ws.set_column(1, 1, 36)
    ws.set_column(2, 2, 80)
    ws.set_column(3, 3, 8)
    ws.set_column(4, 4, 60)
    r = hr + 1
    if not ctx.checks:
        book.text(ws, r, 0, "Information", "sev_info")
        book.text(ws, r, 1, "Inga avvikelser")
        book.text(ws, r, 2, "Alla kontroller passerade utan anmärkning.")
        r += 1
    for c in ctx.checks:
        book.text(ws, r, 0, SEVERITY_LABELS.get(c["severity"], c["severity"]), f"sev_{c['severity']}")
        book.text(ws, r, 1, c["title"])
        book.text(ws, r, 2, c["message"])
        book.number(ws, r, 3, c["count"], "int")
        ids = ", ".join(short_id(i) for i in c.get("trip_ids", [])[:40])
        if len(c.get("trip_ids", [])) > 40:
            ids += " …"
        book.text(ws, r, 4, ids)
        r += 1
    r += 2

    ws.write_string(r, 0, "Körning mellan resorna (privat, ingen brist)" if ctx.business_only
                    else "Dataluckor i mätarställningen", book.fmt["section"])
    r += 1
    for c, h in enumerate(["Status", "Från mätarställning (km)", "Till mätarställning (km)", "Kilometer", "Från tid – till tid",
                           "Notering"]):
        ws.write_string(r, c, h, book.fmt["header"])
    r += 1
    if not ctx.gaps:
        book.text(ws, r, 0, "Inga dataluckor i perioden." if not ctx.selection else "Luckor redovisas inte vid export av valda resor.", "muted")
        r += 1
    first_gap = r
    for g in ctx.gaps:
        if ctx.business_only:
            label = "Privat körning" if g.status == GapStatus.OPEN else GAP_STATUS_LABELS.get(g.status, str(g.status))
            book.text(ws, r, 0, label, "plain")
        else:
            book.text(ws, r, 0, GAP_STATUS_LABELS.get(g.status, str(g.status)),
                      "sev_error" if g.status == GapStatus.OPEN else "plain")
        book.number(ws, r, 1, g.odometer_start, "odo")
        book.number(ws, r, 2, g.odometer_end, "odo")
        book.number(ws, r, 3, g.km, "km")
        lo, hi = _naive_local(g.from_time, tz), _naive_local(g.to_time, tz)
        book.text(ws, r, 4, f"{lo:%Y-%m-%d %H:%M} – {hi:%Y-%m-%d %H:%M}" if lo and hi else "Okänd tid")
        book.text(ws, r, 5, g.resolution_note)
        r += 1
    if ctx.gaps:
        book.text(ws, r, 0, "Summa", "total_label")
        rng = f"{xl_rowcol_to_cell(first_gap, 3)}:{xl_rowcol_to_cell(r - 1, 3)}"
        book.formula(ws, r, 3, f"=SUM({rng})", sum(g.km for g in ctx.gaps), "km_bold")
        r += 1
    r += 2

    ws.write_string(r, 0, "Resor med anmärkning på datakvalitet", book.fmt["section"])
    r += 1
    for c, h in enumerate(["Rese-ID", "Datum", "Anmärkning"]):
        ws.write_string(r, c, h, book.fmt["header"])
    r += 1
    any_flag = False
    for row in rows:
        t = row["trip"]
        remarks = []
        flags = set(t.quality_flags or [])
        if "missing_odometer" in flags:
            remarks.append("Mätarställning saknas")
        if "possible_missed_trips" in flags:
            remarks.append("Flera körningar kan ha skett mellan fordonsrapporterna")
        if "start_time_uncertain" in flags or t.started_at is None:
            remarks.append("Starttiden är inte känd")
        if "end_time_uncertain" in flags:
            remarks.append("Sluttiden är osäker")
        if "summary_of_gap" in flags:
            remarks.append("Sammanfattad lucka (ingen detaljerad resa)")
        if t.source == TripSource.VOLVO_API:
            remarks.append("Rekonstruerad från fordonsrapporter (inte bilens egen körjournal)")
        if t.has_gap:
            remarks.append(f"Lucka på {t.gap_km:.0f} km mellan delresor")
        if t.review_status in common.PRELIMINARY:
            remarks.append("Preliminär klassning – ej godkänd")
        if not remarks:
            continue
        any_flag = True
        book.text(ws, r, 0, row["trip_id"])
        if row["date"] is not None:
            ws.write_datetime(r, 1, row["date"], book.fmt["date"])
        book.text(ws, r, 2, "; ".join(remarks))
        r += 1
    if not any_flag:
        book.text(ws, r, 0, "Inga anmärkningar.", "muted")
    ws.freeze_panes(hr + 1, 0)
    book.page_setup(ws, hr, "Datakvalitet")


def _diff(before: dict[str, Any] | None, after: dict[str, Any] | None) -> tuple[str, str]:
    before, after = before or {}, after or {}
    keys = [k for k in dict.fromkeys([*before, *after]) if before.get(k) != after.get(k)]
    b = {k: before.get(k) for k in keys if k in before}
    a = {k: after.get(k) for k in keys if k in after}

    def dump(d: dict[str, Any]) -> str:
        return json.dumps(d, ensure_ascii=False, default=str)[:600] if d else ""

    return dump(b), dump(a)


def _sources_sheet(book: _Book, rows: list[dict[str, Any]]) -> None:
    ctx, wb = book.ctx, book.wb
    tz = ctx.tz
    ws = wb.add_worksheet(SHEET_SOURCES)
    ws.write_string(0, 0, "Källa och ändringshistorik", book.fmt["title"])
    book.text(
        ws, 1, 0,
        "Varje journalresa består av en eller flera originalresor (delresor). Originaluppgifterna ändras aldrig vid "
        "sammanslagning; kilometer redovisas per delsträcka och kan därför inte dubbelräknas.", "muted",
    )
    headers = ["Rese-ID", "Delresa nr", "Original-ID", "Datakälla", "Start", "Slut", "Mätarställning start (km)",
               "Mätarställning slut (km)", "Delsträcka (km)", "Kategori (delsträcka)", "Originalfil", "Rad i fil",
               "Originalkategori", "Originalanteckning", "Kvalitetsflaggor"]
    hr = 3
    for c, h in enumerate(headers):
        ws.write_string(hr, c, h, book.fmt["header"])
    ws.set_row(hr, 30)
    for c, w in enumerate([10, 8, 10, 26, 16, 16, 12, 12, 10, 14, 30, 8, 14, 40, 30]):
        ws.set_column(c, c, w)
    r = hr + 1
    first = r
    total = 0.0
    for row in rows:
        t = row["trip"]
        private_hidden = _private_only(t) and not ctx.include_private_addresses
        for s in sorted(t.segments, key=lambda s: (s.seq, s.part)):
            raw = s.raw_trip
            book.text(ws, r, 0, row["trip_id"])
            book.text(ws, r, 1, f"{s.seq + 1}" + (f".{s.part + 1}" if s.part or sum(1 for x in t.segments if x.raw_trip_id == s.raw_trip_id) > 1 else ""))
            book.text(ws, r, 2, short_id(raw.id))
            book.text(ws, r, 3, SOURCE_LABELS.get(str(raw.source), str(raw.source)))
            if raw.started_at is not None:
                ws.write_datetime(r, 4, _naive_local(raw.started_at, tz), book.fmt["datetime"])
            if raw.ended_at is not None:
                ws.write_datetime(r, 5, _naive_local(raw.ended_at, tz), book.fmt["datetime"])
            book.number(ws, r, 6, raw.odometer_start, "odo")
            book.number(ws, r, 7, raw.odometer_end, "odo")
            book.number(ws, r, 8, s.distance_km, "km")
            book.text(ws, r, 9, CATEGORY_LABELS.get(seg.segment_category(t, s), ""))
            src = ctx.sources.get(raw.id, {})
            book.text(ws, r, 10, src.get("filename", ""))
            book.number(ws, r, 11, src.get("row"), "int")
            book.text(ws, r, 12, raw.original_category)
            book.text(ws, r, 13, "" if private_hidden else raw.original_notes)
            book.text(ws, r, 14, ", ".join(raw.quality_flags or []))
            total += s.distance_km
            r += 1
    last = max(first, r - 1)
    book.text(ws, last + 1, 7, "Summa", "total_label")
    rng = f"{xl_rowcol_to_cell(first, 8)}:{xl_rowcol_to_cell(last, 8)}"
    book.formula(ws, last + 1, 8, f"=SUM({rng})", total, "km_bold")
    ws.autofilter(hr, 0, last, len(headers) - 1)
    r = last + 4

    ws.write_string(r, 0, "Ändringshistorik", book.fmt["section"])
    r += 1
    if not ctx.include_change_history:
        book.text(ws, r, 0, "Ändringshistorik är avstängd i exportinställningarna.", "muted")
    else:
        for c, h in enumerate(["Tidpunkt", "Rese-ID", "Åtgärd", "Utförd av", "Före", "Efter"]):
            ws.write_string(r, c, h, book.fmt["header"])
        r += 1
        if not ctx.audit_events:
            book.text(ws, r, 0, "Inga ändringar registrerade för periodens resor.", "muted")
        actor_labels = {"user": "Föraren", "system": "Systemet", "ai": "AI-förslag", "ha": "Föraren (Home Assistant)",
                        "import": "Import"}
        for ev in ctx.audit_events:
            ws.write_datetime(r, 0, _naive_local(ev.at, tz), book.fmt["datetime"])
            book.text(ws, r, 1, short_id(ev.entity_id or ""))
            book.text(ws, r, 2, AUDIT_LABELS.get(ev.action, ev.action))
            book.text(ws, r, 3, actor_labels.get(ev.actor_type, ev.actor_type))
            before, after = _diff(ev.before, ev.after)
            book.text(ws, r, 4, before)
            book.text(ws, r, 5, after)
            r += 1
    ws.freeze_panes(hr + 1, 1)
    book.page_setup(ws, hr, "Källa och ändringshistorik")


def build_workbook(path: Path, ctx: ExportContext) -> dict[str, Any]:
    """Skriver körjournalen till ``path`` och returnerar de summor som skrevs."""
    book = _Book(path, ctx)
    wb = book.wb
    v = ctx.vehicle
    wb.set_properties(
        {
            "title": f"Körjournal {v.registration_number or ''} {ctx.period.label}".strip(),
            "subject": "Körjournal",
            "author": "SMGD DriveLog",
            "comments": (DEMO_WATERMARK if v.is_demo else "")
            + ("" if ctx.is_complete else f" {status_text(ctx)}."),
        }
    )
    rows = journal_rows(ctx)
    overview = wb.add_worksheet(SHEET_OVERVIEW)  # först i filen, fylls i sist
    jr = _journal_sheet(book, rows)
    mr = _monthly_sheet(book, rows, jr)
    _energy_sheet(book)
    _quality_sheet(book, rows)
    _sources_sheet(book, rows)
    _overview_sheet(book, overview, rows, jr, mr)
    overview.activate()
    wb.close()
    tot = mr["facit"]["total"]
    kinds: dict[str, int] = defaultdict(int)
    for row in rows:
        kinds[row["kind"]] += 1
    return {
        "trips": len(rows),
        "total_km": round(mr["grand_total"], 3),
        "work_km": round(tot.get("work_km", 0), 3),
        "private_km": round(tot.get("private_km", 0), 3),
        "undecided_km": round(tot.get("undecided_km", 0), 3),
        "fuel_liters_verified": round(tot.get("fuel", 0), 3),
        "energy_kwh_verified": round(tot.get("energy", 0), 3),
        "trips_by_category": dict(kinds),
        "months": month_keys(ctx, rows),
    }
