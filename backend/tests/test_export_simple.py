"""Den enkla exporten (layout ``simple``): ett blad med tjänsteresorna, underlag för milersättning.

Prövar urvalet (endast godkända/importerade tjänsteresor), kolumnerna, tidskolumnen,
summor och formler mot handräkning, sidhuvudet, skydd mot formula injection,
förhandsvisningen (samma rader och summor som filen), layoutvalet, LibreOffice och
behörighet.
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import openpyxl
import pytest
from sqlalchemy import select

import app.models as M
from app.models.enums import Category, ReviewStatus
from app.security import passwords
from app.services import audit
from app.services import settings as settings_svc
from app.services.export import service as export_svc
from app.services.export import simple
from app.services.stats import common
from app.services.trips import gaps, merge
from app.services.trips import segments as seg
from tests.conftest import TEST_PASSWORD, reload_trip
from tests.test_export import _run_soffice, _soffice

TZ = ZoneInfo("Europe/Stockholm")
SEPT = common.resolve_period("custom", tz=TZ, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30))
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
A, B, C, D = "Kundvägen 1, Storstad", "Hemlig väg 7, Smultronby", "Lagergatan 3, Storstad", "Torget 1, Storstad"
HEADERS = ["Datum", "Tid", "Från", "Till", "Ärende", "Mätarställning start", "Mätarställning slut", "Km"]
PERIOD = {"from": "2026-09-01", "to": "2026-09-30"}


# ----------------------------------------------------------------------------- hjälpare


def export(db, user, vehicle, actor, start=SEPT.start, end=SEPT.end, **kw) -> tuple[M.ExportJob, Path]:
    job = export_svc.create_export(db, user_id=user.id, vehicle=vehicle, start=start, end=end, actor=actor, tz=TZ,
                                   now=NOW, **kw)
    return job, Path(job.file_path)


def books(path: Path):
    return openpyxl.load_workbook(path, data_only=False), openpyxl.load_workbook(path, data_only=True)


def header_row(ws) -> int:
    """Rubrikens första rad. Rubriken har två rader: "Mätarställning" står över "start" och "slut"."""
    return next(r for r in range(1, ws.max_row + 1) if ws.cell(r, 1).value == "Datum")


def first_data_row(ws) -> int:
    return header_row(ws) + simple.HEADER_ROWS


def header_titles(ws) -> list[str]:
    """Kolumnrubrikerna så som de läses: den gemensamma rubriken följd av underrubriken."""
    hr = header_row(ws)
    titles = []
    group = None
    for c in range(1, len(HEADERS) + 1):
        top, sub = ws.cell(hr, c).value, ws.cell(hr + 1, c).value
        if top is not None and sub is not None:
            group = top
        titles.append(f"{group} {sub}" if sub is not None else top)
    return titles


def table(ws) -> list[dict[str, object]]:
    """Tabellraderna som {rubrik: värde}, fram till summaraden."""
    hr = header_row(ws)
    out = []
    for r in range(hr + simple.HEADER_ROWS, ws.max_row + 1):
        if ws.cell(r, 1).value == "Summa":
            return out
        out.append({h: ws.cell(r, c).value for c, h in enumerate(HEADERS, start=1)})
    raise AssertionError("Summaraden saknas")


def summary_rows(ws) -> dict[str, int]:
    """Radnummer för summaraden samt mil- och ersättningsraderna (efter etikett)."""
    total = next(r for r in range(1, ws.max_row + 1) if ws.cell(r, 1).value == "Summa")
    out = {"total": total}
    for r in range(total + 1, total + 4):
        label = str(ws.cell(r, 4).value or "")
        if label.startswith("Mil"):
            out["mil"] = r
        elif label.startswith("Ersättning"):
            out["amount"] = r
    return out


def header_info(ws) -> dict[str, str]:
    hr = header_row(ws)
    left, right = 1, simple.INFO_RIGHT_COL + 1  # sidhuvudet har två spalter med etikett + värde
    return {ws.cell(r, c).value: ws.cell(r, c + 1).value for r in range(2, hr) for c in (left, right)
            if ws.cell(r, c).value}


def standard_month(db, trips, vehicle):
    """Två tjänsteresor (godkänd och importerad) samt resor som inte ska tas med."""
    t1 = trips.trip(A, C, category=Category.WORK, purpose="Kundbesök Exempelbolaget", km=12.4)
    t1.contacts = "Anna Andersson"
    private = trips.trip(C, B, category=Category.PRIVATE, km=7.6, gap_minutes=30)
    t2 = trips.trip(B, D, category=Category.WORK, purpose="Leverans av material", km=25.0, gap_minutes=60,
                    review_status=ReviewStatus.IMPORTED)
    undecided = trips.trip(D, A, category=Category.UNDECIDED, km=3.3, gap_minutes=60)
    preliminary = trips.trip(A, C, category=Category.WORK, purpose="Möte", km=8.0, gap_minutes=60,
                             review_status=ReviewStatus.AUTO)
    db.flush()
    return t1, t2, private, undecided, preliminary


# ----------------------------------------------------------------------------- innehåll


def test_one_sheet_with_exactly_the_eight_columns_and_only_reported_business_trips(db, user, vehicle, actor, trips):
    t1, t2, private, undecided, preliminary = standard_month(db, trips, vehicle)
    job, path = export(db, user, vehicle, actor)

    assert job.status == "done" and job.params["layout"] == "simple"
    wf, wv = books(path)
    assert wf.sheetnames == ["Körjournal"]
    ws = wv["Körjournal"]
    hr = header_row(ws)
    assert header_titles(ws) == HEADERS
    assert ws.max_column == len(HEADERS)
    assert hr <= 5  # kompakt sidhuvud: titel + högst två rader i två spalter

    rows = table(ws)
    assert len(rows) == 2
    first, second = rows
    assert first["Datum"] == datetime(2026, 9, 14)
    assert wf["Körjournal"].cell(first_data_row(ws), 1).number_format == "yyyy-mm-dd"
    assert first["Tid"] == "08:00–08:20"
    assert (first["Från"], first["Till"]) == (A, C)
    assert first["Ärende"] == "Kundbesök Exempelbolaget – Anna Andersson"
    assert (first["Mätarställning start"], first["Mätarställning slut"]) == (10000, 10012)
    assert first["Km"] == pytest.approx(12.4)
    assert second["Ärende"] == "Leverans av material" and second["Km"] == pytest.approx(25.0)

    # Utelämnade resor syns inte i filen, och sammanfattningen säger varför.
    text = "\n".join(str(c.value) for row in ws.iter_rows() for c in row if c.value is not None)
    assert "Möte" not in text  # preliminär tjänsteresa
    assert [r["Km"] for r in rows] == [12.4, 25.0]  # varken privat (7,6) eller oavgjord (3,3)
    assert job.summary["trips"] == 2 and job.summary["km"] == pytest.approx(37.4)
    excluded = job.summary["excluded"]
    assert excluded["pending_approval"] == {"count": 1, "km": 8.0}
    assert excluded["undecided"] == {"count": 1, "km": 3.3}
    assert excluded["total"] == 2
    # Inga datakvalitets-, käll- eller historikuppgifter.
    for word in ("Datakälla", "Granskningsstatus", "Rese-ID", "Kategori", "Delresor"):
        assert word not in text
    del t1, t2, private, undecided, preliminary


def test_contacts_already_in_purpose_are_not_repeated(db, user, vehicle, actor, trips):
    t = trips.trip(A, C, category=Category.WORK, purpose="Möte med Anna Andersson", km=5.0)
    t.contacts = "anna andersson"
    db.flush()
    _, path = export(db, user, vehicle, actor)
    (row,) = table(books(path)[1]["Körjournal"])
    assert row["Ärende"] == "Möte med Anna Andersson"


def test_rows_are_sorted_by_start_time(db, user, vehicle, actor, trips):
    late = trips.trip(A, C, category=Category.WORK, purpose="Sen", km=5.0, start_at=datetime(2026, 9, 20, 8, 0, tzinfo=UTC))
    early = trips.trip(C, A, category=Category.WORK, purpose="Tidig", km=5.0,
                       start_at=datetime(2026, 9, 2, 8, 0, tzinfo=UTC), odometer_start=9000)
    _, path = export(db, user, vehicle, actor)
    assert [r["Ärende"] for r in table(books(path)[1]["Körjournal"])] == ["Tidig", "Sen"]
    del late, early


def test_mixed_trip_reports_only_business_km(db, user, vehicle, actor, trips):
    t = trips.trip(A, D, category=Category.WORK, purpose="Kundbesök och privat ärende", km=42.0)
    raw = t.segments[0].raw_trip
    seg.allocate_raw_trip(
        db, t, raw.id,
        [{"distance_km": 30.0, "category": Category.WORK}, {"distance_km": 12.0, "category": Category.PRIVATE}],
        actor="test",
    )
    reload_trip(db, t.id)
    job, path = export(db, user, vehicle, actor)
    ws = books(path)[1]["Körjournal"]
    (row,) = table(ws)
    assert row["Km"] == pytest.approx(30.0)
    assert row["Ärende"] == "Kundbesök och privat ärende"
    assert job.summary["km"] == pytest.approx(30.0)
    # Fotnoten (en rad) förklarar varför mätarställningen visar mer än Km.
    note = ws.cell(summary_rows(ws)["amount"] + 1, 1).value
    assert "endast tjänstedelen" in note


def test_merged_trip_is_one_row_with_via_and_never_double_counted(db, user, vehicle, actor, trips):
    t1 = trips.trip(A, C, category=Category.WORK, purpose="Kundbesök", km=4.2, minutes=6)
    t2 = trips.trip(C, D, category=Category.WORK, purpose="Kundbesök", km=37.9, minutes=31, gap_minutes=8)
    t3 = trips.trip(D, B, category=Category.WORK, purpose="Leverans", km=10.1, gap_minutes=120)
    raw_total = sum(r.distance_km for r in db.execute(select(M.RawTrip)).scalars())
    merged = merge.merge_trips(db, [t1, t2], trigger="manual", actor=actor)

    job, path = export(db, user, vehicle, actor)
    rows = table(books(path)[1]["Körjournal"])
    assert len(rows) == 2
    m = rows[0]
    assert m["Till"] == f"{D}\nvia {C}"
    assert m["Km"] == pytest.approx(42.1)
    assert sum(r["Km"] for r in rows) == pytest.approx(raw_total)
    assert job.summary["km"] == pytest.approx(raw_total)
    del merged, t3


def test_odometer_gaps_change_nothing(db, user, vehicle, actor, trips):
    trips.trip(A, C, category=Category.WORK, purpose="Kundbesök", km=12.0)
    _, path_before = export(db, user, vehicle, actor)
    before = table(books(path_before)[1]["Körjournal"])

    trips.skip(hours=3, km=250)  # privat körning mellan tjänsteresorna
    trips.trip(C, D, category=Category.WORK, purpose="Möte", km=8.0)
    gaps.recompute(db, vehicle.id)
    job, path = export(db, user, vehicle, actor)
    rows = table(books(path)[1]["Körjournal"])
    assert rows[0] == before[0]
    assert (rows[1]["Mätarställning start"], rows[1]["Mätarställning slut"]) == (10262, 10270)
    assert job.is_complete and job.warnings == []
    assert job.summary["km"] == pytest.approx(20.0)


def test_missing_odometer_leaves_cell_empty(db, user, vehicle, actor, trips):
    t = trips.trip(A, C, category=Category.WORK, purpose="Kundbesök", km=12.0)
    t.odometer_start = None
    db.flush()
    _, path = export(db, user, vehicle, actor)
    (row,) = table(books(path)[1]["Körjournal"])
    assert row["Mätarställning start"] is None and row["Mätarställning slut"] == 10012


def test_time_column(db, user, vehicle, actor, trips):
    trips.trip(A, C, category=Category.WORK, purpose="Vanlig", km=5.0, start_at=datetime(2026, 9, 14, 6, 31, tzinfo=UTC),
               minutes=33)
    trips.trip(C, A, category=Category.WORK, purpose="Över midnatt", km=96.5,
               start_at=datetime(2026, 9, 15, 21, 37, tzinfo=UTC), minutes=151)
    trips.trip(A, D, category=Category.WORK, purpose="Okänd start", km=5.0,
               start_at=datetime(2026, 9, 17, 6, 0, tzinfo=UTC), start_known=False)
    _, path = export(db, user, vehicle, actor)
    rows = {r["Ärende"]: r for r in table(books(path)[1]["Körjournal"])}
    assert rows["Vanlig"]["Tid"] == "08:31–09:04"
    assert rows["Över midnatt"]["Tid"] == "23:37–02:08 (+1)"
    assert rows["Över midnatt"]["Datum"] == datetime(2026, 9, 15)
    assert rows["Okänd start"]["Tid"] == "–08:20"  # starttiden hittas aldrig på
    assert rows["Okänd start"]["Datum"] == datetime(2026, 9, 17)


def test_time_text_without_any_known_time():
    class Trip:
        started_at = ended_at = None
        start_time_quality = end_time_quality = "unknown"

    assert simple.time_text(Trip(), TZ) == ""  # type: ignore[arg-type]
    Trip.started_at = datetime(2026, 9, 1, 6, 0, tzinfo=UTC)
    Trip.start_time_quality = "exact"
    assert simple.time_text(Trip(), TZ) == "08:00–"  # type: ignore[arg-type]


# ----------------------------------------------------------------------------- summor och formler


def test_sums_and_amount_match_hand_computation_with_formulas_and_cached_values(db, user, vehicle, actor, trips):
    settings_svc.update_section(db, user.id, "export", {"rate_per_mil": 18.5})
    for i, km in enumerate((12.4, 25.0, 7.35, 101.25)):
        trips.trip(A, C, category=Category.WORK, purpose=f"Resa {i}", km=km, gap_minutes=60)
    # Handräkning: raderna avrundas till en decimal (7,35 → 7,4; 101,25 → 101,3).
    km = 12.4 + 25.0 + 7.4 + 101.3  # 146,1
    mil = 14.61
    amount = 270.29  # 14,61 × 18,5 = 270,285 → 270,29

    job, path = export(db, user, vehicle, actor)
    wf, wv = books(path)
    fs, vs = wf["Körjournal"], wv["Körjournal"]
    assert [r["Km"] for r in table(vs)] == [12.4, 25.0, 7.4, 101.3]
    pos = summary_rows(fs)
    t, m, a = pos["total"], pos["mil"], pos["amount"]

    assert fs.cell(t, 2).value.startswith("=COUNT(") and vs.cell(t, 2).value == 4
    assert fs.cell(t, 8).value.startswith("=SUM(") and vs.cell(t, 8).value == pytest.approx(km)
    assert fs.cell(m, 7).value.startswith("=ROUND(") and vs.cell(m, 7).value == pytest.approx(mil)
    assert vs.cell(a, 4).value == "Ersättning à 18,50 kr/mil"
    assert fs.cell(a, 7).value == f"=ROUND(G{m}*18.5,2)" and vs.cell(a, 7).value == pytest.approx(amount)
    assert (job.summary["km"], job.summary["mil"], job.summary["amount"]) == (pytest.approx(km), mil, amount)
    assert job.summary["rate_per_mil"] == 18.5

    # Summeringen står direkt under tabellen.
    assert t == first_data_row(fs) + 4
    # Samma tal som dashboardens milersättning.
    trips_ = common.load_trips(db, vehicle.id, SEPT.start, SEPT.end)
    r = common.reimbursement(trips_, rate_per_mil=18.5)
    assert (r["km"], r["mil"], r["amount"]) == (pytest.approx(km), mil, amount)


def test_show_amount_false_removes_the_amount(db, user, vehicle, actor, trips):
    trips.trip(A, C, category=Category.WORK, purpose="Kundbesök", km=12.4)
    settings_svc.update_section(db, user.id, "export", {"show_amount": False})
    job, path = export(db, user, vehicle, actor)
    ws = books(path)[1]["Körjournal"]
    pos = summary_rows(ws)
    assert "amount" not in pos and "mil" in pos
    text = "\n".join(str(c.value) for row in ws.iter_rows() for c in row if c.value is not None)
    assert "Ersättning" not in text and " kr" not in text
    assert job.summary["amount"] is None and job.summary["show_amount"] is False
    assert job.summary["mil"] == pytest.approx(1.24)


def test_rate_zero_hides_the_amount(db, user, vehicle, actor, trips):
    trips.trip(A, C, category=Category.WORK, purpose="Kundbesök", km=12.4)
    settings_svc.update_section(db, user.id, "export", {"rate_per_mil": 0})
    job, path = export(db, user, vehicle, actor)
    assert "amount" not in summary_rows(books(path)[1]["Körjournal"]) and job.summary["amount"] is None


def test_empty_period_keeps_valid_formulas(db, user, vehicle, actor):
    job, path = export(db, user, vehicle, actor)
    wf, wv = books(path)
    ws = wv["Körjournal"]
    assert ws.cell(first_data_row(ws), 1).value == simple.EMPTY_TEXT
    pos = summary_rows(ws)
    assert ws.cell(pos["total"], 2).value == 0 and ws.cell(pos["total"], 8).value == 0
    assert ws.cell(pos["amount"], 7).value == 0
    assert wf["Körjournal"].cell(pos["total"], 8).value.startswith("=SUM(")
    assert job.summary["trips"] == 0 and job.is_complete


# ----------------------------------------------------------------------------- sidhuvud och filnamn


def test_header_for_a_whole_month(db, user, vehicle, actor, trips):
    settings_svc.update_section(db, user.id, "general", {"driver_name": "Test Förare", "employer_name": "Exempel AB"})
    trips.trip(A, C, category=Category.WORK, purpose="Kundbesök", km=12.4)
    job, path = export(db, user, vehicle, actor)
    ws = books(path)[1]["Körjournal"]
    assert ws["A1"].value == "Körjournal – tjänsteresor"
    assert header_info(ws) == {"Period": "September 2026", "Förare": "Test Förare", "Arbetsgivare": "Exempel AB",
                               "Bil": "Volvo V90, ABC123"}
    assert job.filename == "Underlag milersättning - Test Förare - ABC123 - september 2026.xlsx"
    assert job.summary["period_label"] == "September 2026"


def test_employer_row_is_omitted_and_driver_falls_back_to_display_name(db, user, vehicle, actor, trips):
    trips.trip(A, C, category=Category.WORK, purpose="Kundbesök", km=12.4)
    start = common.local_midnight(date(2026, 6, 1), TZ)
    end = common.local_midnight(date(2026, 9, 1), TZ) + timedelta(days=30)  # 2026-06-01 – 2026-09-30
    job, path = export(db, user, vehicle, actor, start=start, end=end)
    info = header_info(books(path)[1]["Körjournal"])
    assert "Arbetsgivare" not in info
    assert info["Förare"] == user.display_name
    assert info["Period"] == "2026-06-01 – 2026-09-30"
    assert job.filename == "Underlag milersättning - Test Förare - ABC123 - 2026-06-01_2026-09-30.xlsx"


def test_period_label_and_filename_helpers():
    june = (common.local_midnight(date(2026, 6, 1), TZ), common.local_midnight(date(2026, 7, 1), TZ))
    assert simple.period_label(*june, TZ, []) == "Juni 2026"
    assert simple.filename_part(*june, TZ) == "2026-06"
    summer = (june[0], common.local_midnight(date(2026, 9, 1), TZ))
    assert simple.period_label(*summer, TZ, []) == "2026-06-01 – 2026-08-31"
    assert simple.filename_part(*summer, TZ) == "2026-06-01_2026-08-31"
    year = (common.local_midnight(date(2026, 1, 1), TZ), common.local_midnight(date(2027, 1, 1), TZ))
    assert simple.filename_part(*year, TZ) == "2026-01-01_2026-12-31"


def test_demo_vehicle_is_watermarked(db, user, vehicle, actor, trips):
    vehicle.is_demo = True
    trips.trip(A, C, category=Category.WORK, purpose="Kundbesök", km=12.4)
    job, path = export(db, user, vehicle, actor)
    assert books(path)[1]["Körjournal"]["A1"].value == "Körjournal – tjänsteresor – DEMODATA"
    assert job.filename.startswith("DEMO Underlag milersättning - ") and not job.is_complete


def test_formula_injection_is_neutralised(db, user, vehicle, actor, trips):
    settings_svc.update_section(db, user.id, "general", {"driver_name": "=1+1", "employer_name": "@SUM(A1)"})
    t = trips.trip("=cmd|' /C calc'!A0", "+46 Storstad", category=Category.WORK,
                   purpose='=HYPERLINK("http://evil.example","klicka")', km=5.0)
    t.contacts = "@SUM(A1:A2)"
    trips.trip(C, A, category=Category.WORK, purpose="-2+3", km=5.0, gap_minutes=30)
    db.flush()
    _, path = export(db, user, vehicle, actor)
    wf, _ = books(path)
    ws = wf["Körjournal"]
    for row in ws.iter_rows():
        for c in row:
            if c.data_type == "f":
                assert "HYPERLINK" not in str(c.value) and "cmd" not in str(c.value) and "@" not in str(c.value)
    rows = table(ws)
    assert rows[0]["Från"].startswith("'=cmd") and rows[0]["Till"] == "'+46 Storstad"
    assert rows[0]["Ärende"].startswith("'=HYPERLINK") and rows[0]["Ärende"].endswith("– @SUM(A1:A2)")
    assert rows[1]["Ärende"] == "'-2+3"
    info = header_info(ws)
    assert info["Förare"] == "'=1+1" and info["Arbetsgivare"] == "'@SUM(A1)"


# ----------------------------------------------------------------------------- utskrift


def test_print_setup(db, user, vehicle, actor, trips):
    for i in range(60):
        trips.trip(A, C, category=Category.WORK, purpose=f"Resa {i}", km=5.0, gap_minutes=60)
    job, path = export(db, user, vehicle, actor)
    wf, _ = books(path)
    ws = wf["Körjournal"]
    hr = header_row(ws)
    assert ws.page_setup.orientation == "landscape" and ws.page_setup.paperSize == 9
    assert ws.print_title_rows == f"${hr}:${hr + 1}"
    assert ws.freeze_panes == f"A{hr + 2}"
    assert ws.auto_filter.ref is None  # inga filterknappar
    assert "Sida &P av &N" in ws.oddFooter.right.text
    breaks = [b.id for b in ws.row_breaks.brk]
    assert breaks, "60 resor ska ge flera sidor"
    # Sista sidan innehåller minst två resor före summeringen.
    total = summary_rows(ws)["total"]
    assert total - breaks[-1] >= 2


def test_page_breaks_keep_the_summary_with_the_last_rows():
    page = (simple.PAGE_H_IN - simple.MARGIN_TOP_IN - simple.MARGIN_BOTTOM_IN) * 72 * simple.PAGE_FILL
    top = 120.0
    row = 18.0
    fits = int((page - top) // row)  # rader som ryms på första sidan
    # Exakt fyllda sidan: summeringen ryms inte → de två sista raderna följer med till nästa sida.
    breaks = simple.page_breaks(top, [row] * fits, 80.0, 10)
    assert breaks == [10 + fits - 2]
    # Gott om plats: inga brytningar.
    assert simple.page_breaks(top, [row] * 5, 80.0, 10) == []


# ----------------------------------------------------------------------------- förhandsvisning och layout


def test_preview_has_the_same_rows_and_totals_as_the_file(client, db, user, vehicle, trips, actor):
    standard_month(db, trips, vehicle)
    trips.trip(A, C, category=Category.WORK, purpose="Över midnatt", km=96.5,
               start_at=datetime(2026, 9, 25, 21, 37, tzinfo=UTC), minutes=151)
    db.commit()
    r = client.get("/api/v1/exports/preview", params=PERIOD)
    assert r.status_code == 200, r.text
    preview = r.json()
    assert preview["period_label"] == "September 2026"
    assert preview["filename"] == "Underlag milersättning - Test Förare - ABC123 - september 2026.xlsx"
    assert set(preview["rows"][0]) == {"trip_id", "date", "time", "from", "to", "purpose", "odometer_start",
                                       "odometer_end", "km", "address_missing"}
    assert set(preview["totals"]) >= {"trips", "km", "mil", "rate_per_mil", "show_amount", "amount"}
    assert preview["excluded"]["pending_approval"]["count"] == 1
    assert preview["excluded"]["undecided"]["count"] == 1

    created = client.post("/api/v1/exports", json=PERIOD)
    assert created.status_code == 201, created.text
    job = created.json()
    assert job["params"]["layout"] == "simple" and job["filename"] == preview["filename"]
    content = client.get(f"/api/v1/exports/{job['id']}/download").content
    ws = openpyxl.load_workbook(io.BytesIO(content), data_only=True)["Körjournal"]
    file_rows = [
        {"date": row["Datum"].date().isoformat(), "time": row["Tid"], "from": row["Från"], "to": row["Till"],
         "purpose": row["Ärende"], "odometer_start": row["Mätarställning start"],
         "odometer_end": row["Mätarställning slut"], "km": row["Km"]}
        for row in table(ws)
    ]
    assert [{k: v for k, v in row.items() if k not in ("trip_id", "address_missing")}
            for row in preview["rows"]] == file_rows
    pos = summary_rows(ws)
    totals = preview["totals"]
    assert ws.cell(pos["total"], 2).value == totals["trips"] == 3
    assert ws.cell(pos["total"], 8).value == pytest.approx(totals["km"])
    assert ws.cell(pos["mil"], 7).value == pytest.approx(totals["mil"])
    assert ws.cell(pos["amount"], 7).value == pytest.approx(totals["amount"])
    for key in ("trips", "km", "mil", "amount"):
        assert job["summary"][key] == totals[key]


def test_preview_validation(client):
    assert client.get("/api/v1/exports/preview").status_code == 400
    assert client.get("/api/v1/exports/preview", params={"from": "2026-10-01", "to": "2026-09-01"}).status_code == 400


def test_default_layout_is_simple_and_detailed_gives_six_sheets(client, db, user, vehicle, trips, actor):
    trips.trip(A, C, category=Category.WORK, purpose="Kundbesök", km=12.4)
    db.commit()
    simple_job = client.post("/api/v1/exports", json=PERIOD).json()
    detailed_job = client.post("/api/v1/exports", json={**PERIOD, "layout": "detailed"}).json()
    assert client.post("/api/v1/exports", json={**PERIOD, "layout": "fancy"}).status_code == 422

    def sheets(job):
        return openpyxl.load_workbook(io.BytesIO(client.get(f"/api/v1/exports/{job['id']}/download").content)).sheetnames

    assert simple_job["params"]["layout"] == "simple" and sheets(simple_job) == ["Körjournal"]
    assert detailed_job["params"]["layout"] == "detailed" and len(sheets(detailed_job)) == 6
    assert detailed_job["summary"]["layout"] == "detailed"

    checks = client.get("/api/v1/exports/checks", params={**PERIOD, "layout": "detailed"}).json()
    assert checks["layout"] == "detailed" and checks["summary"] is None
    checks = client.get("/api/v1/exports/checks", params=PERIOD).json()
    assert checks["layout"] == "simple" and checks["summary"]["trips"] == 1


def test_layout_setting_is_used_when_no_layout_is_given(db, user, vehicle, actor, trips):
    trips.trip(A, C, category=Category.WORK, purpose="Kundbesök", km=12.4)
    settings_svc.update_section(db, user.id, "export", {"layout": "detailed"})
    job, path = export(db, user, vehicle, actor)
    assert job.params["layout"] == "detailed" and len(books(path)[0].sheetnames) == 6
    job, path = export(db, user, vehicle, actor, layout="simple")
    assert books(path)[0].sheetnames == ["Körjournal"]
    with pytest.raises(export_svc.ExportError):
        export(db, user, vehicle, actor, layout="fancy")


def test_detailed_workbook_follows_business_only_scope(db, user, vehicle, actor, trips):
    from app.services.export import workbook as wbmod

    trips.trip(A, C, category=Category.WORK, purpose="Kundbesök", km=12.0)
    trips.skip(hours=3, km=250)
    trips.trip(C, D, category=Category.WORK, purpose="Möte", km=8.0)
    gaps.recompute(db, vehicle.id)
    job, path = export(db, user, vehicle, actor, layout="detailed")
    assert job.is_complete
    _, wv = books(path)
    text = "\n".join(str(c.value) for ws in wv.worksheets for row in ws.iter_rows() for c in row if c.value is not None)
    assert "OFULLSTÄNDIG" not in text and "ofullständig" not in text
    assert "Öppen – saknar journalpost" not in text
    assert wbmod.STATUS_READY in text


# ----------------------------------------------------------------------------- LibreOffice


def test_libreoffice_opens_and_recalculates_the_file(db, user, vehicle, actor, trips, tmp_path):
    exe = _soffice()
    for km in (12.4, 25.0, 7.35, 101.25):
        trips.trip(A, C, category=Category.WORK, purpose="Kundbesök", km=km, gap_minutes=60)
    _, path = export(db, user, vehicle, actor)

    profile = tmp_path / "lo-profile"
    (profile / "user").mkdir(parents=True)
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
    assert (pdf_dir / (path.stem + ".pdf")).read_bytes().startswith(b"%PDF")

    csv_dir = tmp_path / "csv"
    _run_soffice(exe, profile, ["--convert-to", "csv:Text - txt - csv (StarCalc):44,34,76,1,,0,false,true,false,false",
                                "--outdir", str(csv_dir), str(path)])
    (csv_file,) = list(csv_dir.iterdir())
    with csv_file.open(encoding="utf-8") as fh:
        lines = list(csv.reader(fh))
    _, wv = books(path)
    ws = wv["Körjournal"]
    pos = summary_rows(ws)
    for key, col in (("total", 8), ("mil", 7), ("amount", 7)):
        recalculated = float(lines[pos[key] - 1][col - 1])
        assert recalculated == pytest.approx(float(ws.cell(pos[key], col).value), abs=0.001), key
    assert float(lines[pos["total"] - 1][1]) == 4


# ----------------------------------------------------------------------------- behörighet


def test_other_users_simple_export_and_preview_are_not_accessible(client, db, vehicle):
    other = M.User(email="annan@example.com", display_name="Annan", password_hash=passwords.hash_password(TEST_PASSWORD))
    db.add(other)
    db.flush()
    car = M.Vehicle(owner_user_id=other.id, name="Annans bil", registration_number="XYZ999", is_default=True)
    db.add(car)
    db.flush()
    job = export_svc.create_export(db, user_id=other.id, vehicle=car, start=SEPT.start, end=SEPT.end,
                                   actor=audit.user_actor(other.id), tz=TZ)
    db.commit()
    assert job.params["layout"] == "simple"
    assert client.get(f"/api/v1/exports/{job.id}").status_code == 404
    assert client.get(f"/api/v1/exports/{job.id}/download").status_code == 404
    assert client.get("/api/v1/exports/preview", params={**PERIOD, "vehicle_id": str(car.id)}).status_code == 404
    assert client.get(f"/api/v1/exports/{uuid.uuid4()}").status_code == 404


def test_preview_requires_login(anon_client):
    assert anon_client.get("/api/v1/exports/preview", params=PERIOD).status_code == 401
    assert anon_client.post("/api/v1/exports", json=PERIOD).status_code in (401, 403)
