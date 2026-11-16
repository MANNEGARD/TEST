"""1.2.3: underlaget – datumet får plats, radhöjden räcker och mellanstoppen går att stänga av."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import date

import pytest

from app.services.export import simple


def _row(**kwargs) -> simple.SimpleRow:
    row = simple.SimpleRow(
        trip_id=uuid.uuid4(), date=date(2026, 11, 3), time="16:18–19:07", from_address="Exempelgatan 4, Storby",
        to_address="Hotellgränd 2, Annanstad", via=("Rastplats 1, Mellanby", "Lagervägen 50, Annanstad"),
        purpose="Rest mellan kontor och hotell", odometer_start=1000.0, odometer_end=1241.0, km=240.9,
    )
    return replace(row, **kwargs)


def test_date_column_is_wide_enough_for_excel():
    # Excel visar ##### när ett datum inte får plats; tio tecken behöver mer än tio enheter.
    assert simple.WIDTH["date"] >= 12
    assert sum(width for _, _, width in simple.COLUMNS) == pytest.approx(133)


def test_rows_get_room_for_text_that_excel_wraps_earlier():
    # Texten ryms precis på en rad enligt uppskattningen men bryts i Excel: raden ska räknas som två.
    text = "Resa från hotell till kontoret i Annanstad"
    assert simple.wrapped_lines(text, simple.WIDTH["purpose"] * simple.WRAP_MARGIN) >= 2
    tall = simple.row_height(_row(purpose=text, via=()))
    short = simple.row_height(_row(purpose="Kundbesök", via=()))
    assert tall > short


def test_via_is_written_under_the_destination_by_default():
    row = _row()
    assert row.to_text == "Hotellgränd 2, Annanstad\nvia Rastplats 1, Mellanby; Lagervägen 50, Annanstad"


def test_rows_without_via_only_show_the_destination():
    row = replace(_row(), via=())
    assert row.to_text == "Hotellgränd 2, Annanstad"
    assert simple.row_height(row) < simple.row_height(_row())
