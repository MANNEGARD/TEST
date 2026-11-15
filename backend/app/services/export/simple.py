"""Enkel körjournal för milersättning: ett blad med periodens tjänsteresor.

Underlaget är avsett för den som kör tjänsteresor med egen bil och redovisar dem för
sin arbetsgivare. Det innehåller bara det arbetsgivaren behöver: vem som kört, vilken
bil, och per resa datum, klockslag, start- och slutadress, ärende, mätarställning och
körsträcka.

Endast tjänsteresor som är godkända eller importerade tas med. Privata, oavgjorda och
ännu inte godkända resor utelämnas, och av en blandad resa redovisas bara
tjänste-kilometrarna. Körningen mellan de redovisade resorna är privat och syns därför
som hopp i mätarställningen – det är avsett och anmärks inte.

Förhandsvisningen (``SimpleJournal.as_dict``) och filen (``write_workbook``) byggs av
samma ``SimpleJournal`` och kan därför aldrig visa olika rader eller summor.

Summorna skrivs som riktiga Excel-formler MED förberäknade värden. All text från
användare och importerade filer skrivs som ren text (skydd mot formula injection).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import xlsxwriter
from xlsxwriter.utility import xl_rowcol_to_cell

from app.models import LogicalTrip, Vehicle
from app.models.enums import TimeQuality, TripSource
from app.services.export.workbook import safe_text
from app.services.places.normalize import clean, fold
from app.services.stats import common

SHEET_NAME = "Körjournal"
TITLE = "Körjournal – tjänsteresor"
DEMO_SUFFIX = "DEMODATA"
FOOTNOTE = "Endast tjänsteresor redovisas. Övrig körning med bilen är privat och ingår inte i underlaget."
FOOTNOTE_MIXED = " För delvis privata resor anges endast tjänstedelen under Km."
# Tider som DriveLog räknat fram märks "ca" (hårt mellanslag, så att märkningen aldrig
# hamnar ensam på en rad). Fotnoten står på en egen rad under tabellen.
ESTIMATED_PREFIX = "ca\u00a0"
FOOTNOTE_TIMES = "ca = uträknad ur bilens egna uppgifter, felmarginalen visas i DriveLog; –08:52 = starttid okänd"
LIVE_SOURCES = (TripSource.VOLVO_API, TripSource.DEMO)
EMPTY_TEXT = "Inga tjänsteresor att redovisa i perioden."
# Ett mellanstopp hemma eller vid en privat plats skrivs aldrig med adress (A.5).
PRIVATE_STOP = "Privat stopp"

# (nyckel, rubrik, kolumnbredd i Excels teckenenheter). Tabellen skrivs ut i 100 % skala
# och bredderna är avvägda så att den fyller en liggande A4 med smala marginaler.
# Textkolumnerna får så mycket bredd som möjligt: ju färre adresser och ärenden som
# bryts på två rader, desto färre sidor blir underlaget. Mätarställningens två kolumner
# delar därför en gemensam rubrik ("Mätarställning" över "start" och "slut").
COLUMNS: list[tuple[str, str, float]] = [
    ("date", "Datum", 10),
    ("time", "Tid", 10.5),
    ("from", "Från", 25.5),
    ("to", "Till", 28),
    ("purpose", "Ärende", 33.5),
    ("odometer_start", "Mätarställning start", 9),
    ("odometer_end", "Mätarställning slut", 9),
    ("km", "Km", 7.5),
]
INFO_RIGHT_COL = 3  # sidhuvudets högra spalt: etikett i "Till"-kolumnen, värde i "Ärende"-kolumnen
ODOMETER_GROUP = "Mätarställning"  # gemensam rubrik över de två mätarkolumnerna
ODOMETER_SUB = {"odometer_start": "start", "odometer_end": "slut"}
HEADERS = [title for _, title, _ in COLUMNS]
COL = {key: i for i, (key, _, _) in enumerate(COLUMNS)}
WIDTH = {key: width for key, _, width in COLUMNS}
NUMERIC = ("odometer_start", "odometer_end", "km")

MONTHS_SV = ["Januari", "Februari", "Mars", "April", "Maj", "Juni", "Juli", "Augusti", "September", "Oktober",
             "November", "December"]

# Tider vars värde bara är en gräns eller saknas skrivs aldrig ut som klockslag.
UNKNOWN_TIME = (TimeQuality.BOUNDED, TimeQuality.UNKNOWN)

FONT = "Calibri"
FONT_SIZE = 10
INK = "#1F2933"  # brödtext
MUTED = "#667085"  # etiketter och fotnot
HEADER_BG = "#2F3B4B"  # diskret mörk rubrikrad
LINE = "#E1E5EA"  # tunna linjer mellan raderna
ZEBRA = "#F7F9FB"  # mycket ljus toning på varannan rad

# Radhöjder i punkter. Alla rader får en uttrycklig höjd så att sidbrytningarna kan
# beräknas i förväg (summeringen ska inte hamna ensam på en sida).
TITLE_PT = 25.0
INFO_ROW_PT = 14.0
SPACER_PT = 6.0
HEADER_ROWS = 2  # rubriken har två rader: "Mätarställning" över "start" och "slut"
HEADER_PT = 30.0  # rubrikens hela höjd (båda raderna)
LINE_PT = 12.75  # en textrad i tabellen
ROW_PAD_PT = 3.0  # luft över och under texten – kompakt, så att en månad ryms på få sidor
TOTAL_PT = 19.5
SUM_ROW_PT = 15.0
NOTE_PT = 16.5

# Utskrift: A4 liggande, 100 % skala.
PAGE_W_IN, PAGE_H_IN = 11.69, 8.27
MARGIN_LR_IN, MARGIN_TOP_IN, MARGIN_BOTTOM_IN = 0.3, 0.35, 0.55
PAGE_FILL = 0.98  # säkerhetsmarginal mot avvikande radhöjder i olika program


# ----------------------------------------------------------------------------- datamodell


@dataclass(frozen=True)
class SimpleRow:
    """En redovisad tjänsteresa – exakt det som står på raden i bladet."""

    trip_id: uuid.UUID
    date: date | None
    time: str
    from_address: str
    to_address: str
    via: tuple[str, ...]
    purpose: str
    odometer_start: float | None
    odometer_end: float | None
    km: float
    mixed: bool = False  # resan innehåller även privat körning (endast tjänstedelen redovisas)
    time_note: bool = False  # tiden behöver fotnoten om "ca" och okänd starttid

    @property
    def to_text(self) -> str:
        """Cellen "Till": slutdestinationen och, för sammanslagna resor, mellanstoppen."""
        if not self.via:
            return self.to_address
        via = f"via {'; '.join(self.via)}"
        return f"{self.to_address}\n{via}" if self.to_address else via

    def as_dict(self) -> dict[str, Any]:
        return {
            "trip_id": str(self.trip_id),
            "date": self.date.isoformat() if self.date else None,
            "time": self.time,
            "from": self.from_address,
            "to": self.to_text,
            "purpose": self.purpose,
            "odometer_start": _odometer(self.odometer_start),
            "odometer_end": _odometer(self.odometer_end),
            "km": self.km,
        }


def _odometer(value: float | None) -> int | None:
    """Mätarställningen som heltal (så som den står i bladet)."""
    return None if value is None else int(round(value))


@dataclass
class SimpleJournal:
    """Allt som den enkla exporten innehåller (ingen fil skrivs förrän ``write_workbook``)."""

    vehicle: Vehicle
    tz: ZoneInfo
    period_label: str
    driver_name: str = ""
    employer_name: str = ""
    rate_per_mil: float = 0.0
    show_amount: bool = True
    rows: list[SimpleRow] = field(default_factory=list)
    excluded: dict[str, Any] = field(default_factory=dict)

    @property
    def title(self) -> str:
        return f"{TITLE} – {DEMO_SUFFIX}" if self.vehicle.is_demo else TITLE

    @property
    def vehicle_label(self) -> str:
        v = self.vehicle
        return " ".join(clean(p) for p in (v.make, v.model) if p and clean(p)) or clean(v.name)

    @property
    def registration_number(self) -> str:
        return clean(self.vehicle.registration_number)

    @property
    def vehicle_text(self) -> str:
        """Raden "Bil": märke och modell samt registreringsnummer."""
        label, reg = self.vehicle_label, self.registration_number
        if label and reg:
            return f"{label}, {reg}"
        return label or reg

    @property
    def amount_shown(self) -> bool:
        return self.show_amount and self.rate_per_mil > 0

    @property
    def footnote(self) -> str:
        return FOOTNOTE + (FOOTNOTE_MIXED if any(r.mixed for r in self.rows) else "")

    @property
    def time_footnote(self) -> str:
        """Fotnoten om uträknade och okända tider, eller tom text när ingen rad behöver den."""
        return FOOTNOTE_TIMES if any(r.time_note for r in self.rows) else ""

    def totals(self) -> dict[str, Any]:
        """Summorna – samma tal som formlernas cachevärden och som dashboardens milersättning."""
        km = round(sum(r.km for r in self.rows), 1)
        mil = common.km_to_mil_exact(km)
        return {
            "trips": len(self.rows),
            "km": km,
            "mil": mil,
            "rate_per_mil": self.rate_per_mil,
            "show_amount": self.show_amount,
            "amount": common.reimbursement_amount(mil, self.rate_per_mil) if self.amount_shown else None,
        }

    def header(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "period": self.period_label,
            "driver": self.driver_name,
            "employer": self.employer_name or None,
            "vehicle": self.vehicle_label,
            "registration_number": self.registration_number or None,
        }

    def as_dict(self) -> dict[str, Any]:
        """Förhandsvisningen: exakt de rader och summor som filen innehåller."""
        return {
            "period_label": self.period_label,
            "header": self.header(),
            "columns": list(HEADERS),
            "rows": [r.as_dict() for r in self.rows],
            "totals": self.totals(),
            "excluded": self.excluded,
        }


# ----------------------------------------------------------------------------- urval och rader


def sv_number(value: float, decimals: int = 1) -> str:
    return f"{value:.{decimals}f}".replace(".", ",")


def rate_text(rate: float) -> str:
    """Kronor per mil i löptext: 25 → "25", 18.5 → "18,50"."""
    return f"{rate:.0f}" if float(rate).is_integer() else sv_number(rate, 2)


def time_text(trip: LogicalTrip, tz: ZoneInfo) -> str:
    """Klockslagen "08:31–09:04" i lokal tid; "(+1)" när resan slutar ett senare dygn.

    En okänd tid hittas aldrig på: "–09:04" när bara sluttiden är känd, "08:31–" när bara
    starttiden är känd och tom text när ingen av dem är känd. En tid som DriveLog räknat
    fram ur bilens uppgifter (``estimated``) skrivs "ca 07:35", både start och slut.
    """
    start = trip.started_at if trip.start_time_quality not in UNKNOWN_TIME else None
    end = trip.ended_at if trip.end_time_quality not in UNKNOWN_TIME else None
    s = start.astimezone(tz) if start is not None else None
    e = end.astimezone(tz) if end is not None else None
    s_text = _clock(s, trip.start_time_quality) if s is not None else ""
    e_text = _clock(e, trip.end_time_quality) if e is not None else ""
    if s is not None and e is not None:
        text = f"{s_text}–{e_text}"
        days = (e.date() - s.date()).days
        return f"{text} (+{days})" if days > 0 else text
    if e is not None:
        return f"–{e_text}"
    if s is not None:
        return f"{s_text}–"
    return ""


def _clock(value: datetime, quality: str | None) -> str:
    text = f"{value:%H:%M}"
    return f"{ESTIMATED_PREFIX}{text}" if quality == TimeQuality.ESTIMATED else text


def needs_time_note(trip: LogicalTrip, text: str) -> bool:
    """Behöver raden fotnoten? Vid "ca", och vid okänd starttid på en resa från bilen."""
    if ESTIMATED_PREFIX in text:
        return True
    return text.startswith("–") and getattr(trip, "source", None) in LIVE_SOURCES


def _via(trip: LogicalTrip, private_ids: frozenset[str] = frozenset()) -> tuple[str, ...]:
    """Mellanstoppens adresser i ordning, utan tomma och utan upprepningar i följd.

    Ett stopp hemma eller vid en privat plats (``private_ids``) skrivs "Privat stopp".
    """
    out: list[str] = []
    for stop in trip.via or []:
        if not isinstance(stop, dict):
            continue
        if stop.get("location_id") and str(stop["location_id"]) in private_ids:
            address = PRIVATE_STOP
        else:
            address = clean(stop.get("address"))
        if address and (not out or out[-1] != address):
            out.append(address)
    return tuple(out)


def private_stop_ids(trips: list[LogicalTrip]) -> frozenset[str]:
    """Id för mellanstopp som är hemmet eller en privat plats (A.5)."""
    from sqlalchemy import select
    from sqlalchemy.orm import object_session

    from app.models import KnownLocation
    from app.models.enums import LocationType

    ids: set[uuid.UUID] = set()
    for t in trips:
        for stop in t.via or []:
            if isinstance(stop, dict) and stop.get("location_id"):
                try:
                    ids.add(uuid.UUID(str(stop["location_id"])))
                except ValueError:
                    continue
    session = object_session(trips[0]) if trips else None
    if not ids or session is None:
        return frozenset()
    rows = session.execute(
        select(KnownLocation.id).where(
            KnownLocation.id.in_(ids), KnownLocation.type.in_((LocationType.HOME, LocationType.PRIVATE))
        )
    ).scalars()
    return frozenset(str(i) for i in rows)


def purpose_text(trip: LogicalTrip) -> str:
    """Ärendet, kompletterat med besökta företag/kontaktpersoner om de inte redan framgår."""
    text = clean(trip.purpose)
    contacts = clean(trip.contacts)
    if contacts and fold(contacts) not in fold(text):
        text = f"{text} – {contacts}" if text else contacts
    return text


def _row_time(trip: LogicalTrip) -> datetime | None:
    return trip.started_at or common.trip_time(trip)


def build_row(trip: LogicalTrip, tz: ZoneInfo, private_ids: frozenset[str] = frozenset()) -> SimpleRow:
    when = _row_time(trip)
    km = common.reported_work_km(trip)
    per_category = common.segment_km(trip)
    time = time_text(trip, tz)
    return SimpleRow(
        trip_id=trip.id,
        date=when.astimezone(tz).date() if when is not None else None,
        time=time,
        from_address=clean(trip.start_address),
        to_address=clean(trip.end_address),
        via=_via(trip, private_ids),
        purpose=purpose_text(trip),
        odometer_start=trip.odometer_start,
        odometer_end=trip.odometer_end,
        km=km,
        mixed=per_category["private"] + per_category["undecided"] > common.KM_EPS,
        time_note=needs_time_note(trip, time),
    )


def excluded_counts(trips: list[LogicalTrip]) -> dict[str, Any]:
    """Resor i perioden med tjänstekörning som inte tas med i underlaget, och varför.

    * ``pending_approval`` – tjänsteresa vars klassning ännu inte är godkänd.
    * ``undecided`` – resan har sträcka som varken är klassad som arbete eller privat.

    Privata resor räknas inte: de redovisas aldrig. Godkända tjänsteresor som saknar
    syfte tas med i underlaget och redovisas som fel i exportkontrollerna.
    """
    undecided: list[LogicalTrip] = []
    pending: list[LogicalTrip] = []
    for t in trips:
        if common.is_reported(t):
            continue
        km = common.segment_km(t)
        if km["undecided"] > common.KM_EPS:
            undecided.append(t)
        elif km["work"] > common.KM_EPS:
            pending.append(t)
    return {
        "pending_approval": {
            "count": len(pending),
            "km": round(sum(common.segment_km(t)["work"] for t in pending), 1),
            "trip_ids": [str(t.id) for t in pending][:200],
        },
        "undecided": {
            "count": len(undecided),
            "km": round(sum(common.segment_km(t)["undecided"] for t in undecided), 1),
            "trip_ids": [str(t.id) for t in undecided][:200],
        },
    }


def whole_month(start: datetime | None, end: datetime | None, tz: ZoneInfo) -> date | None:
    """Första dagen i månaden om perioden är exakt en kalendermånad, annars ``None``."""
    if start is None or end is None:
        return None
    first = start.astimezone(tz)
    last = end.astimezone(tz)
    if first.time() != datetime.min.time() or last.time() != datetime.min.time() or first.day != 1:
        return None
    next_month = (first.date().replace(day=28) + timedelta(days=4)).replace(day=1)
    return first.date() if last.date() == next_month else None


def period_label(start: datetime | None, end: datetime | None, tz: ZoneInfo, rows: list[SimpleRow]) -> str:
    """"Juni 2026" för en hel kalendermånad, annars "2026-06-01 – 2026-08-31".

    Saknas periodgränser (urval av resor) anges första och sista redovisade resdag.
    """
    month = whole_month(start, end, tz)
    if month is not None:
        return f"{MONTHS_SV[month.month - 1]} {month.year}"
    if start is not None and end is not None:
        first = start.astimezone(tz).date()
        last = (end.astimezone(tz) - timedelta(seconds=1)).date()
        return first.isoformat() if first == last else f"{first.isoformat()} – {last.isoformat()}"
    days = sorted(r.date for r in rows if r.date is not None)
    if not days:
        return "Inga resor"
    if days[0] == days[-1]:
        return days[0].isoformat()
    return f"{days[0].isoformat()} – {days[-1].isoformat()}"


def filename_part(start: datetime | None, end: datetime | None, tz: ZoneInfo) -> str:
    """"2026-06" för en hel kalendermånad, annars "2026-06-01_2026-08-31"."""
    month = whole_month(start, end, tz)
    if month is not None:
        return month.strftime("%Y-%m")
    if start is None or end is None:
        return "hela-historiken"
    first = start.astimezone(tz).date()
    last = (end.astimezone(tz) - timedelta(seconds=1)).date()
    return f"{first.isoformat()}_{last.isoformat()}"


def build_journal(
    *,
    vehicle: Vehicle,
    trips: list[LogicalTrip],
    start: datetime | None,
    end: datetime | None,
    tz: ZoneInfo,
    driver_name: str = "",
    employer_name: str = "",
    rate_per_mil: float = 0.0,
    show_amount: bool = True,
) -> SimpleJournal:
    """Väljer ut periodens redovisade tjänsteresor, sorterade på starttid."""
    reported = [t for t in trips if common.is_reported(t)]
    # Stabil sortering: resor utan känd tid (ovanligt) behåller sin plats sist.
    reported.sort(key=lambda t: (_row_time(t) is None, _row_time(t) or datetime.min.replace(tzinfo=tz)))
    private_ids = private_stop_ids(reported)
    rows = [build_row(t, tz, private_ids) for t in reported]
    return SimpleJournal(
        vehicle=vehicle,
        tz=tz,
        period_label=period_label(start, end, tz, rows),
        driver_name=clean(driver_name),
        employer_name=clean(employer_name),
        rate_per_mil=float(rate_per_mil),
        show_amount=bool(show_amount),
        rows=rows,
        excluded=excluded_counts(trips),
    )


# ----------------------------------------------------------------------------- radhöjder

_NARROW = set("iIjlft.,:;'|!()[] ")
_WIDE = set("mwMW@%—")


def _text_units(text: str) -> float:
    """Ungefärlig textbredd i kolumnbreddsenheter (en enhet ≈ en siffra i Calibri 11).

    Räknat för Calibri 11; tabellens mindre teckenstorlek tas hänsyn till i ``wrapped_lines``.
    """
    units = 0.0
    for ch in text:
        if ch in _NARROW:
            units += 0.5
        elif ch in _WIDE:
            units += 1.4
        elif ch.isupper():
            units += 1.12
        else:
            units += 0.9
    return units


def wrapped_lines(text: str, width: float) -> int:
    """Antal rader texten tar i en cell med radbrytning. Räknar hellre en rad för mycket än för lite."""
    usable = max(width - 1.5, 4.0) * 11 / FONT_SIZE  # kolumnbredden i tabellens teckenstorlek
    lines = 0
    for paragraph in (text or "").split("\n"):
        current = 0.0
        count = 1
        for word in paragraph.split(" "):
            w = _text_units(word)
            if current and current + 0.55 + w > usable:
                count += 1
                current = 0.0
            if w > usable:  # ett ord som är längre än kolumnen bryts mitt i
                count += int(w // usable)
                w = w % usable
            current += (0.55 if current else 0.0) + w
        lines += count
    return max(lines, 1)


def row_height(row: SimpleRow) -> float:
    lines = max(
        wrapped_lines(row.time, WIDTH["time"]),  # "(+1)" efter en resa över midnatt får en egen rad
        wrapped_lines(row.from_address, WIDTH["from"]),
        wrapped_lines(row.to_text, WIDTH["to"]),
        wrapped_lines(row.purpose, WIDTH["purpose"]),
    )
    return LINE_PT * lines + ROW_PAD_PT


def page_breaks(top_pt: float, rows_pt: list[float], tail_pt: float, first_row: int) -> list[int]:
    """Radnummer (0-baserade) där en ny sida börjar.

    ``top_pt`` är höjden av allt ovanför första dataraden på första sidan (sidhuvud och
    rubrikrad); på följande sidor upprepas rubrikraden. Summeringen (``tail_pt``) hålls
    ihop med minst de två sista resorna, så att den aldrig hamnar ensam på en sida.
    """
    page = (PAGE_H_IN - MARGIN_TOP_IN - MARGIN_BOTTOM_IN) * 72 * PAGE_FILL
    breaks: list[int] = []  # index i rows_pt
    used = top_pt
    for i, h in enumerate(rows_pt):
        if used + h > page and i > 0 and (not breaks or breaks[-1] != i):
            breaks.append(i)
            used = HEADER_PT
        used += h
    if rows_pt and used + tail_pt > page:
        keep = min(2, len(rows_pt) - 1)
        if keep > 0:
            at = len(rows_pt) - keep
            breaks = [b for b in breaks if b < at]
            breaks.append(at)
    return [first_row + b for b in breaks]


# ----------------------------------------------------------------------------- arbetsbok


def _footer_text(value: str) -> str:
    return value.replace("&", "&&")


def write_workbook(path: Path, journal: SimpleJournal) -> dict[str, Any]:
    """Skriver den enkla körjournalen till ``path`` och returnerar summorna samt bladets radlägen.

    Bladets uppställning:

    * rad 1: titel, därefter högst två rader i två spalter: period och förare, arbetsgivare och bil
    * tabellen med en rad per tjänsteresa
    * summering direkt under tabellen: antal resor och km, mil och (valfritt) ersättning
    * en förklarande rad om att endast tjänsteresor redovisas
    """
    wb = xlsxwriter.Workbook(
        str(path),
        {"strings_to_formulas": False, "strings_to_urls": False, "strings_to_numbers": False, "remove_timezone": True},
    )
    totals = journal.totals()
    reg = journal.registration_number
    wb.set_properties(
        {
            "title": " ".join(p for p in (journal.title, reg, journal.period_label) if p),
            "subject": "Körjournal för tjänsteresor med egen bil",
            "author": "SMGD DriveLog",
        }
    )
    ws = wb.add_worksheet(SHEET_NAME)
    ws.hide_gridlines(2)

    base = {"font_name": FONT, "font_size": FONT_SIZE, "font_color": INK, "valign": "vcenter"}
    f = wb.add_format
    fmt_title = f({**base, "font_size": 16, "bold": True})
    fmt_label = f({**base, "font_color": MUTED})
    fmt_value = f({**base})
    fmt_label_right = f({**base, "font_color": MUTED, "align": "right"})
    fmt_value_right = f({**base, "indent": 1})
    head = {**base, "bold": True, "font_color": "#FFFFFF", "bg_color": HEADER_BG, "text_wrap": True}
    fmt_head = f({**head, "align": "left", "indent": 1})
    fmt_head_num = f({**head, "align": "right", "indent": 1})
    fmt_head_group = f({**head, "align": "center", "valign": "bottom"})
    fmt_head_sub = f({**head, "align": "right", "indent": 1, "valign": "top"})

    def cell_formats(shaded: bool) -> dict[str, Any]:
        row = {**base, "valign": "top", "bottom": 1, "bottom_color": LINE}
        if shaded:
            row["bg_color"] = ZEBRA
        return {
            "date": f({**row, "num_format": "yyyy-mm-dd", "align": "left", "indent": 1}),
            "time": f({**row, "text_wrap": True, "align": "left", "indent": 1}),
            "text": f({**row, "text_wrap": True, "align": "left", "indent": 1}),
            "odo": f({**row, "num_format": "0", "align": "right", "indent": 1}),
            "km": f({**row, "num_format": "#,##0.0", "align": "right", "indent": 1}),
        }

    fmt_rows = (cell_formats(False), cell_formats(True))
    total = {**base, "bold": True, "top": 1, "top_color": HEADER_BG}
    fmt_total = f({**total, "align": "left", "indent": 1})
    fmt_total_count = f({**total, "align": "left", "indent": 1, "num_format": '[=1]0" resa";0" resor"'})
    fmt_total_km = f({**total, "align": "right", "indent": 1, "num_format": "#,##0.0"})
    sums = {**base, "align": "right", "indent": 1}
    fmt_sum_label = f({**sums, "font_color": MUTED})
    fmt_sum_mil = f({**sums, "num_format": '#,##0.00" mil"'})
    fmt_amount_label = f({**sums, "bold": True})
    fmt_amount = f({**sums, "bold": True, "num_format": '#,##0.00" kr"'})
    fmt_note = f({**base, "font_size": 9, "italic": True, "font_color": MUTED})
    fmt_empty = f({**base, "italic": True, "font_color": MUTED, "align": "left", "indent": 1, "bottom": 1,
                   "bottom_color": LINE})

    last_col = len(COLUMNS) - 1
    for c, (_, _, width) in enumerate(COLUMNS):
        ws.set_column(c, c, width)

    # --- sidhuvud ---------------------------------------------------------------------------
    ws.write_string(0, 0, safe_text(journal.title), fmt_title)
    ws.set_row(0, TITLE_PT)
    info = [("Period", journal.period_label), ("Förare", journal.driver_name)]
    if journal.employer_name:
        info.append(("Arbetsgivare", journal.employer_name))  # raden utelämnas när arbetsgivare saknas
    if journal.vehicle_text:
        info.append(("Bil", journal.vehicle_text))
    # Uppgifterna står i två spalter (vänster: period och förare, höger: arbetsgivare och bil)
    # så att sidhuvudet tar så lite höjd som möjligt.
    info_rows = (len(info) + 1) // 2
    for i, (label, value) in enumerate(info):
        r = 1 + i % info_rows
        if i < info_rows:
            ws.set_row(r, INFO_ROW_PT)
            ws.write_string(r, 0, label, fmt_label)
            ws.write_string(r, 1, safe_text(value), fmt_value)
        else:
            ws.write_string(r, INFO_RIGHT_COL, label, fmt_label_right)
            ws.write_string(r, INFO_RIGHT_COL + 1, safe_text(value), fmt_value_right)
    spacer = 1 + info_rows
    ws.set_row(spacer, SPACER_PT)

    header_row = spacer + 1
    first = header_row + HEADER_ROWS
    last = first + max(len(journal.rows), 1) - 1
    total_row = last + 1
    mil_row = total_row + 1
    amount_row = mil_row + 1 if journal.amount_shown else None
    note_row = (amount_row if amount_row is not None else mil_row) + 1
    time_note_row = note_row + 1 if journal.time_footnote else None
    last_note_row = time_note_row if time_note_row is not None else note_row

    # --- tabell -----------------------------------------------------------------------------
    sub_row = header_row + 1
    ws.set_row(header_row, HEADER_PT / HEADER_ROWS)
    ws.set_row(sub_row, HEADER_PT / HEADER_ROWS)
    for c, (key, title, _) in enumerate(COLUMNS):
        if key in ODOMETER_SUB:
            ws.write_string(sub_row, c, ODOMETER_SUB[key], fmt_head_sub)
        else:
            ws.merge_range(header_row, c, sub_row, c, title, fmt_head_num if key in NUMERIC else fmt_head)
    ws.merge_range(header_row, COL["odometer_start"], header_row, COL["odometer_end"], ODOMETER_GROUP, fmt_head_group)
    heights: list[float] = []
    for i, row in enumerate(journal.rows):
        r = first + i
        fm = fmt_rows[i % 2]
        height = row_height(row)
        heights.append(height)
        ws.set_row(r, height)
        if row.date is not None:
            ws.write_datetime(r, COL["date"], row.date, fm["date"])
        else:
            ws.write_blank(r, COL["date"], None, fm["date"])
        ws.write_string(r, COL["time"], row.time, fm["time"])
        ws.write_string(r, COL["from"], safe_text(row.from_address), fm["text"])
        ws.write_string(r, COL["to"], safe_text(row.to_text), fm["text"])
        ws.write_string(r, COL["purpose"], safe_text(row.purpose), fm["text"])
        for key in ("odometer_start", "odometer_end"):
            value = _odometer(getattr(row, key))
            if value is None:
                ws.write_blank(r, COL[key], None, fm["odo"])  # saknad mätarställning lämnas tom
            else:
                ws.write_number(r, COL[key], value, fm["odo"])
        ws.write_number(r, COL["km"], row.km, fm["km"])
    if not journal.rows:
        # En tom rad håller formelområdena giltiga även när perioden saknar tjänsteresor.
        heights.append(LINE_PT + ROW_PAD_PT)
        ws.set_row(first, heights[0])
        ws.merge_range(first, 0, first, last_col - 1, EMPTY_TEXT, fmt_empty)
        ws.write_blank(first, last_col, None, fmt_empty)

    # --- summering direkt under tabellen ------------------------------------------------------
    km_col = COL["km"]
    value_col = COL["odometer_end"]  # mil och belopp står i de två sista kolumnerna (sammanfogade)
    label_first, label_last = COL["to"], COL["odometer_start"]
    km_range = f"{xl_rowcol_to_cell(first, km_col)}:{xl_rowcol_to_cell(last, km_col)}"
    km_total_cell = xl_rowcol_to_cell(total_row, km_col)
    mil_cell = xl_rowcol_to_cell(mil_row, value_col)

    ws.set_row(total_row, TOTAL_PT)
    ws.write_string(total_row, 0, "Summa", fmt_total)
    for c in range(2, km_col):
        ws.write_blank(total_row, c, None, fmt_total)
    ws.write_formula(total_row, COL["time"], f"=COUNT({km_range})", fmt_total_count, totals["trips"])
    ws.write_formula(total_row, km_col, f"=SUM({km_range})", fmt_total_km, totals["km"])

    ws.set_row(mil_row, SUM_ROW_PT)
    ws.merge_range(mil_row, label_first, mil_row, label_last, "Mil (km ÷ 10)", fmt_sum_label)
    ws.merge_range(mil_row, value_col, mil_row, last_col, "", fmt_sum_mil)
    ws.write_formula(mil_row, value_col, f"=ROUND({km_total_cell}/10,2)", fmt_sum_mil, totals["mil"])
    if amount_row is not None:
        rate = journal.rate_per_mil
        ws.set_row(amount_row, SUM_ROW_PT)
        ws.merge_range(amount_row, label_first, amount_row, label_last, f"Ersättning à {rate_text(rate)} kr/mil",
                       fmt_amount_label)
        ws.merge_range(amount_row, value_col, amount_row, last_col, "", fmt_amount)
        ws.write_formula(amount_row, value_col, f"=ROUND({mil_cell}*{rate:.10g},2)", fmt_amount, totals["amount"])

    # Högst en förklarande rad: varför mätarställningen hoppar mellan resorna.
    ws.set_row(note_row, NOTE_PT)
    ws.write_string(note_row, 0, journal.footnote, fmt_note)
    if time_note_row is not None:
        ws.set_row(time_note_row, NOTE_PT)
        ws.write_string(time_note_row, 0, journal.time_footnote, fmt_note)

    # --- skärm och utskrift -------------------------------------------------------------------
    ws.freeze_panes(first, 0)
    ws.set_landscape()
    ws.set_paper(9)  # A4
    # 100 % skala: kolumnbredderna fyller en sidas bredd. (Anpassa-till-sida används inte,
    # eftersom Excel då bortser från sidbrytningarna som håller ihop summeringen.)
    ws.set_print_scale(100)
    ws.set_margins(left=MARGIN_LR_IN, right=MARGIN_LR_IN, top=MARGIN_TOP_IN, bottom=MARGIN_BOTTOM_IN)
    ws.center_horizontally()
    ws.repeat_rows(header_row, header_row + HEADER_ROWS - 1)
    ws.print_area(0, 0, last_note_row, last_col)
    top_pt = TITLE_PT + INFO_ROW_PT * info_rows + SPACER_PT + HEADER_PT
    tail_pt = TOTAL_PT + SUM_ROW_PT * (note_row - total_row - 1) + NOTE_PT * (last_note_row - note_row + 1)
    breaks = page_breaks(top_pt, heights, tail_pt, first)
    if breaks:
        ws.set_h_pagebreaks(breaks)
    footer_left = ", ".join(p for p in (reg, journal.period_label) if p)
    ws.set_footer(f'&L&"Calibri,Regular"&8{_footer_text(footer_left)}&R&"Calibri,Regular"&8Sida &P av &N'[:250],
                  {"margin": 0.25})
    wb.close()
    return {
        **totals,
        "sheet": SHEET_NAME,
        "header_row": header_row,
        "header_rows": HEADER_ROWS,
        "first_row": first,
        "last_row": last,
        "total_row": total_row,
        "mil_row": mil_row,
        "amount_row": amount_row,
        "note_row": note_row,
        "time_note_row": time_note_row,
        "value_col": value_col,
        "page_breaks": breaks,
    }
