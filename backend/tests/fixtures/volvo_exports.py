"""Anonymiserade testfiler med exakt samma struktur som Volvos körjournalsexporter.

Innehållet (adresser, anteckningar, mätarställningar) är påhittat, men alla
strukturella egenheter från de verkliga underlagen återskapas:

* Äldre format: blad "Sheet1" (+ tomma "Sheet2"/"Sheet3"), 14 kolumner med Titel och
  Batteriregenerering, datum som text, körtid som tidsintervall-cell, Titel = "".
  Två rader överst med äkta datumceller, körtid som text "01:52 h" och tom Titel.
* Nyare format: blad "Trips", 12 kolumner, allt som text/tal, körtid "00:33 h".
* Raderna ligger i fallande tidsordning, heltal och decimaltal blandas, mätarställning
  är heltal, och körtiden är ibland kortare än tiden mellan start och slut.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import openpyxl

HEADERS_V1 = [
    "Kategori", "Startade", "Mätarställning start (km)", "Startposition", "Stannade",
    "Mätarställning slut (km)", "Slutdestination", "Tid", "Avstånd (km)", "Bränsleåtgång (liter)",
    "Titel", "Batteriförbrukning (kWh)", "Batteriregenerering (kWh)", "Anteckningar",
]
HEADERS_V2 = [
    "Kategori", "Startade", "Mätarställning start (km)", "Startposition", "Stannade",
    "Mätarställning slut (km)", "Slutdestination", "Tid", "Avstånd (km)", "Bränsleåtgång (liter)",
    "Batteriförbrukning (kWh)", "Anteckningar",
]

HOME = "EXEMPELTORP 1, Hemby"
OFFICE_A = "Kontorsgatan 4, Storstad"
OFFICE_B = "Verkstadsvägen 1, Sjöstad"
HOTEL_B = "Strandgatan 30, Sjöstad"
CUSTOMER_1 = "Lagergatan 7, Storstad"
CUSTOMER_2 = "Industrigatan 17, Sjöstad"


@dataclass
class Row:
    start: datetime
    end: datetime
    odo_start: int
    odo_end: int
    start_addr: str
    end_addr: str
    drive_seconds: int
    km: float
    fuel: float
    kwh: float
    regen: float
    note: str
    category: str = "Arbete"


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def _hm(seconds: int) -> str:
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d} h"


def month_rows(year: int, month: int, odo: int) -> list[Row]:
    """Ett realistiskt månadsurval: pendling till kontor, flerdagsresa med hotell,
    kundbesök med korta stopp samt luckor i mätarställningen (privat körning)."""
    d = datetime(year, month, 1, 0, 0)
    rows: list[Row] = []

    def add(day: int, hh: int, mm: int, minutes: int, km: float, a: str, b: str, note: str,
            drive: int | None = None, gap_before: int = 0, fuel: float | None = None) -> None:
        nonlocal odo
        odo += gap_before
        start = d.replace(day=day, hour=hh, minute=mm)
        end = start + timedelta(minutes=minutes)
        rows.append(
            Row(start, end, odo, odo + round(km), a, b, (drive if drive is not None else minutes * 60) , km,
                round(km * 0.075, 6) if fuel is None else fuel, round(km * 0.11, 6), round(km * 0.02, 6), note)
        )
        odo += round(km)

    # Dag 1: hem → kontor → hem (lång arbetsdag; körtiden kortare än start–slut på morgonen)
    add(1, 8, 37, 84, 41.794, HOME, OFFICE_A, "Driftat Storstadskontoret", drive=27 * 60)
    add(1, 16, 32, 33, 40.163, OFFICE_A, HOME, "Driftat Storstadskontoret")
    # Dag 3: kundbesök med kort stopp (9 min) och tillbaka till kontoret
    add(3, 7, 34, 33, 41.244, HOME, OFFICE_A, "Arbetat på kontoret samt kundbesök", gap_before=37)
    add(3, 14, 30, 12, 6.2, OFFICE_A, CUSTOMER_1, "Kundbesök på Exempelbolaget")
    add(3, 14, 51, 14, 6.4, CUSTOMER_1, OFFICE_A, "Kundbesök på Exempelbolaget")  # 9 min stopp
    add(3, 16, 4, 28, 39.1, OFFICE_A, HOME, "Arbetat på kontoret samt kundbesök", fuel=0)
    # Dag 8–9: flerdagsresa med hotell, 35 min stopp mellan två delresor
    add(8, 5, 29, 131, 202.0, HOME, OFFICE_B, "Resa till SJST", gap_before=112)
    add(8, 16, 46, 9, 4.3, OFFICE_B, HOTEL_B, "Från kontoret till hotellet i SJST")
    add(9, 7, 37, 8, 4.1, HOTEL_B, OFFICE_B, "Rest mellan kontor och hotell")
    add(9, 9, 10, 19, 11.5, OFFICE_B, CUSTOMER_2, "Kundbesök på Testföretaget")
    add(9, 10, 4, 21, 11.7, CUSTOMER_2, OFFICE_B, "Kundbesök på Testföretaget")  # 35 min stopp
    add(9, 13, 59, 176, 237.1, OFFICE_B, HOME, "Hemresa från SJST")
    # Dag 12 (lördag kväll): operativ insats – tjänsteresa på helg
    add(12, 20, 46, 115, 94.0, HOME, CUSTOMER_1, "Fått rycka in och köra", gap_before=58)
    add(12, 23, 37, 151, 96.5, CUSTOMER_1, HOME, "Fått rycka in och köra")  # passerar midnatt
    # Dag 20: möte
    add(20, 8, 31, 33, 39.1, HOME, OFFICE_A, "Möte på Kontoret i Storstad", gap_before=240)
    add(20, 15, 28, 24, 38.9, OFFICE_A, HOME, "Möte på Kontoret i Storstad")
    return rows


def build_v1(rows: list[Row], *, datetime_cell_rows: int = 2) -> bytes:
    """Äldre 14-kolumnersformat. De ``datetime_cell_rows`` kronologiskt sista raderna
    läggs överst som äkta datumceller med körtid som text (som i juli-underlaget)."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    wb.create_sheet("Sheet2")
    wb.create_sheet("Sheet3")
    ws.append(HEADERS_V1)
    ordered = sorted(rows, key=lambda r: r.start, reverse=True)
    special = set()
    if datetime_cell_rows:
        special = {id(r) for r in ordered[:datetime_cell_rows]}

    def emit(r: Row, as_cells: bool) -> None:
        if as_cells:
            values: list[Any] = [
                r.category, r.start, r.odo_start, r.start_addr, r.end, r.odo_end, r.end_addr,
                _hm(r.drive_seconds), int(r.km) if float(r.km).is_integer() else round(r.km, 1), round(r.fuel, 1),
                None, round(r.kwh, 1), round(r.regen, 1), r.note,
            ]
        else:
            values = [
                r.category, _fmt(r.start), r.odo_start, r.start_addr, _fmt(r.end), r.odo_end, r.end_addr,
                timedelta(seconds=r.drive_seconds), r.km, r.fuel, "", r.kwh, r.regen, r.note,
            ]
        ws.append(values)
        row_idx = ws.max_row
        ws.cell(row_idx, 2).number_format = "yyyy\\-mm\\-dd\\ hh:mm;@"
        ws.cell(row_idx, 5).number_format = "yyyy\\-mm\\-dd\\ hh:mm;@"
        ws.cell(row_idx, 8).number_format = '[hh]:mm\\ "h"'

    for r in ordered:
        if id(r) in special:
            emit(r, True)
    for r in ordered:
        if id(r) not in special:
            emit(r, False)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_v2(rows: list[Row]) -> bytes:
    """Nyare 12-kolumnersformat (blad "Trips")."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Trips"
    ws.append(HEADERS_V2)
    for r in sorted(rows, key=lambda r: r.start, reverse=True):
        km: Any = int(r.km) if float(r.km).is_integer() else round(r.km, 1)
        fuel: Any = int(r.fuel) if float(r.fuel).is_integer() else round(r.fuel, 1)
        kwh: Any = int(r.kwh) if float(r.kwh).is_integer() else round(r.kwh, 1)
        ws.append(
            [r.category, _fmt(r.start), r.odo_start, r.start_addr, _fmt(r.end), r.odo_end, r.end_addr,
             _hm(r.drive_seconds), km, fuel, kwh, r.note]
        )
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_csv(rows: list[Row], delimiter: str = ";") -> bytes:
    lines = [delimiter.join(HEADERS_V2)]
    for r in sorted(rows, key=lambda r: r.start, reverse=True):
        cells = [
            r.category, _fmt(r.start), str(r.odo_start), r.start_addr, _fmt(r.end), str(r.odo_end), r.end_addr,
            _hm(r.drive_seconds), f"{r.km:.1f}".replace(".", ","), f"{r.fuel:.1f}".replace(".", ","),
            f"{r.kwh:.1f}".replace(".", ","), r.note,
        ]
        lines.append(delimiter.join(f'"{c}"' if delimiter in c or "," in c else c for c in cells))
    return ("﻿" + "\n".join(lines) + "\n").encode("utf-8")


def sample_files() -> list[tuple[str, bytes]]:
    """Två månader: en i äldre och en i nyare format, med sammanhängande mätarställning."""
    june = month_rows(2026, 6, 180000)
    july = month_rows(2026, 7, june[-1].odo_end + 310)
    return [
        ("Underlag_Exempel_Juni_2026.xlsx", build_v1(june)),
        ("Underlag_Exempel_Juli_2026.xlsx", build_v2(july)),
    ]


def totals(rows: list[Row]) -> dict[str, float]:
    return {
        "km": round(sum(r.km for r in rows), 3),
        "fuel": round(sum(r.fuel for r in rows), 3),
        "kwh": round(sum(r.kwh for r in rows), 3),
    }
