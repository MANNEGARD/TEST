"""Importmotorn: båda Volvo-formaten, datum- och tidsformat, dubbletter, ångra."""

from __future__ import annotations

import io
import os
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import openpyxl
import pytest
from sqlalchemy import func, select

import app.models as M
from app.models.enums import BatchStatus, Category, Coverage, ImportRowStatus, ReviewStatus, TripSource
from app.services.importer import parse as P
from app.services.importer import service as imp
from app.services.trips import segments as seg
from tests.fixtures import volvo_exports as fx

TZ = ZoneInfo("Europe/Stockholm")


def _parse(name: str, data: bytes) -> P.ParsedFile:
    return P.parse_file(name, data, TZ, max_bytes=10_000_000, max_rows=20000)


# ----------------------------------------------------------------------------- tolkning


def test_detects_older_14_column_format():
    rows = fx.month_rows(2026, 6, 180000)
    parsed = _parse("juni.xlsx", fx.build_v1(rows))
    assert parsed.format == "volvo_xlsx_v1_14col"
    assert parsed.sheet_name == "Sheet1"
    assert len(parsed.rows) == len(rows)
    assert set(parsed.column_map) >= {"title", "regen", "notes", "duration", "energy"}
    assert all(r.ok for r in parsed.rows)


def test_detects_newer_12_column_format():
    rows = fx.month_rows(2026, 8, 190000)
    parsed = _parse("augusti.xlsx", fx.build_v2(rows))
    assert parsed.format == "volvo_xlsx_v2_12col"
    assert parsed.sheet_name == "Trips"
    assert "title" not in parsed.column_map and "regen" not in parsed.column_map
    assert len(parsed.rows) == len(rows)


def test_mixed_date_cell_types_give_identical_times():
    """Datum som text och som äkta datumceller ska ge samma UTC-tid."""
    rows = fx.month_rows(2026, 7, 184000)
    as_text = {r.values["started_local"]: r.values for r in _parse("a.xlsx", fx.build_v1(rows, datetime_cell_rows=0)).rows}
    mixed = _parse("b.xlsx", fx.build_v1(rows, datetime_cell_rows=2))
    kinds = {r.values["started_kind"] for r in mixed.rows}
    assert kinds == {"string", "datetime_cell"}
    for r in mixed.rows:
        assert as_text[r.values["started_local"]]["started_at"] == r.values["started_at"]


def test_summer_and_winter_time_are_converted_to_utc():
    july, _ = P.parse_local_datetime("2026-07-01 08:34")
    january, _ = P.parse_local_datetime("2026-01-15 08:34")
    assert P.to_utc(july, TZ)[0] == datetime(2026, 7, 1, 6, 34, tzinfo=UTC)
    assert P.to_utc(january, TZ)[0] == datetime(2026, 1, 15, 7, 34, tzinfo=UTC)


def test_dst_edge_cases_are_flagged():
    missing, _ = P.parse_local_datetime("2026-03-29 02:30")  # finns inte (sommartid börjar)
    ambiguous, _ = P.parse_local_datetime("2026-10-25 02:30")  # inträffar två gånger
    assert "finns inte" in P.to_utc(missing, TZ)[1]
    assert "tvetydig" in P.to_utc(ambiguous, TZ)[1]


def test_excel_serial_dates():
    # 46204.5 = 2026-07-01 12:00 i Excels 1900-system
    dt, kind = P.parse_local_datetime(46204.5)
    assert (dt, kind) == (datetime(2026, 7, 1, 12, 0), "excel_serial")
    dt2, kind2 = P.parse_local_datetime("46204,5")
    assert dt2 == dt and kind2 == "excel_serial"
    assert P.parse_local_datetime(12.0) == (None, None)  # orimligt tal är inget datum


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        ("00:33 h", 33 * 60),
        ("01:52 h", 112 * 60),
        ("02:56", 176 * 60),
        ("1:05:30", 3930),
        (timedelta(seconds=2084), 2084),
        ("1 h 5 min", 3900),
        ("45 min", 2700),
        (0.5, 43200),  # bråkdel av dygn
        (datetime(1899, 12, 30, 0, 33), 1980),
    ],
)
def test_duration_formats(value, seconds):
    assert P.parse_duration_seconds(value)[0] == seconds


def test_duration_garbage_is_not_guessed():
    assert P.parse_duration_seconds("snart")[0] is None
    assert P.parse_duration_seconds(None)[0] is None


@pytest.mark.parametrize(("raw", "expected"), [("41,2", 41.2), ("1 234,5", 1234.5), (39, 39.0), ("", None), ("x", None)])
def test_number_parsing(raw, expected):
    assert P.parse_number(raw) == expected


def test_title_is_not_used_as_purpose_and_notes_are_preserved_exactly():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(fx.HEADERS_V1)
    ws.append(["Arbete", "2026-06-01 08:00", 100, "A 1, X", "2026-06-01 08:30", 130, "B 2, Y", "00:30 h", 30.2, 2.1,
               "19", 1.0, 0.2, "Resa  X - Y  "])
    buf = io.BytesIO()
    wb.save(buf)
    row = _parse("t.xlsx", buf.getvalue()).rows[0]
    assert row.values["original_title"] == "19"
    assert row.values["original_notes"] == "Resa  X - Y  "  # dubbla blanksteg och slutblanksteg kvar


def test_incomplete_and_invalid_rows_are_reported_per_row():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(fx.HEADERS_V2)
    ws.append(["Arbete", "2026-06-01 08:00", 100, "A", "2026-06-01 08:30", 130, "B", "00:30 h", 30, 2, 1, "ok"])
    ws.append(["Arbete", None, 130, "A", "2026-06-02 08:30", 160, "B", "00:30 h", 30, 2, 1, "saknar start"])
    ws.append(["Arbete", "2026-06-03 09:00", 200, "A", "2026-06-03 08:00", 230, "B", "00:30 h", 30, 2, 1, "slut före start"])
    ws.append(["Arbete", "2026-06-04 09:00", 300, "A", "2026-06-04 09:30", 290, "B", "00:30 h", 30, 2, 1, "odo bakåt"])
    ws.append(["Arbete", "2026-06-05 09:00", 400, "A", "2026-06-05 09:30", 430, "B", "00:30 h", -5, 2, 1, "negativ"])
    ws.append([None] * 12)  # helt tom rad ignoreras
    ws.append(["Fritid?", "2026-06-06 09:00", 500, "A", "2026-06-06 09:30", 590, "B", "??", 30, 2, 1, "avvikelse"])
    buf = io.BytesIO()
    wb.save(buf)
    rows = _parse("t.xlsx", buf.getvalue()).rows
    assert [r.ok for r in rows] == [True, False, False, False, False, True]
    assert "Starttid saknas" in rows[1].errors[0]
    assert any("före starttiden" in e for e in rows[2].errors)
    assert any("lägre än" in e for e in rows[3].errors)
    assert any("Negativt" in e for e in rows[4].errors)
    last = rows[5]
    assert last.values["category"] == Category.UNDECIDED and last.values["original_category"] == "Fritid?"
    assert any("avviker från mätarställningarna" in m for m in last.messages)
    assert any("Körtiden kunde inte tolkas" in m for m in last.messages)


def test_csv_with_semicolon_and_decimal_comma():
    rows = fx.month_rows(2026, 5, 170000)
    parsed = _parse("maj.csv", fx.build_csv(rows))
    assert parsed.format == "volvo_csv_v2_12col"
    assert len(parsed.rows) == len(rows)
    assert parsed.rows[0].values["distance_km"] == pytest.approx(sorted(rows, key=lambda r: r.start)[-1].km, abs=0.05)


def test_rejects_unsafe_or_unknown_files():
    with pytest.raises(P.ImportFileError, match="XLSX"):
        _parse("fil.xlsx", b"inte en zip")
    with pytest.raises(P.ImportFileError, match="makron"):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("[Content_Types].xml", "<x/>")
            zf.writestr("xl/workbook.xml", "<x/>")
            zf.writestr("xl/vbaProject.bin", b"0")
        _parse("fil.xlsx", buf.getvalue())
    with pytest.raises(P.ImportFileError, match="Endast XLSX"):
        _parse("fil.xlsm", b"x")
    with pytest.raises(P.ImportFileError, match="större än"):
        P.parse_file("stor.xlsx", b"x" * 2000, TZ, max_bytes=1000, max_rows=10)
    with pytest.raises(P.ImportFileError, match="kolumnrubriker"):
        _parse("annan.csv", b"a;b;c\n1;2;3\n")


def test_zip_bomb_guard(monkeypatch):
    monkeypatch.setattr(P, "MAX_UNCOMPRESSED_BYTES", 1000)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<x/>")
        zf.writestr("xl/worksheets/sheet1.xml", b"0" * 50_000)
    with pytest.raises(P.ImportFileError, match="för stor"):
        _parse("bomb.xlsx", buf.getvalue())


# ----------------------------------------------------------------------------- flödet


def test_preview_then_commit_creates_trips_with_preserved_originals(db, user, vehicle, actor):
    files = fx.sample_files()
    june, july = fx.month_rows(2026, 6, 180000), None
    batch = imp.create_preview(db, user_id=user.id, vehicle=vehicle, files=files, actor=actor)
    assert batch.status == BatchStatus.PREVIEW
    assert db.scalar(select(func.count()).select_from(M.RawTrip)) == 0  # inget skapas vid förhandsgranskning
    s = batch.summary
    assert s["rows_total"] == s["rows_new"] == 2 * len(june)
    assert s["rows_error"] == 0
    assert s["suggested_coverage"] == Coverage.SELECTION  # luckor i mätarställning → urval
    assert s["single_category_only"] is True
    assert s["odometer_gap_count"] > 0
    assert {f["format"] for f in s["files"]} == {"volvo_xlsx_v1_14col", "volvo_xlsx_v2_12col"}

    result = imp.commit(db, batch, actor=actor, apply_merge_rules=False)
    assert result["imported"] == 2 * len(june)
    raws = db.execute(select(M.RawTrip).order_by(M.RawTrip.sort_time)).scalars().all()
    assert len(raws) == 2 * len(june)
    first = raws[0]
    src = sorted(june, key=lambda r: r.start)[0]
    assert first.source == TripSource.VOLVO_IMPORT
    assert first.started_at == src.start.replace(tzinfo=TZ).astimezone(UTC)
    assert (first.odometer_start, first.odometer_end) == (src.odo_start, src.odo_end)
    assert first.distance_km == pytest.approx(src.km)
    assert first.duration_seconds == src.drive_seconds
    assert first.fuel_liters == pytest.approx(src.fuel) and first.fuel_quality == "verified"
    assert first.energy_kwh == pytest.approx(src.kwh)
    assert first.regen_kwh == pytest.approx(src.regen)  # finns i äldre formatet
    assert first.original_notes == src.note and first.original_category == "Arbete"
    assert first.start_address == src.start_addr
    assert "source_includes_stops" in first.quality_flags  # körtid 27 min, start–slut 84 min
    july_raw = raws[-1]
    assert july_raw.regen_kwh is None  # saknas i nyare formatet

    trips = seg.active_trips(db, vehicle.id)
    assert all(t.category == Category.WORK and t.review_status == ReviewStatus.IMPORTED for t in trips)
    assert trips[0].purpose == src.note and trips[0].purpose_confirmed
    assert july is None  # (variabel endast för läsbarhet)

    row = db.execute(select(M.ImportRow).where(M.ImportRow.raw_trip_id == first.id)).scalar_one()
    assert row.status == ImportRowStatus.IMPORTED
    assert row.raw["Anteckningar"] == src.note  # originalrad bevarad
    f = db.get(M.ImportedFile, row.file_id)
    assert len(f.sha256) == 64 and f.stored_path and Path(f.stored_path).exists()


def test_commit_totals_match_source(db, user, vehicle, actor):
    june = fx.month_rows(2026, 6, 180000)
    batch = imp.create_preview(
        db, user_id=user.id, vehicle=vehicle,
        files=[("juni.xlsx", fx.build_v1(june, datetime_cell_rows=0))], actor=actor,
    )
    imp.commit(db, batch, actor=actor)
    trips = seg.active_trips(db, vehicle.id)
    expected = fx.totals(june)
    assert sum(t.distance_km for t in trips) == pytest.approx(expected["km"], abs=0.01)
    assert sum(t.fuel_liters or 0 for t in trips) == pytest.approx(expected["fuel"], abs=0.01)
    assert sum(t.energy_kwh or 0 for t in trips) == pytest.approx(expected["kwh"], abs=0.01)


def test_repeated_import_is_idempotent(db, user, vehicle, actor):
    files = fx.sample_files()
    first = imp.create_preview(db, user_id=user.id, vehicle=vehicle, files=files, actor=actor)
    imp.commit(db, first, actor=actor)
    count = db.scalar(select(func.count()).select_from(M.RawTrip))
    again = imp.commit(db, first, actor=actor)  # dubbelklick på bekräfta
    assert again["already_committed"] is True

    second = imp.create_preview(db, user_id=user.id, vehicle=vehicle, files=files, actor=actor)
    assert second.summary["rows_new"] == 0
    assert second.summary["rows_duplicate"] == second.summary["rows_total"]
    assert all(f["status"] == "duplicate_file" for f in second.summary["files"])
    assert imp.commit(db, second, actor=actor)["imported"] == 0
    assert db.scalar(select(func.count()).select_from(M.RawTrip)) == count


def test_same_trips_in_other_format_are_duplicates(db, user, vehicle, actor):
    """Samma resor exporterade i det andra formatet (annan filhash) ska kännas igen per rad."""
    rows = fx.month_rows(2026, 6, 180000)
    b1 = imp.create_preview(db, user_id=user.id, vehicle=vehicle, files=[("a.xlsx", fx.build_v1(rows))], actor=actor)
    imp.commit(db, b1, actor=actor)
    b2 = imp.create_preview(db, user_id=user.id, vehicle=vehicle, files=[("b.xlsx", fx.build_v2(rows))], actor=actor)
    assert b2.summary["files"][0]["status"] == "parsed"
    assert b2.summary["rows_new"] == 0
    assert b2.summary["rows_duplicate"] + b2.summary["rows_duplicate_differs"] == len(rows)


def test_duplicate_with_changed_note_is_reported_not_overwritten(db, user, vehicle, actor):
    rows = fx.month_rows(2026, 6, 180000)
    b1 = imp.create_preview(db, user_id=user.id, vehicle=vehicle, files=[("a.xlsx", fx.build_v2(rows))], actor=actor)
    imp.commit(db, b1, actor=actor)
    original = sorted(rows, key=lambda r: r.start)[0].note
    sorted(rows, key=lambda r: r.start)[0].note = "Helt annan anteckning"
    b2 = imp.create_preview(db, user_id=user.id, vehicle=vehicle, files=[("b.xlsx", fx.build_v2(rows))], actor=actor)
    assert b2.summary["rows_duplicate_differs"] == 1
    imp.commit(db, b2, actor=actor)
    first = db.execute(select(M.RawTrip).order_by(M.RawTrip.sort_time)).scalars().first()
    assert first.original_notes == original


def test_duplicates_within_one_upload(db, user, vehicle, actor):
    rows = fx.month_rows(2026, 6, 180000)
    data = fx.build_v2(rows + rows[:3])
    batch = imp.create_preview(db, user_id=user.id, vehicle=vehicle, files=[("a.xlsx", data)], actor=actor)
    assert batch.summary["rows_new"] == len(rows)
    assert batch.summary["rows_duplicate"] == 3


def test_bad_file_does_not_block_good_file(db, user, vehicle, actor):
    rows = fx.month_rows(2026, 6, 180000)
    batch = imp.create_preview(
        db, user_id=user.id, vehicle=vehicle,
        files=[("trasig.xlsx", b"trasigt innehall"), ("bra.xlsx", fx.build_v2(rows))], actor=actor,
    )
    statuses = {f["filename"]: f["status"] for f in batch.summary["files"]}
    assert statuses == {"trasig.xlsx": "error", "bra.xlsx": "parsed"}
    assert imp.commit(db, batch, actor=actor)["imported"] == len(rows)


def test_undo_import_voids_trips_and_allows_reimport(db, user, vehicle, actor):
    files = fx.sample_files()
    batch = imp.create_preview(db, user_id=user.id, vehicle=vehicle, files=files, actor=actor)
    imp.commit(db, batch, actor=actor)  # med sammanslagningsregler
    assert any(t.segment_count > 1 for t in seg.active_trips(db, vehicle.id))
    result = imp.undo(db, batch, actor=actor)
    assert result["voided"] == batch.summary["rows_new"]
    assert seg.active_trips(db, vehicle.id) == []
    assert db.scalar(select(func.count()).select_from(M.RawTrip).where(M.RawTrip.voided_at.is_(None))) == 0
    assert db.scalar(select(func.count()).select_from(M.ImportRow)) == batch.summary["rows_total"]  # loggen finns kvar
    assert imp.undo(db, batch, actor=actor)["already_undone"] is True
    again = imp.create_preview(db, user_id=user.id, vehicle=vehicle, files=files, actor=actor)
    assert again.summary["rows_new"] == batch.summary["rows_total"]


def test_import_never_marks_selection_as_complete_journal(db, user, vehicle, actor):
    batch = imp.create_preview(db, user_id=user.id, vehicle=vehicle, files=fx.sample_files(), actor=actor)
    result = imp.commit(db, batch, actor=actor)
    assert batch.coverage == Coverage.SELECTION
    assert result["open_gaps"] > 0 and result["open_gap_km"] > 0
    assert any("urval" in n for n in batch.summary["notes"])


def test_delete_original_files(db, user, vehicle, actor):
    batch = imp.create_preview(db, user_id=user.id, vehicle=vehicle, files=fx.sample_files(), actor=actor)
    paths = [f.stored_path for f in batch.files]
    assert all(Path(p).exists() for p in paths)
    assert imp.delete_original_files(db, batch, actor=actor) == 2
    assert not any(Path(p).exists() for p in paths)


# ----------------------------------------------------------- verkliga underlag (valfritt)

REAL_DIR = os.environ.get("DRIVELOG_REAL_EXPORT_DIR")


@pytest.mark.skipif(not REAL_DIR, reason="Sätt DRIVELOG_REAL_EXPORT_DIR för att verifiera mot egna originalfiler")
def test_real_exports_import_cleanly(db, user, vehicle, actor):
    """Kör importen mot verkliga originalfiler utan att skriva ut något innehåll."""
    files = [(p.name, p.read_bytes()) for p in sorted(Path(REAL_DIR).glob("*.xlsx"))]
    assert files, "Inga .xlsx-filer i katalogen"
    batch = imp.create_preview(db, user_id=user.id, vehicle=vehicle, files=files, actor=actor)
    s = batch.summary
    assert s["rows_error"] == 0
    assert all(f["status"] == "parsed" for f in s["files"])
    assert all(f["format"] in ("volvo_xlsx_v1_14col", "volvo_xlsx_v2_12col") for f in s["files"])
    result = imp.commit(db, batch, actor=actor)
    assert result["imported"] == s["rows_new"] == s["rows_total"]
    trips = seg.active_trips(db, vehicle.id)
    assert sum(t.distance_km for t in trips) == pytest.approx(s["distance_km"], abs=0.2)  # ingen dubbelräkning
    again = imp.create_preview(db, user_id=user.id, vehicle=vehicle, files=files, actor=actor)
    assert again.summary["rows_new"] == 0
