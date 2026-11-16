"""1.2.2: underlagets texter – ingen dubblerad adress och tider på en rad."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.models.enums import LocationType
from app.services.export import place_text, simple


def _place(name: str, address: str, city: str | None = None, *, kind=LocationType.CUSTOMER, confirmed=True):
    return SimpleNamespace(
        name=name, address=address, city=city, type=kind, confirmed=confirmed,
        merged_into_id=None, owner_user_id=None, is_demo=False,
    )


@pytest.mark.parametrize(
    ("name", "address", "city", "expected"),
    [
        # Platsen heter som sin adress: adressen skrivs en gång.
        ("Kundvägen 2, Storby", "Kundvägen 2, Storby", "Storby", "Kundvägen 2, Storby"),
        ("Kundvägen 2, Storby", "Kundvägen 2, 123 45 Storby", "Storby", "Kundvägen 2, 123 45 Storby"),
        ("kundvägen 2,  storby", "Kundvägen 2, Storby", None, "Kundvägen 2, Storby"),
        # Ett riktigt namn står kvar före adressen.
        ("Exempel Åkeri AB", "Kundvägen 2, Storby", "Storby", "Exempel Åkeri AB, Kundvägen 2, Storby"),
        # Ett namn som bara delvis liknar adressen är fortfarande ett namn.
        ("Kundvägen 2, Lager B", "Kundvägen 2, Storby", "Storby", "Kundvägen 2, Lager B, Kundvägen 2, Storby"),
        ("Berg", "Bergsgatan 1, Storby", "Storby", "Berg, Bergsgatan 1, Storby"),
    ],
)
def test_place_named_as_its_address_is_written_once(name, address, city, expected):
    assert place_text.report_text(_place(name, address, city), None, live=True) == expected


@pytest.mark.parametrize("text", ["06:53–08:51", "ca 12:31–12:46", "–08:52"])
def test_time_column_holds_the_longest_time_on_one_line(text):
    assert simple.wrapped_lines(text, simple.WIDTH["time"]) == 1


def test_table_still_fills_the_page_width():
    # Bredderna är avvägda mot en liggande A4 i 100 % skala; summan får inte ändras.
    assert sum(width for _, _, width in simple.COLUMNS) == pytest.approx(133)
