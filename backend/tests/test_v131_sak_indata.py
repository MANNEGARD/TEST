"""1.3.1, säkerhet – import, export och uppladdning.

* indata-4: formelskyddet i underlaget gäller också text som börjar med en radbrytning.
* indata-5: osynliga styr- och formattecken (bidi, nollbredd) i en importerad anteckning eller
  adress hamnar inte i syftet eller adressen; vanlig text, å/ä/ö och radbrytningar är kvar, och
  originalanteckningen sparas oförändrad.
* indata-6: en XLSX vars delade strängar är orimligt stora avvisas innan openpyxl läser dem.
* indata-7: "Radera originalfiler" raderar aldrig en fil utanför importkatalogen.
* indata-8: loggans filnamn på servern lämnas inte ut i GET /settings.
"""

from __future__ import annotations

import io
import zipfile
from zoneinfo import ZoneInfo

import openpyxl
import pytest
from sqlalchemy import select

import app.models as M
from app.services import text_safety
from app.services.export.workbook import safe_text
from app.services.importer import parse as P
from app.services.importer import service as imp
from app.services.trips import segments as seg
from tests.fixtures import volvo_exports as fx
from tests.test_v130_logga import png, upload

TZ = ZoneInfo("Europe/Stockholm")
API = "/api/v1"


# ----------------------------------------------------------------------------- indata-4


def test_formula_guard_covers_leading_newline():
    assert safe_text("\n=1+1") == "'\n=1+1"
    assert safe_text("\r=1+1") == "'\r=1+1"
    # Vanlig text och text med radbrytning inuti rörs inte.
    assert safe_text("Kundbesök\nMöte") == "Kundbesök\nMöte"
    assert safe_text(" -kund") == " -kund"


# ----------------------------------------------------------------------------- indata-5


def test_strip_invisible_keeps_ordinary_text():
    text = "Kundbesök hos Åkesson – möte\nRad två\tmed tabb  och dubbla blanksteg "
    assert text_safety.strip_invisible(text) == text
    assert text_safety.strip_invisible("Kund‮besök​") == "Kundbesök"
    assert text_safety.strip_invisible("Väg⁦ 1⁩\x07") == "Väg 1"
    assert text_safety.strip_invisible(None) is None
    # Sammansatta emoji (nollbreddsbindning) lämnas hela.
    assert text_safety.strip_invisible("Familj \U0001F468‍\U0001F467") == "Familj \U0001F468‍\U0001F467"


def _one_row_file(note: str, start: str, end: str) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(fx.HEADERS_V1)
    ws.append(["Arbete", "2026-06-01 08:00", 100, start, "2026-06-01 08:30", 130, end, "00:30 h", 30.2, 2.1,
               "19", 1.0, 0.2, note])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_import_strips_invisible_characters_from_purpose_and_address(db, user, vehicle, actor):
    note = "Kund‮besök​\nAndra raden"
    data = _one_row_file(note, "Testgatan​ 1, Påhittstad", "Exempel-vägen 2, Byn")
    batch = imp.create_preview(db, user_id=user.id, vehicle=vehicle, files=[("a.xlsx", data)], actor=actor)
    imp.commit(db, batch, actor=actor)
    trip = seg.active_trips(db, vehicle.id)[0]
    assert trip.purpose == "Kundbesök\nAndra raden"
    raw = db.execute(select(M.RawTrip)).scalar_one()
    assert raw.start_address == "Testgatan 1, Påhittstad"
    assert raw.end_address == "Exempel-vägen 2, Byn"
    assert raw.original_notes == note  # originalet bevaras för spårbarheten
    row = db.execute(select(M.ImportRow).where(M.ImportRow.raw_trip_id == raw.id)).scalar_one()
    assert row.parsed["original_notes"] == note
    # Samma fil igen är en ren dubblett (anteckningen jämförs oförändrad).
    again = imp.create_preview(db, user_id=user.id, vehicle=vehicle, files=[("a.xlsx", data)], actor=actor)
    assert again.summary["rows_new"] == 0 and again.summary["rows_duplicate_differs"] == 0


# ----------------------------------------------------------------------------- indata-6


def _xlsx_with_declared_shared_strings(size: int) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<x/>")
        zf.writestr("xl/worksheets/sheet1.xml", "<x/>")
        zf.writestr("xl/sharedStrings.xml", b" " * size)
    return buf.getvalue()


def test_oversized_shared_strings_are_rejected():
    data = _xlsx_with_declared_shared_strings(P.MAX_SHARED_STRINGS_BYTES + 1)
    with pytest.raises(P.ImportFileError, match="för stor"):
        P._guard_xlsx(data)


def test_renamed_shared_strings_part_is_also_capped():
    ct = ('<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Override ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"'
          ' PartName="/xl/annat.xml"/></Types>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", ct)
        zf.writestr("xl/worksheets/sheet1.xml", "<x/>")
        zf.writestr("xl/annat.xml", b" " * (P.MAX_SHARED_STRINGS_BYTES + 1))
    with pytest.raises(P.ImportFileError, match="för stor"):
        P._guard_xlsx(buf.getvalue())


def test_ordinary_exports_pass_the_shared_strings_guard():
    for _name, data in fx.sample_files():
        P._guard_xlsx(data)
    # Ett års körningar med långa anteckningar ligger långt under taket.
    rows = fx.month_rows(2026, 6, 180000)
    for r in rows:
        r.note = "Kundbesök hos Exempelbolaget AB, genomgång av avtal och leverans " * 4
    P._guard_xlsx(fx.build_v1(rows * 12))
    assert P.MAX_SHARED_STRINGS_BYTES >= 40 * 1024 * 1024


# ----------------------------------------------------------------------------- indata-7


def test_delete_original_files_never_removes_files_outside_uploads(db, user, vehicle, actor, tmp_path):
    outside = tmp_path / "utanfor" / "viktig.db"
    outside.parent.mkdir()
    outside.write_bytes(b"data")
    batch = imp.create_preview(db, user_id=user.id, vehicle=vehicle, files=fx.sample_files(), actor=actor)
    inside = [f.stored_path for f in batch.files]
    batch.files[0].stored_path = str(outside)
    db.flush()
    assert imp.delete_original_files(db, batch, actor=actor) == 2
    assert outside.exists()
    assert all(f.stored_path is None for f in batch.files)
    from pathlib import Path

    assert not Path(inside[1]).exists()  # vanliga filer raderas som förut


# ----------------------------------------------------------------------------- indata-8


@pytest.fixture()
def ha_token(client) -> str:
    resp = client.post(f"{API}/auth/tokens", json={"name": "Home Assistant", "scopes": ["ha"]})
    assert resp.status_code == 201, resp.text
    return resp.json()["token"]


def test_settings_do_not_reveal_the_logo_file_name(client, anon_client, ha_token):
    assert upload(client, png(300, 100)).status_code == 200
    for resp in (client.get(f"{API}/settings"),
                 anon_client.get(f"{API}/settings", headers={"Authorization": f"Bearer {ha_token}"})):
        if resp.status_code == 403:  # en token utan rätt till inställningarna ser ingenting alls
            continue
        assert resp.status_code == 200, resp.text
        meta = resp.json()["settings"]["export"]["logo"]
        assert "file" not in meta
        assert meta["content_type"] == "image/png" and meta["uploaded_at"]
        assert (meta["width"], meta["height"]) == (300, 100)
    patched = client.patch(f"{API}/settings/export", json={"show_logo": True})
    assert patched.status_code == 200 and "file" not in patched.json()["logo"]
    # Utan logga är uppgifterna tomma som förut.
    client.delete(f"{API}/settings/logo")
    assert client.get(f"{API}/settings").json()["settings"]["export"]["logo"] == {}
