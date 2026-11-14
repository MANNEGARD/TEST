"""Excel-exporten av körjournalen (spec 20.5): innehåll, summor, formler, kontroller och filformat."""

from __future__ import annotations

import csv
import shutil
import subprocess
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import openpyxl
import pytest
from sqlalchemy import select

import app.models as M
from app.models.enums import Category, GapKind, GapStatus, ReviewStatus, ValueQuality
from app.services import settings as settings_svc
from app.services.export import checks as checks_mod
from app.services.export import service as export_svc
from app.services.export import workbook as wbmod
from app.services.stats import common
from app.services.trips import merge
from app.services.trips import segments as seg
from tests.conftest import BASE_TIME, reload_trip

TZ = ZoneInfo("Europe/Stockholm")
SEPT = common.resolve_period("custom", tz=TZ, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30))
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
A, B, C, D = "Kundvägen 1, Storstad", "Hemlig väg 7, Smultronby", "Lagergatan 3, Storstad", "Torget 1, Storstad"

REQUIRED_JOURNAL_HEADERS = [
    "Datum", "Starttid", "Sluttid", "Resans start, adress", "Resans slut, adress", "Mätarställning start (km)",
    "Mätarställning ankomst (km)", "Reslängd (km)", "Kategori", "Ärende/syfte", "Plats/företag/kontaktperson",
    "Förare", "Anteckningar (bilförare, tankning m.m.)", "Tjänst (km)", "Privat (km)", "Delresor", "Datakälla",
    "Granskningsstatus",
]


# ----------------------------------------------------------------------------- hjälpare


def snapshot(db, vehicle, at: datetime, km: float, source: str = "manual") -> None:
    db.add(M.VehicleOdometerSnapshot(vehicle_id=vehicle.id, at=at, odometer_km=km, source=source))
    db.flush()


def complete_month(db, trips, vehicle):
    """Tre godkända resor i september med avläsningar vid periodens början och slut."""
    snapshot(db, vehicle, SEPT.start, 10000)
    t1 = trips.trip(A, C, category=Category.WORK, purpose="Kundbesök Exempelbolaget", km=12.4)
    t1.contacts = "Anna Andersson"
    t2 = trips.trip(C, B, category=Category.PRIVATE, km=7.6, gap_minutes=30)
    t3 = trips.trip(B, D, category=Category.WORK, purpose="Leverans av material", km=25.0, gap_minutes=60)
    snapshot(db, vehicle, SEPT.end, trips._odo)
    return t1, t2, t3


def export(db, user, vehicle, actor, period=SEPT, **kw) -> tuple[M.ExportJob, Path]:
    job = export_svc.create_export(
        db, user_id=user.id, vehicle=vehicle, start=period.start, end=period.end, actor=actor, tz=TZ, now=NOW, **kw
    )
    return job, Path(job.file_path)


def books(path: Path):
    return openpyxl.load_workbook(path, data_only=False), openpyxl.load_workbook(path, data_only=True)


def header_map(ws, row: int = wbmod.JOURNAL_HEADER_ROW + 1) -> dict[str, int]:
    return {c.value: c.column for c in ws[row] if c.value}


def journal(ws) -> list[dict[str, object]]:
    """Datarader på bladet Körjournal som {rubrik: värde}."""
    hm = header_map(ws)
    out = []
    for r in range(wbmod.JOURNAL_HEADER_ROW + 2, ws.max_row + 1):
        if not ws.cell(r, hm["Rese-ID"]).value:
            continue
        out.append({h: ws.cell(r, c).value for h, c in hm.items()})
    return out


def overview_value(ws, label: str):
    for row in ws.iter_rows():
        if row[0].value == label:
            return row[1].value, (row[3].value if len(row) > 3 else None)
    raise AssertionError(f"Hittar inte {label!r} på översiktsbladet")


def all_text(wb) -> str:
    return "\n".join(str(c.value) for ws in wb.worksheets for row in ws.iter_rows() for c in row if c.value is not None)


def source_rows(ws) -> list[int]:
    """Radnummer för delresorna i bladet Källa och ändringshistorik (fram till summaraden)."""
    out = []
    for r in range(5, ws.max_row + 1):
        if ws.cell(r, 8).value == "Summa":
            return out
        if ws.cell(r, 3).value:
            out.append(r)
    raise AssertionError("Summaraden saknas")


def monthly_rows(ws) -> dict[str, list]:
    out = {}
    for r in range(5, ws.max_row + 1):
        key = ws.cell(r, 1).value
        if key:
            out[key] = [ws.cell(r, c).value for c in range(1, 12)]
    return out


# ----------------------------------------------------------------------------- innehåll


def test_complete_export_has_all_required_fields(db, user, vehicle, actor, trips):
    settings_svc.update_section(db, user.id, "general", {"driver_name": "Test Förare"})
    t1, t2, t3 = complete_month(db, trips, vehicle)
    job, path = export(db, user, vehicle, actor)

    assert job.status == "done" and job.is_complete, job.warnings
    assert job.filename == "Körjournal_ABC123_2026-09.xlsx"
    assert job.summary["trips"] == 3
    assert job.summary["total_km"] == pytest.approx(45.0)
    assert job.summary["work_km"] == pytest.approx(37.4) and job.summary["private_km"] == pytest.approx(7.6)

    wf, wv = books(path)
    assert wf.sheetnames == [wbmod.SHEET_OVERVIEW, wbmod.SHEET_JOURNAL, wbmod.SHEET_MONTHLY, wbmod.SHEET_ENERGY,
                             wbmod.SHEET_QUALITY, wbmod.SHEET_SOURCES]

    ov = wv[wbmod.SHEET_OVERVIEW]
    assert overview_value(ov, "Status")[0] == wbmod.STATUS_COMPLETE
    assert overview_value(ov, "Bilens registreringsnummer")[0] == "ABC123"
    assert overview_value(ov, "År / period")[0] == "2026-09"
    assert overview_value(ov, "Förare")[0] == "Test Förare"
    start_val, start_note = overview_value(ov, "Mätarställning vid periodens början (km)")
    end_val, end_note = overview_value(ov, "Mätarställning vid periodens slut (km)")
    assert (start_val, end_val) == (10000, 10045)
    assert "Manuell avläsning" in start_note and "Manuell avläsning" in end_note
    assert overview_value(ov, "Tjänst (arbete)")[0] == pytest.approx(37.4)
    assert overview_value(ov, "Privat")[0] == pytest.approx(7.6)
    assert overview_value(ov, "Totalt")[0] == pytest.approx(45.0)

    js = wv[wbmod.SHEET_JOURNAL]
    hm = header_map(js)
    for h in REQUIRED_JOURNAL_HEADERS:
        assert h in hm, f"Rubriken {h!r} saknas"
    assert js["A2"].value == "Bilens registreringsnummer: ABC123"
    rows = journal(js)
    assert len(rows) == 3
    first = rows[0]
    assert first["Datum"].date() == date(2026, 9, 14)
    assert first["Starttid"] == datetime(2026, 9, 14, 8, 0)  # 06:00 UTC = 08:00 svensk sommartid
    assert first["Sluttid"] == datetime(2026, 9, 14, 8, 20)
    assert first["Resans start, adress"] == A and first["Resans slut, adress"] == C
    assert (first["Mätarställning start (km)"], first["Mätarställning ankomst (km)"]) == (10000, 10012)
    assert first["Reslängd (km)"] == pytest.approx(12.4)
    assert first["Kategori"] == "Arbete" and first["Tjänst (km)"] == pytest.approx(12.4)
    assert first["Ärende/syfte"] == "Kundbesök Exempelbolaget"
    assert first["Plats/företag/kontaktperson"] == "Anna Andersson"
    assert first["Förare"] == "Test Förare"
    assert first["Delresor"] == 1
    assert first["Datakälla"] == "Importerad Volvo-körjournal"
    assert first["Granskningsstatus"] == "Godkänd"
    private = rows[1]
    assert private["Kategori"] == "Privat" and private["Privat (km)"] == pytest.approx(7.6)
    assert not private["Plats/företag/kontaktperson"]  # behövs inte vid privata resor

    # Källa och ändringshistorik: en rad per delresa.
    src = wv[wbmod.SHEET_SOURCES]
    ids = [src.cell(r, 1).value for r in source_rows(src)]
    assert sorted(ids) == sorted(wbmod.short_id(t.id) for t in (t1, t2, t3))


def test_sums_and_formulas_have_correct_cached_values(db, user, vehicle, actor, trips):
    complete_month(db, trips, vehicle)
    # En resa i oktober ingår inte i septemberexporten.
    trips.trip(A, C, category=Category.WORK, purpose="Utanför perioden", km=99.0,
               start_at=datetime(2026, 10, 2, 8, 0, tzinfo=UTC))
    _, path = export(db, user, vehicle, actor)
    wf, wv = books(path)

    jf, jv = wf[wbmod.SHEET_JOURNAL], wv[wbmod.SHEET_JOURNAL]
    hm = header_map(jf)
    total_row = next(r for r in range(1, jf.max_row + 1) if jf.cell(r, hm["Resans slut, adress"]).value == "Summa (visade rader)")
    for header, expected in (("Reslängd (km)", 45.0), ("Tjänst (km)", 37.4), ("Privat (km)", 7.6), ("Ej avgjort (km)", 0.0)):
        cell_f, cell_v = jf.cell(total_row, hm[header]), jv.cell(total_row, hm[header])
        assert str(cell_f.value).startswith("=SUBTOTAL(109,"), cell_f.value
        assert cell_v.value == pytest.approx(expected)
    fuel = round(12.4 * 0.07, 2) + round(7.6 * 0.07, 2) + round(25.0 * 0.07, 2)
    assert jv.cell(total_row, hm["Bensin (l)"]).value == pytest.approx(fuel)

    mf, mv = monthly_rows(wf[wbmod.SHEET_MONTHLY]), monthly_rows(wv[wbmod.SHEET_MONTHLY])
    assert list(mf) == ["2026-09", "Summa"]  # alla månader i perioden, ingen oktober
    sept_f, sept_v = mf["2026-09"], mv["2026-09"]
    assert sept_f[1].startswith("=COUNTIFS('Körjournal'!") and sept_v[1] == 3
    assert sept_f[2].startswith("=SUMIFS('Körjournal'!") and sept_v[2] == pytest.approx(37.4)
    assert sept_v[3] == pytest.approx(7.6) and sept_v[4] == pytest.approx(0)
    assert sept_v[5] == pytest.approx(45.0)
    assert sept_v[6] == pytest.approx(3.74) and sept_v[7] == pytest.approx(0.76) and sept_v[8] == pytest.approx(4.5)
    assert sept_v[9] == pytest.approx(fuel)
    assert mf["Summa"][5].startswith("=SUM(") and mv["Summa"][5] == pytest.approx(45.0)

    of, ovw = wf[wbmod.SHEET_OVERVIEW], wv[wbmod.SHEET_OVERVIEW]
    f_total = overview_value(of, "Journalförd körsträcka (km)")[0]
    assert f_total.startswith("='Månadssammanställning'!")
    assert overview_value(ovw, "Journalförd körsträcka (km)")[0] == pytest.approx(45.0)
    assert overview_value(of, "Körsträcka enligt mätarställning (km)")[0].startswith("=")
    assert overview_value(ovw, "Körsträcka enligt mätarställning (km)")[0] == pytest.approx(45.0)
    assert overview_value(ovw, "Differens mätarställning − journal (km)")[0] == pytest.approx(0.0)
    assert overview_value(ovw, "Tjänst (arbete)")[0] == pytest.approx(37.4)

    # Cachevärdena stämmer med Python-facit.
    ctx = export_svc.build_context(db, vehicle=vehicle, period=SEPT, tz=TZ, now=NOW)
    facit = wbmod.python_totals(wbmod.journal_rows(ctx))
    assert facit["total"]["km"] == pytest.approx(mv["Summa"][5])
    assert facit["total"]["work_km"] == pytest.approx(mv["Summa"][2])


def test_merged_trips_are_not_double_counted(db, user, vehicle, actor, trips):
    snapshot(db, vehicle, SEPT.start, 10000)
    t1 = trips.trip(A, C, category=Category.WORK, purpose="Kundbesök", km=4.2, minutes=6)
    t2 = trips.trip(C, D, category=Category.WORK, purpose="Kundbesök", km=37.9, minutes=31, gap_minutes=8)
    t3 = trips.trip(D, A, category=Category.PRIVATE, km=10.1, gap_minutes=120)
    snapshot(db, vehicle, SEPT.end, trips._odo)
    raw_total = sum(r.distance_km for r in db.execute(select(M.RawTrip)).scalars())
    merged = merge.merge_trips(db, [t1, t2], trigger="manual", actor=actor)

    job, path = export(db, user, vehicle, actor)
    _, wv = books(path)
    rows = journal(wv[wbmod.SHEET_JOURNAL])
    assert len(rows) == 2
    m = next(r for r in rows if r["Rese-ID"] == wbmod.short_id(merged.id))
    assert m["Reslängd (km)"] == pytest.approx(42.1) and m["Delresor"] == 2
    assert "Sammanslagen resa, 2 delresor" in m["Anteckningar (bilförare, tankning m.m.)"]
    assert sum(r["Reslängd (km)"] for r in rows) == pytest.approx(raw_total)
    assert job.summary["total_km"] == pytest.approx(raw_total)
    assert monthly_rows(wv[wbmod.SHEET_MONTHLY])["Summa"][5] == pytest.approx(raw_total)

    # Delresorna redovisas var för sig i källbladet – summan är fortfarande densamma.
    src = wv[wbmod.SHEET_SOURCES]
    seg_rows = source_rows(src)
    assert len(seg_rows) == 3
    assert sum(src.cell(r, 9).value for r in seg_rows) == pytest.approx(raw_total)
    assert job.is_complete, job.warnings
    del t3


def test_mixed_trip_is_allocated_per_segment(db, user, vehicle, actor, trips):
    snapshot(db, vehicle, SEPT.start, 10000)
    t = trips.trip(A, D, category=Category.WORK, purpose="Kundbesök och privat ärende", km=42.0)
    snapshot(db, vehicle, SEPT.end, trips._odo)
    raw = t.segments[0].raw_trip
    seg.allocate_raw_trip(
        db, t, raw.id,
        [{"distance_km": 30.0, "category": Category.WORK}, {"distance_km": 12.0, "category": Category.PRIVATE}],
        actor="test",
    )
    t = reload_trip(db, t.id)
    assert t.has_mixed_categories

    job, path = export(db, user, vehicle, actor)
    _, wv = books(path)
    (row,) = journal(wv[wbmod.SHEET_JOURNAL])
    assert row["Kategori"] == "Blandad"
    assert row["Reslängd (km)"] == pytest.approx(42.0)
    assert row["Tjänst (km)"] == pytest.approx(30.0) and row["Privat (km)"] == pytest.approx(12.0)
    sept = monthly_rows(wv[wbmod.SHEET_MONTHLY])["2026-09"]
    assert (sept[2], sept[3], sept[5]) == (pytest.approx(30.0), pytest.approx(12.0), pytest.approx(42.0))
    ov = wv[wbmod.SHEET_OVERVIEW]
    assert overview_value(ov, "Tjänst (arbete)")[0] == pytest.approx(30.0)
    assert overview_value(ov, "Privat")[0] == pytest.approx(12.0)
    assert overview_value(ov, "Blandade resor (privat och tjänst)")[0] == "ingår ovan"
    src = wv[wbmod.SHEET_SOURCES]
    cats = sorted((src.cell(r, 9).value, src.cell(r, 10).value) for r in source_rows(src))
    assert cats == [(12.0, "Privat"), (30.0, "Arbete")]
    assert job.summary["work_km"] == pytest.approx(30.0) and job.summary["private_km"] == pytest.approx(12.0)


def test_incomplete_history_is_never_marked_complete(db, user, vehicle, actor, trips):
    # Ingen avläsning vid periodens gränser, en öppen lucka och en preliminär klassning.
    trips.trip(A, C, category=Category.WORK, purpose="Kundbesök", km=12.0)
    trips.skip(hours=3, km=50)
    trips.trip(C, D, category=Category.WORK, purpose="Möte", km=8.0, review_status=ReviewStatus.AUTO)
    db.add(M.DataGap(vehicle_id=vehicle.id, kind=GapKind.ODOMETER, dedupe_key="odo:10012:10062", odometer_start=10012,
                     odometer_end=10062, km=50.0, from_time=BASE_TIME + timedelta(minutes=20),
                     to_time=BASE_TIME + timedelta(hours=3, minutes=20), status=GapStatus.OPEN))
    db.flush()

    job, path = export(db, user, vehicle, actor)
    assert job.status == "done" and not job.is_complete
    codes = {w["code"] for w in job.warnings}
    assert {"open_gaps", "incomplete_period", "preliminary_classification"} <= codes
    assert job.summary["open_gap_km"] == pytest.approx(50.0)

    _, wv = books(path)
    ov = wv[wbmod.SHEET_OVERVIEW]
    assert overview_value(ov, "Status")[0] == wbmod.STATUS_INCOMPLETE
    assert overview_value(ov, "Mätarställning vid periodens början (km)")[0] == wbmod.MISSING
    assert overview_value(ov, "Körsträcka enligt mätarställning (km)")[0] == "Kan inte beräknas"
    assert overview_value(ov, "Ej journalförda km (öppna luckor)")[0] == pytest.approx(50.0)
    assert overview_value(ov, "varav preliminärt klassat – ej godkänt (km)")[0] == pytest.approx(8.0)
    text = all_text(wv)
    assert wbmod.STATUS_COMPLETE not in text.replace("OFULLSTÄNDIG", "")
    assert "Oavstämda kilometer (dataluckor)" in text
    js = wv[wbmod.SHEET_JOURNAL]
    assert js["A3"].value.startswith("Status: OFULLSTÄNDIG")
    statuses = {r["Granskningsstatus"] for r in journal(js)}
    assert "Automatiskt klassad – ej godkänd" in statuses


def test_formula_injection_is_neutralised(db, user, vehicle, actor, trips):
    nasty = ['=HYPERLINK("http://evil.example","klicka")', "+1+1", "-2+3", "@SUM(A1:A2)", "\tflik", "\rretur"]
    t = trips.trip("=cmd|' /C calc'!A0", C, category=Category.WORK, purpose=nasty[0], km=5.0)
    t.notes, t.contacts = nasty[1], nasty[3]
    trips.trip(C, A, category=Category.WORK, purpose=nasty[2], km=5.0, gap_minutes=30).notes = nasty[4]
    trips.trip(A, C, category=Category.WORK, purpose=nasty[5], km=5.0, gap_minutes=30)
    db.flush()
    _, path = export(db, user, vehicle, actor)
    wf, _ = books(path)
    for ws in wf.worksheets:
        for row in ws.iter_rows():
            for c in row:
                if isinstance(c.value, str) and c.data_type == "f":
                    assert "HYPERLINK" not in c.value and "cmd" not in c.value and "@SUM" not in c.value
    rows = journal(wf[wbmod.SHEET_JOURNAL])
    assert rows[0]["Ärende/syfte"] == "'" + nasty[0]
    assert rows[0]["Resans start, adress"].startswith("'=cmd")
    assert rows[0]["Plats/företag/kontaktperson"] == "'" + nasty[3]
    assert rows[0]["Anteckningar (bilförare, tankning m.m.)"].startswith("'+1+1")
    assert rows[1]["Ärende/syfte"] == "'-2+3"
    assert rows[1]["Anteckningar (bilförare, tankning m.m.)"].startswith("'\tflik")
    assert rows[2]["Ärende/syfte"].startswith("'") and "retur" in rows[2]["Ärende/syfte"]
    assert wbmod.safe_text("Vanlig text") == "Vanlig text" and wbmod.safe_text(None) == ""


def test_private_addresses_hidden_unless_enabled(db, user, vehicle, actor, trips):
    trips.trip(A, B, category=Category.PRIVATE, km=7.0)
    trips.trip(B, A, category=Category.WORK, purpose="Kundbesök", km=7.0, gap_minutes=60)
    _, path = export(db, user, vehicle, actor)
    _, wv = books(path)
    rows = journal(wv[wbmod.SHEET_JOURNAL])
    assert rows[0]["Resans start, adress"] == "Privat resa (Storstad)"
    assert rows[0]["Resans slut, adress"] == "Privat resa (Smultronby)"
    assert rows[1]["Resans start, adress"] == B  # tjänsteresa visas med adress
    private_hits = [r for r in journal(wv[wbmod.SHEET_JOURNAL]) if r["Kategori"] == "Privat" and "Hemlig" in str(r)]
    assert private_hits == []

    settings_svc.update_section(db, user.id, "export", {"include_private_addresses": True})
    _, path2 = export(db, user, vehicle, actor)
    rows2 = journal(books(path2)[1][wbmod.SHEET_JOURNAL])
    assert rows2[0]["Resans slut, adress"] == B


def test_unknown_start_time_is_left_blank(db, user, vehicle, actor, trips):
    trips.trip(A, C, category=Category.WORK, purpose="Kundbesök", km=10.0, start_known=False,
               start_earliest=BASE_TIME - timedelta(hours=2), start_latest=BASE_TIME)
    _, path = export(db, user, vehicle, actor)
    (row,) = journal(books(path)[1][wbmod.SHEET_JOURNAL])
    assert row["Starttid"] is None
    assert row["Datum"].date() == date(2026, 9, 14) and row["Sluttid"] is not None
    assert "Starttid okänd – mellan 2026-09-14 06:00 och 2026-09-14 08:00" in row["Anteckningar (bilförare, tankning m.m.)"]


def test_demo_vehicle_export_is_watermarked(db, user, vehicle, actor, trips):
    vehicle.is_demo = True
    complete_month(db, trips, vehicle)
    job, path = export(db, user, vehicle, actor)
    assert not job.is_complete and job.filename.startswith("DEMO_Körjournal_")
    _, wv = books(path)
    assert "DEMODATA" in str(wv[wbmod.SHEET_OVERVIEW]["A2"].value)
    assert "DEMODATA" in all_text(wv[wbmod.SHEET_JOURNAL].parent)
    assert any(w["code"] == "demo_data" for w in job.warnings)


def test_empty_period_export(db, user, vehicle, actor):
    job, path = export(db, user, vehicle, actor)
    assert job.status == "done" and not job.is_complete
    _, wv = books(path)
    assert journal(wv[wbmod.SHEET_JOURNAL]) == []
    assert monthly_rows(wv[wbmod.SHEET_MONTHLY])["Summa"][5] == 0
    assert overview_value(wv[wbmod.SHEET_OVERVIEW], "Totalt")[0] == 0


def test_energy_sheet_and_refuel_notes(db, user, vehicle, actor, trips):
    t = trips.trip(A, C, category=Category.WORK, purpose="Kundbesök", km=20.0)
    t2 = trips.trip(C, A, category=Category.WORK, purpose="Retur", km=20.0, gap_minutes=60)
    raw2 = t2.segments[0].raw_trip
    raw2.fuel_liters, raw2.fuel_quality = None, ValueQuality.UNKNOWN
    seg.refresh_logical(db, t2)
    db.add(M.FuelRecord(vehicle_id=vehicle.id, at=t.started_at + timedelta(minutes=10), liters=40.5, total_cost=810.0,
                        price_per_liter=20.0, station="Macken"))
    db.flush()
    _, path = export(db, user, vehicle, actor)
    _, wv = books(path)
    rows = journal(wv[wbmod.SHEET_JOURNAL])
    assert "Tankning 40,5 l, 810 kr" in rows[0]["Anteckningar (bilförare, tankning m.m.)"]
    ov = wv[wbmod.SHEET_OVERVIEW]
    assert overview_value(ov, "Täckningsgrad bensin")[0] == pytest.approx(0.5)
    assert overview_value(ov, "Bensin (l/100 km, verifierad sträcka)")[0] == pytest.approx(7.0)
    assert overview_value(ov, "Tankningar (antal)")[0] == 1
    en = wv[wbmod.SHEET_ENERGY]
    assert en["A5"].value == "2026-09" and en["C5"].value == pytest.approx(1.4) and en["E5"].value == pytest.approx(0.5)


# ----------------------------------------------------------------------------- kontroller


def test_checks_detect_problems(db, user, vehicle, actor, trips):
    t_missing = trips.trip(A, C, category=Category.WORK, km=5.0)  # tjänsteresa utan syfte
    trips.trip(C, D, category=Category.UNDECIDED, km=5.0, gap_minutes=30)
    trips.trip(D, A, category=Category.WORK, purpose="Förslag", purpose_confirmed=False, km=5.0, gap_minutes=30)
    t_neg = trips.trip(A, C, category=Category.PRIVATE, km=5.0, gap_minutes=30)
    t_neg.odometer_end = t_neg.odometer_start - 3
    raw_dup = t_neg.segments[0].raw_trip
    dup = trips.trip(A, C, category=Category.PRIVATE, km=5.0, start_at=raw_dup.started_at, odometer_start=raw_dup.odometer_start)
    dup.odometer_end = t_neg.odometer_end
    t_sum = trips.trip(C, A, category=Category.PRIVATE, km=6.0, gap_minutes=60)
    t_sum.distance_km = 9.0  # cache som inte stämmer med segmenten
    t_noodo = trips.trip(A, C, category=Category.PRIVATE, km=3.0, gap_minutes=60)
    t_noodo.odometer_start = None
    db.flush()

    result = checks_mod.run_checks(db, vehicle=vehicle, start=SEPT.start, end=SEPT.end, tz=TZ, now=NOW)
    by_code = {c["code"]: c for c in result}
    assert {"missing_purpose", "unconfirmed_purpose", "undecided_category", "negative_distance", "duplicates",
            "overlapping_trips", "sum_mismatch", "missing_odometer", "incomplete_period"} <= set(by_code)
    assert str(t_missing.id) in by_code["missing_purpose"]["trip_ids"]
    assert str(t_sum.id) in by_code["sum_mismatch"]["trip_ids"]
    assert str(t_noodo.id) in by_code["missing_odometer"]["trip_ids"]
    assert not checks_mod.is_complete(result)
    assert [c["severity"] for c in result] == sorted((c["severity"] for c in result),
                                                      key=lambda s: {"error": 0, "warning": 1, "info": 2}[s])


def test_checks_pass_for_complete_month_and_detect_unreconciled_km(db, user, vehicle, actor, trips):
    complete_month(db, trips, vehicle)
    result = checks_mod.run_checks(db, vehicle=vehicle, start=SEPT.start, end=SEPT.end, tz=TZ, now=NOW)
    assert checks_mod.is_complete(result), result
    # Avläsningen vid periodens slut visar 80 km mer än journalen.
    db.execute(select(M.VehicleOdometerSnapshot).where(M.VehicleOdometerSnapshot.at == SEPT.end)).scalar_one().odometer_km += 80
    db.flush()
    result = checks_mod.run_checks(db, vehicle=vehicle, start=SEPT.start, end=SEPT.end, tz=TZ, now=NOW)
    rec = next(c for c in result if c["code"] == "odometer_reconciliation")
    assert rec["severity"] == "error" and rec["data"]["unexplained_km"] == pytest.approx(80.0)


def test_selection_export(db, user, vehicle, actor, trips):
    t1, t2, _ = complete_month(db, trips, vehicle)
    job = export_svc.create_export(db, user_id=user.id, vehicle=vehicle, start=None, end=None, trip_ids=[t1.id, t2.id],
                                   actor=actor, tz=TZ, now=NOW)
    assert job.filename == "Körjournal_ABC123_urval.xlsx"
    assert job.summary["trips"] == 2 and job.summary["total_km"] == pytest.approx(20.0)
    assert any(w["code"] == "selection_export" for w in job.warnings)
    with pytest.raises(export_svc.ExportError):
        export_svc.create_export(db, user_id=user.id, vehicle=vehicle, start=None, end=None, trip_ids=[uuid.uuid4()],
                                 actor=actor, tz=TZ, now=NOW)


def test_delete_export_removes_file(db, user, vehicle, actor, trips):
    complete_month(db, trips, vehicle)
    job, path = export(db, user, vehicle, actor)
    assert path.is_file() and (path.stat().st_mode & 0o777) == 0o600
    export_svc.delete_export(db, job, actor=actor)
    assert not path.exists() and job.status == "deleted"
    with pytest.raises(export_svc.ExportError):
        export_svc.get_export(db, user.id, job.id)


# ----------------------------------------------------------------------------- filformat (LibreOffice)


def _soffice() -> str:
    exe = shutil.which("soffice") or shutil.which("libreoffice")
    if exe is None:
        pytest.skip("LibreOffice (soffice) saknas")
    return exe


def _run_soffice(exe: str, profile: Path, args: list[str]) -> None:
    subprocess.run(  # noqa: S603 – fast körbar fil från PATH, inga användardata
        [exe, f"-env:UserInstallation={profile.as_uri()}", "--headless", "--norestore", *args],
        check=True, capture_output=True, timeout=180,
    )


def test_file_opens_in_libreoffice_and_formulas_recalculate(db, user, vehicle, actor, trips, tmp_path):
    exe = _soffice()
    t1, t2, t3 = complete_month(db, trips, vehicle)
    trips.trip(D, A, category=Category.UNDECIDED, km=3.3, gap_minutes=600)
    _, path = export(db, user, vehicle, actor)

    profile = tmp_path / "lo-profile"
    (profile / "user").mkdir(parents=True)
    # Tvinga omräkning av alla formler vid inläsning, så att cachevärdena verkligen prövas.
    (profile / "user" / "registrymodifications.xcu").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<oor:items xmlns:oor="http://openoffice.org/2001/registry" xmlns:xs="http://www.w3.org/2001/XMLSchema" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        '<item oor:path="/org.openoffice.Office.Calc/Formula/Load"><prop oor:name="OOXMLRecalcMode" oor:op="fuse">'
        "<value>0</value></prop></item></oor:items>",
        encoding="utf-8",
    )
    pdf_dir = tmp_path / "pdf"
    _run_soffice(exe, profile, ["--convert-to", "pdf", "--outdir", str(pdf_dir), str(path)])
    pdf = pdf_dir / (path.stem + ".pdf")
    assert pdf.is_file() and pdf.read_bytes().startswith(b"%PDF")

    # Filen går fortfarande att läsa med openpyxl efteråt.
    openpyxl.load_workbook(path, data_only=False)
    openpyxl.load_workbook(path, data_only=True)

    csv_dir = tmp_path / "csv"
    _run_soffice(exe, profile, ["--convert-to", "csv:Text - txt - csv (StarCalc):44,34,76,1,,0,false,true,false,false,false,-1",
                                "--outdir", str(csv_dir), str(path)])
    monthly_csv = next(p for p in csv_dir.iterdir() if "Månadssammanställning" in p.name)
    with monthly_csv.open(encoding="utf-8") as fh:
        recalculated = {row[0]: row for row in csv.reader(fh) if row and row[0] in ("2026-09", "Summa")}
    _, wv = books(path)
    cached = monthly_rows(wv[wbmod.SHEET_MONTHLY])
    for key in ("2026-09", "Summa"):
        for col in range(1, 11):
            assert float(recalculated[key][col]) == pytest.approx(float(cached[key][col]), abs=0.001), (key, col)
    assert float(recalculated["Summa"][4]) == pytest.approx(3.3)

    overview_csv = next(p for p in csv_dir.iterdir() if "Översikt" in p.name)
    with overview_csv.open(encoding="utf-8") as fh:
        ov_rows = {row[0]: row for row in csv.reader(fh) if row}
    assert float(ov_rows["Journalförd körsträcka (km)"][1]) == pytest.approx(48.3)
    assert float(ov_rows["Totalt"][1]) == pytest.approx(48.3)
    del t1, t2, t3
