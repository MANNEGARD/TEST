"""Enkel körjournal för milersättning: ett blad med periodens tjänsteresor.

Underlaget är avsett för den som kör tjänsteresor med egen bil och redovisar dem för
sin arbetsgivare. Det innehåller bara det arbetsgivaren behöver: vem som kört, vilken
bil, och per resa datum, start- och slutadress, ärende, mätarställning och körsträcka.

Endast tjänsteresor som är godkända eller importerade tas med. Privata, oavgjorda och
ännu inte godkända resor utelämnas, och av en blandad resa redovisas bara
tjänste-kilometrarna. Körningen mellan de redovisade resorna är privat och syns därför
som hopp i mätarställningen – det är avsett och anmärks inte.

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
from app.services.export.workbook import safe_text
from app.services.places.normalize import clean, fold
from app.services.stats import common

SHEET_NAME = "Körjournal"
TITLE = "Körjournal – tjänsteresor"
DEMO_SUFFIX = "DEMODATA"
FOOTNOTE = "Endast tjänsteresor redovisas. Övrig körning med bilen är privat och ingår inte i underlaget."
EMPTY_TEXT = "Inga tjänsteresor att redovisa i perioden."

# (nyckel, rubrik, kolumnbredd i tecken). Bredderna är avvägda så att tabellen fyller en
# liggande A4 i 100 % skala.
COLUMNS: list[tuple[str, str, float]] = [
    ("date", "Datum", 12),
    ("from", "Från", 30),
    ("to", "Till", 30),
    ("purpose", "Ärende", 37),
    ("odometer_start", "Mätarställning start", 13),
    ("odometer_end", "Mätarställning slut", 13),
    ("km", "Km", 9),
]
HEADERS = [title for _, title, _ in COLUMNS]
COL = {key: i for i, (key, _, _) in enumerate(COLUMNS)}
WIDTH = {key: width for key, _, width in COLUMNS}

MONTHS_SV = ["Januari", "Februari", "Mars", "April", "Maj", "Juni", "Juli", "Augusti", "September", "Oktober",
             "November", "December"]

FONT = "Calibri"
FONT_SIZE = 10
INK = "#1F2933"  # brödtext
MUTED = "#667085"  # etiketter och fotnot
HEADER_BG = "#2F3B4B"  # diskret mörk rubrikrad
LINE = "#E1E5EA"  # tunna linjer mellan raderna
ZEBRA = "#F6F8FA"  # mycket ljus toning på varannan rad

LINE_PT = 13.0  # en textrad i brödtexten
ROW_PAD_PT = 4.5  # luft över och under texten
INFO_ROW_PT = 16.0  # raderna i sidhuvudet


# ----------------------------------------------------------------------------- datamodell


@dataclass(frozen=True)
class SimpleRow:
    """En redovisad tjänsteresa – exakt det som står på raden i bladet."""

    trip_id: uuid.UUID
    date: date | None
    started_at: datetime | None
    ended_at: datetime | None
    from_address: str
    to_address: str
    via: tuple[str, ...]
    purpose: str
    odometer_start: float | None
    odometer_end: float | None
    km: float

    @property
    def to_text(self) -> str:
        """Cellen "Till": slutdestinationen, med mellanstoppen på en egen rad."""
        if not self.via:
            return self.to_address
        return f"{self.to_address}\nvia {', '.join(self.via)}".strip()

    def as_dict(self) -> dict[str, Any]:
        return {
            "trip_id": str(self.trip_id),
            "date": self.date.isoformat() if self.date else None,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "from": self.from_address,
            "to": self.to_text,
            "to_address": self.to_address,
            "via": list(self.via),
            "purpose": self.purpose,
            "odometer_start": self.odometer_start,
            "odometer_end": self.odometer_end,
            "km": self.km,
        }


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
    def amount_shown(self) -> bool:
        return self.show_amount and self.rate_per_mil > 0

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
            "currency": "SEK",
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
        return {
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


def _via(trip: LogicalTrip) -> tuple[str, ...]:
    """Mellanstoppens adresser i ordning, utan tomma och utan upprepningar i följd."""
    out: list[str] = []
    for stop in trip.via or []:
        address = clean(stop.get("address"))
        if address and (not out or out[-1] != address):
            out.append(address)
    return tuple(out)


def _purpose(trip: LogicalTrip, work_km: float) -> str:
    """Ärendet, kompletterat med besökta företag/kontaktpersoner om de inte redan framgår."""
    text = clean(trip.purpose)
    contacts = clean(trip.contacts)
    if contacts and fold(contacts) not in fold(text):
        text = f"{text} – {contacts}" if text else contacts
    total = sum(common.segment_km(trip).values())
    if total - work_km >= 0.05:
        # Blandad resa: mätarställningen avser hela resan, körsträckan bara tjänstedelen.
        part = f"{sv_number(work_km)} av {sv_number(total)} km i tjänsten"
        text = f"{text} ({part})" if text else part
    return text


def build_row(trip: LogicalTrip, tz: ZoneInfo) -> SimpleRow:
    when = trip.started_at or common.trip_time(trip)
    km = common.reported_work_km(trip)
    return SimpleRow(
        trip_id=trip.id,
        date=when.astimezone(tz).date() if when is not None else None,
        started_at=trip.started_at,
        ended_at=trip.ended_at,
        from_address=clean(trip.start_address),
        to_address=clean(trip.end_address),
        via=_via(trip),
        purpose=_purpose(trip, km),
        odometer_start=trip.odometer_start,
        odometer_end=trip.odometer_end,
        km=km,
    )


def excluded_counts(trips: list[LogicalTrip]) -> dict[str, Any]:
    """Resor i perioden som inte tas med i underlaget, och varför.

    * ``undecided`` – resan har sträcka som varken är klassad som arbete eller privat.
    * ``awaiting_approval`` – tjänsteresa vars klassning ännu inte är godkänd.
    * ``private`` – privat resa (redovisas aldrig).
    """
    undecided: list[LogicalTrip] = []
    awaiting: list[LogicalTrip] = []
    private = 0
    for t in trips:
        if common.is_reported(t):
            continue
        km = common.segment_km(t)
        if km["undecided"] > common.KM_EPS:
            undecided.append(t)
        elif km["work"] > common.KM_EPS:
            awaiting.append(t)
        else:
            private += 1
    return {
        "total": len(undecided) + len(awaiting) + private,
        "awaiting_approval": {
            "count": len(awaiting),
            "km": round(sum(common.segment_km(t)["work"] for t in awaiting), 1),
            "trip_ids": [str(t.id) for t in awaiting][:200],
        },
        "undecided": {
            "count": len(undecided),
            "km": round(sum(common.segment_km(t)["undecided"] for t in undecided), 1),
            "trip_ids": [str(t.id) for t in undecided][:200],
        },
        "private": {"count": private},
    }


def period_label(start: datetime | None, end: datetime | None, tz: ZoneInfo, rows: list[SimpleRow]) -> str:
    """"Juni 2026" för en hel kalendermånad, annars "2026-06-01 – 2026-08-31".

    Saknas periodgränser (urval av resor eller hela historiken) anges första och sista
    redovisade resdag.
    """
    if start is not None and end is not None:
        first = start.astimezone(tz).date()
        last = (end.astimezone(tz) - timedelta(seconds=1)).date()
        next_month = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        if first.day == 1 and last == next_month - timedelta(days=1):
            return f"{MONTHS_SV[first.month - 1]} {first.year}"
        return f"{first.isoformat()} – {last.isoformat()}"
    days = sorted(r.date for r in rows if r.date is not None)
    if not days:
        return "Inga resor"
    if days[0] == days[-1]:
        return days[0].isoformat()
    return f"{days[0].isoformat()} – {days[-1].isoformat()}"


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
    """Väljer ut periodens redovisade tjänsteresor (``trips`` är redan sorterade i tid)."""
    rows = [build_row(t, tz) for t in trips if common.is_reported(t)]
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

_NARROW = set("iIjlt.,:;'|!()[] ")
_WIDE = set("mwMW@%—")


def _text_units(text: str) -> float:
    """Ungefärlig textbredd i kolumnbreddsenheter (en enhet ≈ en siffra i Calibri 11)."""
    units = 0.0
    for ch in text:
        if ch in _NARROW:
            units += 0.55
        elif ch in _WIDE:
            units += 1.45
        elif ch.isupper():
            units += 1.15
        else:
            units += 0.98
    return units


def wrapped_lines(text: str, width: float) -> int:
    """Antal rader texten tar i en cell med radbrytning. Räknar hellre en rad för mycket än för lite."""
    usable = max(width - 1.5, 4.0)
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
        wrapped_lines(row.from_address, WIDTH["from"]),
        wrapped_lines(row.to_text, WIDTH["to"]),
        wrapped_lines(row.purpose, WIDTH["purpose"]),
    )
    return LINE_PT * lines + ROW_PAD_PT


# ----------------------------------------------------------------------------- arbetsbok


def _footer_text(value: str) -> str:
    return value.replace("&", "&&")


def write_workbook(path: Path, journal: SimpleJournal) -> dict[str, Any]:
    """Skriver den enkla körjournalen till ``path`` och returnerar summorna samt bladets radlägen.

    Bladets uppställning:

    * rad 1: titel
    * därefter högst fyra rader: period, förare, arbetsgivare och bil till vänster, och till
      höger en liten sammanställning (körsträcka, mil och ersättning)
    * tabellen med en rad per tjänsteresa och en summarad direkt under
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
    fmt_title = f({**base, "font_size": 18, "bold": True})
    fmt_label = f({**base, "font_color": MUTED, "indent": 1})
    fmt_value = f({**base, "indent": 1})
    panel = {**base, "align": "right", "indent": 1}
    fmt_sum_label = f({**panel, "font_color": MUTED})
    fmt_sum_label_bold = f({**panel, "bold": True})
    fmt_sum_km = f({**panel, "num_format": '#,##0.0" km"'})
    fmt_sum_mil = f({**panel, "num_format": '#,##0.00" mil"'})
    fmt_sum_amount = f({**panel, "bold": True, "num_format": '#,##0.00" kr"'})
    head = {**base, "bold": True, "font_color": "#FFFFFF", "bg_color": HEADER_BG, "text_wrap": True}
    fmt_head = f({**head, "align": "left", "indent": 1})
    fmt_head_num = f({**head, "align": "right", "indent": 1})

    def cell_formats(shaded: bool) -> dict[str, Any]:
        row = {**base, "bottom": 1, "bottom_color": LINE}
        if shaded:
            row["bg_color"] = ZEBRA
        return {
            "date": f({**row, "num_format": "yyyy-mm-dd", "align": "left", "indent": 1}),
            "text": f({**row, "text_wrap": True, "align": "left", "indent": 1}),
            "odo": f({**row, "num_format": "#,##0", "align": "right", "indent": 1}),
            "km": f({**row, "num_format": "#,##0.0", "align": "right", "indent": 1}),
        }

    fmt_rows = (cell_formats(False), cell_formats(True))
    total = {**base, "bold": True, "top": 1, "top_color": HEADER_BG}
    fmt_total = f({**total, "align": "left", "indent": 1})
    fmt_total_count = f({**total, "align": "left", "indent": 1, "num_format": '[=1]0" resa";0" resor"'})
    fmt_total_km = f({**total, "align": "right", "indent": 1, "num_format": "#,##0.0"})
    fmt_note = f({**base, "font_size": 9, "italic": True, "font_color": MUTED, "indent": 1})
    fmt_empty = f({**base, "italic": True, "font_color": MUTED, "align": "left", "indent": 1, "bottom": 1,
                   "bottom_color": LINE})

    last_col = len(COLUMNS) - 1
    for c, (_, _, width) in enumerate(COLUMNS):
        ws.set_column(c, c, width)

    # --- sidhuvud ---------------------------------------------------------------------------
    ws.write_string(0, 0, safe_text(journal.title), fmt_title)
    ws.set_row(0, 30)
    info = [("Period", journal.period_label), ("Förare", journal.driver_name)]
    if journal.employer_name:
        info.append(("Arbetsgivare", journal.employer_name))  # raden utelämnas när arbetsgivare saknas
    vehicle_text = f"{journal.vehicle_label} ({reg})" if journal.vehicle_label and reg else journal.vehicle_label or reg
    if vehicle_text:
        info.append(("Bil", vehicle_text))
    summary_rows = 3 if journal.amount_shown else 2
    info_rows = max(len(info), summary_rows)
    for i in range(info_rows):
        ws.set_row(1 + i, INFO_ROW_PT)
    for i, (label, value) in enumerate(info):
        ws.write_string(1 + i, 0, label, fmt_label)
        ws.write_string(1 + i, 1, safe_text(value), fmt_value)
    spacer = 1 + info_rows
    ws.set_row(spacer, 9)

    # Radlägena måste vara kända innan sammanställningen skrivs: dess formler pekar på summaraden.
    header_row = spacer + 1
    first = header_row + 1
    last = first + max(len(journal.rows), 1) - 1
    total_row = last + 1
    note_row = total_row + 1

    # --- liten sammanställning uppe till höger: km, mil och ersättning -------------------------
    label_col, value_col = COL["purpose"], COL["odometer_end"]
    km_row, mil_row = 1, 2
    amount_row = 3 if journal.amount_shown else None
    lines: list[tuple[int, str, str, float, Any, Any]] = [
        (km_row, "Körsträcka i tjänsten", f"={xl_rowcol_to_cell(total_row, COL['km'])}", totals["km"], fmt_sum_label,
         fmt_sum_km),
        (mil_row, "Omräknat till mil", f"=ROUND({xl_rowcol_to_cell(km_row, value_col)}/10,2)", totals["mil"],
         fmt_sum_label, fmt_sum_mil),
    ]
    if amount_row is not None:
        rate = journal.rate_per_mil
        lines.append(
            (amount_row, f"Ersättning à {rate_text(rate)} kr/mil",
             f"=ROUND({xl_rowcol_to_cell(mil_row, value_col)}*{rate:.10g},2)", totals["amount"], fmt_sum_label_bold,
             fmt_sum_amount)
        )
    for row_idx, label, formula, value, label_fmt, value_fmt in lines:
        ws.merge_range(row_idx, label_col, row_idx, value_col - 1, label, label_fmt)
        ws.merge_range(row_idx, value_col, row_idx, last_col, "", value_fmt)
        ws.write_formula(row_idx, value_col, formula, value_fmt, value)

    # --- tabell -----------------------------------------------------------------------------
    ws.set_row(header_row, 30)
    for c, (key, title, _) in enumerate(COLUMNS):
        ws.write_string(header_row, c, title, fmt_head_num if key in ("odometer_start", "odometer_end", "km") else fmt_head)
    for i, row in enumerate(journal.rows):
        r = first + i
        fm = fmt_rows[i % 2]
        ws.set_row(r, row_height(row))
        if row.date is not None:
            ws.write_datetime(r, COL["date"], row.date, fm["date"])
        else:
            ws.write_blank(r, COL["date"], None, fm["date"])
        ws.write_string(r, COL["from"], safe_text(row.from_address), fm["text"])
        ws.write_string(r, COL["to"], safe_text(row.to_text), fm["text"])
        ws.write_string(r, COL["purpose"], safe_text(row.purpose), fm["text"])
        for key in ("odometer_start", "odometer_end"):
            value = getattr(row, key)
            if value is None:
                ws.write_blank(r, COL[key], None, fm["odo"])  # saknad mätarställning lämnas tom
            else:
                ws.write_number(r, COL[key], float(value), fm["odo"])
        ws.write_number(r, COL["km"], row.km, fm["km"])
    if not journal.rows:
        # En tom rad håller formelområdena giltiga även när perioden saknar tjänsteresor.
        ws.set_row(first, LINE_PT + ROW_PAD_PT)
        ws.merge_range(first, 0, first, last_col - 1, EMPTY_TEXT, fmt_empty)
        ws.write_blank(first, last_col, None, fmt_empty)

    # --- summarad direkt under tabellen -------------------------------------------------------
    km_range = f"{xl_rowcol_to_cell(first, COL['km'])}:{xl_rowcol_to_cell(last, COL['km'])}"
    ws.set_row(total_row, 20)
    ws.write_string(total_row, 0, "Summa", fmt_total)
    for c in range(1, last_col):
        ws.write_blank(total_row, c, None, fmt_total)
    ws.write_formula(total_row, COL["purpose"], f"=COUNT({km_range})", fmt_total_count, totals["trips"])
    ws.write_formula(total_row, COL["km"], f"=SUM({km_range})", fmt_total_km, totals["km"])
    # Högst en förklarande rad: varför mätarställningen hoppar mellan resorna.
    ws.set_row(note_row, 18)
    ws.write_string(note_row, 0, FOOTNOTE, fmt_note)

    # --- skärm och utskrift -------------------------------------------------------------------
    ws.freeze_panes(first, 0)
    ws.set_landscape()
    ws.set_paper(9)  # A4
    ws.fit_to_pages(1, 0)  # en sidas bredd, så många sidor på höjden som behövs
    ws.set_margins(left=0.4, right=0.4, top=0.4, bottom=0.55)
    ws.center_horizontally()
    ws.repeat_rows(header_row)
    ws.print_area(0, 0, note_row, last_col)
    footer_left = ", ".join(p for p in (reg, journal.period_label) if p)
    ws.set_footer(f"&L&8 {_footer_text(footer_left)}&R&8 Sida &P av &N"[:250], {"margin": 0.25})
    wb.close()
    return {
        **totals,
        "sheet": SHEET_NAME,
        "header_row": header_row,
        "first_row": first,
        "last_row": last,
        "total_row": total_row,
        "note_row": note_row,
        "km_row": km_row,
        "mil_row": mil_row,
        "amount_row": amount_row,
        "value_col": value_col,
    }
