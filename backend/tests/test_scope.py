"""Journalens omfång: ``business_only`` (standard) och ``complete``.

business_only = egen bil med milersättning. Endast tjänsteresor redovisas, och körningen
mellan dem är privat – luckor i mätarställningen är varken fel eller varningar.
complete = fullständig körjournal (t.ex. förmånsbil) där all körning ska vara journalförd.

Varje del prövas i båda omfången: inställningar, dashboardens täckning och milersättning
(handräknade värden), exportkontroller, avvikelser, importanteckningar, granskningscentret
och rapporterna.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

import app.models as M
from app.db import utcnow
from app.models.enums import (
    BatchStatus,
    Category,
    Coverage,
    GapStatus,
    ReviewKind,
    ReviewState,
    ReviewStatus,
)
from app.services import settings as settings_svc
from app.services.export import checks as checks_mod
from app.services.export import service as export_svc
from app.services.importer import service as imp
from app.services.stats import anomalies, common, dashboard, reports
from app.services.trips import gaps, merge, pipeline, review
from app.services.trips import segments as seg
from tests.fixtures import volvo_exports as fx

API = "/api/v1"
TZ = ZoneInfo("Europe/Stockholm")
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
SEPT = common.resolve_period("custom", tz=TZ, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30))
PERIOD = {"from": "2026-09-01", "to": "2026-09-30"}
A, B, C, D = "Kundvägen 1, Storstad", "Hemvägen 2, Byn", "Lagergatan 3, Storstad", "Torget 1, Storstad"

BUSINESS, COMPLETE = settings_svc.SCOPE_BUSINESS_ONLY, settings_svc.SCOPE_COMPLETE
# Ord som aldrig får förekomma när luckorna är förväntad privat körning.
ALARMING = ("ofullständig", "saknar journalpost", "kan inte härledas", "komplett fordonsjournal", "oavstäm")
# Exportkontroller som bara har mening för en fullständig körjournal.
GAP_CODES = {
    "open_gaps", "ignored_gaps", "incomplete_period", "odometer_reconciliation", "import_selection",
    "summarized_private", "merged_with_gap", "selection_export",
}


# ----------------------------------------------------------------------------- hjälpare


def set_scope(db, user, scope: str) -> None:
    settings_svc.update_section(db, user.id, "general", {"journal_scope": scope})
    db.flush()


def summary(db, vehicle, period=SEPT):
    return dashboard.summary(db, vehicle=vehicle, start=period.start, end=period.end, tz=TZ, now=NOW)


def run(db, vehicle, **kw):
    return checks_mod.run_checks(db, vehicle=vehicle, start=SEPT.start, end=SEPT.end, tz=TZ, now=NOW, **kw)


def by_code(result) -> dict[str, dict]:
    return {c["code"]: c for c in result}


def calm(*texts: str) -> bool:
    joined = " ".join(texts).lower()
    return not any(word in joined for word in ALARMING)


def review_items(db, vehicle, *kinds: str, status: str | None = ReviewState.OPEN) -> list[M.ReviewItem]:
    q = select(M.ReviewItem).where(M.ReviewItem.vehicle_id == vehicle.id)
    if kinds:
        q = q.where(M.ReviewItem.kind.in_(kinds))
    if status is not None:
        q = q.where(M.ReviewItem.status == status)
    return list(db.execute(q).scalars())


def build_reimbursement_month(db, vehicle, trips) -> dict[str, M.LogicalTrip]:
    """Handräknat underlag med alla granskningslägen och 60 km ej journalförd körning.

    Godkänt/importerat tjänst: 12,4 + 20,0 + 30,0 (delsträcka av blandad resa) = 62,4 km.
    Preliminärt tjänst (auto + förslag): 3,0 + 6,6 = 9,6 km.
    Tjänst som behöver granskas: 4,0 km (räknas varken som godkänt eller preliminärt).
    Privat: 7,6 + 12,0 = 19,6 km. Ej avgjort: 5,0 km.
    """
    t = {}
    t["approved"] = trips.trip(A, C, category=Category.WORK, purpose="Kundbesök", km=12.4)
    trips.skip(hours=1, km=60)  # privat körning mellan två tjänsteresor – journalförs inte
    t["imported"] = trips.trip(C, A, category=Category.WORK, purpose="Leverans", km=20.0,
                               review_status=ReviewStatus.IMPORTED)
    t["private"] = trips.trip(A, B, category=Category.PRIVATE, km=7.6, gap_minutes=30)
    t["undecided"] = trips.trip(B, A, category=Category.UNDECIDED, km=5.0, gap_minutes=30)
    t["auto"] = trips.trip(A, C, category=Category.WORK, purpose="Möte", km=3.0, gap_minutes=30,
                           review_status=ReviewStatus.AUTO)
    t["proposed"] = trips.trip(C, A, category=Category.WORK, purpose="Besök", km=6.6, gap_minutes=30,
                               review_status=ReviewStatus.PROPOSED)
    t["needs_review"] = trips.trip(A, C, category=Category.WORK, purpose="Oklart", km=4.0, gap_minutes=30,
                                   review_status=ReviewStatus.NEEDS_REVIEW)
    t["mixed"] = trips.trip(C, B, category=Category.WORK, purpose="Kundbesök och privat ärende", km=42.0,
                            gap_minutes=30)
    seg.allocate_raw_trip(
        db, t["mixed"], t["mixed"].segments[0].raw_trip_id,
        [{"distance_km": 30.0, "category": Category.WORK}, {"distance_km": 12.0, "category": Category.PRIVATE}], "test",
    )
    info = gaps.recompute(db, vehicle.id)
    assert info["open_gaps"] == 1 and info["open_gap_km"] == pytest.approx(60.0)
    db.flush()
    return t


def two_work_trips_with_gap(db, vehicle, trips, gap_km: float = 60.0):
    t1 = trips.trip(A, C, category=Category.WORK, purpose="Kundbesök", km=20.0)
    trips.skip(hours=2, km=gap_km)
    t2 = trips.trip(C, A, category=Category.WORK, purpose="Retur", km=20.0)
    info = gaps.recompute(db, vehicle.id)
    assert info["open_gaps"] == 1 and info["open_gap_km"] == pytest.approx(gap_km)
    return t1, t2


# ----------------------------------------------------------------------------- inställningar


def test_defaults_follow_the_contract(db, user):
    general = settings_svc.get_section(db, user.id, "general")
    assert general["journal_scope"] == "business_only"
    assert {"driver_name", "employer_name"} <= set(general)
    assert settings_svc.get_section(db, user.id, "tax")["car_benefit"] is False  # egen bil
    export = settings_svc.get_section(db, user.id, "export")
    assert export["layout"] == "simple" and export["show_amount"] is True
    assert export["rate_per_mil"] == 25  # Skatteverkets schablon för egen bil, inkomståret 2026
    assert settings_svc.journal_scope(db, user.id) == BUSINESS
    assert settings_svc.is_business_only(db, user.id)
    assert settings_svc.reimbursement_settings(db, user.id) == (25.0, True)


def test_scope_and_layout_are_enumerations(db, user):
    for bad in ("allt", "", "Business_Only", None, 1, True):
        with pytest.raises(settings_svc.SettingsError):
            settings_svc.update_section(db, user.id, "general", {"journal_scope": bad})
    with pytest.raises(settings_svc.SettingsError, match="business_only eller complete"):
        settings_svc.update_section(db, user.id, "general", {"journal_scope": "privat"})
    for bad in ("fancy", "", None, 2):
        with pytest.raises(settings_svc.SettingsError):
            settings_svc.update_section(db, user.id, "export", {"layout": bad})
    with pytest.raises(settings_svc.SettingsError, match="simple eller detailed"):
        settings_svc.update_section(db, user.id, "export", {"layout": "pdf"})
    assert settings_svc.journal_scope(db, user.id) == BUSINESS  # inget ogiltigt värde sparades

    assert settings_svc.update_section(db, user.id, "general", {"journal_scope": "complete"})["journal_scope"] == "complete"
    assert settings_svc.journal_scope(db, user.id) == COMPLETE and not settings_svc.is_business_only(db, user.id)
    assert settings_svc.update_section(db, user.id, "export", {"layout": "detailed"})["layout"] == "detailed"
    assert settings_svc.update_section(db, user.id, "general", {"journal_scope": "business_only"})["journal_scope"] == BUSINESS
    # Övriga allmänna inställningar påverkas inte.
    assert settings_svc.get_section(db, user.id, "general")["timezone"] == "Europe/Stockholm"


@pytest.mark.parametrize("bad", [-0.01, -5, 1000.01, 5000, "25", None, True, math.nan, math.inf])
def test_rate_per_mil_rejects_invalid_values(db, user, bad):
    with pytest.raises(settings_svc.SettingsError):
        settings_svc.update_section(db, user.id, "export", {"rate_per_mil": bad})
    assert settings_svc.get_section(db, user.id, "export")["rate_per_mil"] == 25


def test_rate_per_mil_and_show_amount_accept_valid_values(db, user):
    for ok in (0, 18.5, 25, 1000):
        assert settings_svc.update_section(db, user.id, "export", {"rate_per_mil": ok})["rate_per_mil"] == ok
    with pytest.raises(settings_svc.SettingsError):
        settings_svc.update_section(db, user.id, "export", {"show_amount": "ja"})
    assert settings_svc.update_section(db, user.id, "export", {"show_amount": False})["show_amount"] is False
    assert settings_svc.reimbursement_settings(db, user.id) == (1000.0, False)


def test_corrupt_stored_values_fall_back_to_defaults(db, user):
    db.add(M.Setting(user_id=user.id, key="general", value={"journal_scope": "trasigt"}))
    db.add(M.Setting(user_id=user.id, key="export", value={"rate_per_mil": "mycket"}))
    db.flush()
    assert settings_svc.journal_scope(db, user.id) == BUSINESS
    assert settings_svc.reimbursement_settings(db, user.id) == (25.0, True)


def test_settings_api_exposes_and_validates_scope(client):
    body = client.get(f"{API}/settings").json()
    assert body["defaults"]["general"]["journal_scope"] == "business_only"
    assert body["defaults"]["tax"]["car_benefit"] is False
    assert body["defaults"]["export"] == {
        "include_private_addresses": False, "include_change_history": True, "layout": "simple",
        "rate_per_mil": 25.0, "show_amount": True, "show_via": True,
    }
    assert body["settings"]["general"]["journal_scope"] == "business_only"

    bad = client.patch(f"{API}/settings/general", json={"journal_scope": "allt"})
    assert bad.status_code == 400 and bad.json()["code"] == "invalid_setting"
    assert "business_only eller complete" in bad.json()["detail"]
    assert client.patch(f"{API}/settings/export", json={"layout": "pdf"}).status_code == 400
    assert client.patch(f"{API}/settings/export", json={"rate_per_mil": -1}).status_code == 400
    assert client.patch(f"{API}/settings/export", json={"rate_per_mil": 1001}).status_code == 400

    ok = client.patch(f"{API}/settings/general", json={"journal_scope": "complete"})
    assert ok.status_code == 200 and ok.json()["journal_scope"] == "complete"
    ok = client.patch(f"{API}/settings/export", json={"rate_per_mil": 18.5, "layout": "detailed", "show_amount": False})
    assert ok.status_code == 200
    assert (ok.json()["rate_per_mil"], ok.json()["layout"], ok.json()["show_amount"]) == (18.5, "detailed", False)


# ----------------------------------------------------------------------------- dashboard


def test_business_only_summary_is_calm_about_gaps(db, user, vehicle, trips):
    build_reimbursement_month(db, vehicle, trips)
    out = summary(db, vehicle)
    assert out["journal_scope"] == "business_only"

    cov = out["coverage"]
    assert cov["scope"] == "business_only" and cov["status"] == "business_only"
    assert cov["label"] == "Endast tjänsteresor redovisas"
    assert "privat" in cov["explanation"] and "ingår inte" in cov["explanation"]
    assert calm(cov["label"], cov["explanation"])

    # Blocket för ej journalförda km finns kvar, men är märkt som förväntat.
    un = out["unjournaled"]
    assert un["open_gap_km"] == 60.0 and un["open_gap_mil"] == 6.0 and un["open_gap_count"] == 1
    assert un["expected"] is True and un["label"] == "Övrig körning (redovisas inte)"
    assert calm(un["label"], un["note"])
    # Sträckan räknas aldrig in i någon kategori.
    assert out["distance"]["private"]["km"] == pytest.approx(19.6)
    assert out["distance"]["total"]["km"] == pytest.approx(100.6)


def test_complete_summary_behaves_as_before(db, user, vehicle, trips):
    build_reimbursement_month(db, vehicle, trips)
    business = summary(db, vehicle)
    set_scope(db, user, COMPLETE)
    out = summary(db, vehicle)
    assert out["journal_scope"] == "complete"

    cov = out["coverage"]
    assert cov["scope"] == "complete" and cov["status"] == "selection" and cov["label"] == "Importerat urval"
    assert "saknar journalpost" in cov["explanation"] and "kan därför inte härledas" in cov["explanation"]
    assert not cov["reconciled"] and not cov["private_total_derivable"]
    un = out["unjournaled"]
    assert un["expected"] is False and un["label"] == "Ej journalförda km"
    assert un["note"] == "Kilometer i mätarställningen som saknar journalpost. Ingår inte i någon kategori."
    assert (un["open_gap_km"], un["open_gap_mil"], un["open_gap_count"]) == (60.0, 6.0, 1)

    # Siffrorna är desamma i båda omfången – bara bedömningen av luckorna skiljer sig.
    for key in ("distance", "trips", "fuel", "electricity", "reimbursement"):
        assert out[key] == business[key], key
    for key in ("open_gap_km", "open_gap_count", "journal_km", "odometer_delta_km", "unreconciled_km", "reconciled"):
        assert cov[key] == business["coverage"][key], key


def test_incomplete_period_is_only_reported_for_a_complete_journal(db, user, vehicle, trips):
    # Manuella resor (inte import) med lucka: fullständig journal → "ofullständig period".
    trips.trip(A, C, category=Category.WORK, purpose="Kundbesök", km=20.0, source="manual")
    trips.skip(hours=2, km=60)
    trips.trip(C, A, category=Category.WORK, purpose="Retur", km=20.0, source="manual")
    gaps.recompute(db, vehicle.id)
    assert summary(db, vehicle)["coverage"]["status"] == "business_only"
    set_scope(db, user, COMPLETE)
    cov = summary(db, vehicle)["coverage"]
    assert cov["status"] == "incomplete" and cov["label"] == "Ofullständig period"
    assert "1 öppna luckor (60 km)" in cov["explanation"]


def test_reconciled_period_is_still_business_only(db, user, vehicle, trips):
    # Samma underlag som test_dashboard.test_reconciled_period_is_complete: mätarställningen stämmer med journalen.
    db.add(M.VehicleOdometerSnapshot(vehicle_id=vehicle.id, at=SEPT.start, odometer_km=10000, source="manual"))
    trips.trip(A, C, category=Category.WORK, purpose="Kundbesök", km=20.0)
    trips.trip(C, A, category=Category.PRIVATE, km=20.0, gap_minutes=60)
    db.add(M.VehicleOdometerSnapshot(vehicle_id=vehicle.id, at=SEPT.end, odometer_km=10040, source="manual"))
    db.flush()
    out = summary(db, vehicle)
    cov = out["coverage"]
    assert cov["status"] == "business_only" and cov["label"] == "Endast tjänsteresor redovisas"
    assert cov["reconciled"] is True and cov["odometer_delta_km"] == 40.0 and cov["unreconciled_km"] == 0.0
    assert out["unjournaled"] == {
        "open_gap_km": 0.0, "open_gap_mil": 0.0, "open_gap_count": 0, "expected": True,
        "label": "Övrig körning (redovisas inte)",
        "note": "Körning mellan de redovisade tjänsteresorna. Den är privat och ingår inte i underlaget.",
    }
    assert (out["reimbursement"]["mil"], out["reimbursement"]["amount"]) == (2.0, 50.0)  # bara tjänsteresan


def test_reimbursement_matches_hand_computed_values(db, user, vehicle, trips):
    build_reimbursement_month(db, vehicle, trips)
    out = summary(db, vehicle)
    assert out["distance"]["work"] == {"km": 76.0, "mil": 7.6}  # all tjänstekörning, oavsett granskningsläge
    assert out["distance"]["preliminary_work"]["km"] == 9.6
    # 62,4 km godkänt/importerat → 6,24 mil × 25 kr = 156,00 kr. 9,6 km preliminärt → 0,96 mil × 25 = 24,00 kr.
    assert out["reimbursement"] == {
        "rate_per_mil": 25.0,
        "show_amount": True,
        "mil": 6.24,
        "km": 62.4,
        "amount": 156.0,
        "preliminary_mil": 0.96,
        "preliminary_amount": 24.0,
        "currency": "SEK",
        "note": "Tjänstemil × ersättning per mil. Endast godkända och importerade tjänsteresor räknas.",
    }


def test_preliminary_trips_never_count_towards_the_amount(db, user, vehicle, trips):
    t = build_reimbursement_month(db, vehicle, trips)
    before = summary(db, vehicle)["reimbursement"]
    # Föraren godkänner den automatiskt klassade resan (3,0 km): den flyttas från preliminärt till beloppet.
    t["auto"].review_status = ReviewStatus.APPROVED
    db.flush()
    after = summary(db, vehicle)["reimbursement"]
    assert (before["km"], before["mil"], before["amount"]) == (62.4, 6.24, 156.0)
    assert (after["km"], after["mil"], after["amount"]) == (65.4, 6.54, 163.5)
    assert (before["preliminary_mil"], before["preliminary_amount"]) == (0.96, 24.0)
    assert (after["preliminary_mil"], after["preliminary_amount"]) == (0.66, 16.5)
    # En resa som behöver granskas räknas inte förrän den är godkänd.
    t["needs_review"].review_status = ReviewStatus.APPROVED
    db.flush()
    assert summary(db, vehicle)["reimbursement"]["km"] == 69.4
    # Privat och oavgjord körning räknas aldrig – inte heller den privata delen av en blandad resa.
    t["private"].review_status = ReviewStatus.APPROVED
    t["undecided"].review_status = ReviewStatus.APPROVED
    db.flush()
    assert summary(db, vehicle)["reimbursement"]["km"] == 69.4


def test_reimbursement_uses_the_configured_rate(db, user, vehicle, trips):
    build_reimbursement_month(db, vehicle, trips)
    settings_svc.update_section(db, user.id, "export", {"rate_per_mil": 18.5, "show_amount": False})
    r = summary(db, vehicle)["reimbursement"]
    # 6,24 mil × 18,50 kr = 115,44 kr. 0,96 mil × 18,50 kr = 17,76 kr.
    assert (r["rate_per_mil"], r["show_amount"]) == (18.5, False)
    assert (r["mil"], r["amount"], r["preliminary_mil"], r["preliminary_amount"]) == (6.24, 115.44, 0.96, 17.76)
    settings_svc.update_section(db, user.id, "export", {"rate_per_mil": 0})
    r = summary(db, vehicle)["reimbursement"]
    assert (r["mil"], r["amount"], r["preliminary_amount"]) == (6.24, 0.0, 0.0)


@pytest.mark.parametrize("scope", [BUSINESS, COMPLETE])
def test_reimbursement_block_exists_in_both_scopes_also_when_empty(db, user, vehicle, trips, scope):
    set_scope(db, user, scope)
    build_reimbursement_month(db, vehicle, trips)
    march = common.resolve_period("custom", tz=TZ, date_from=date(2026, 3, 1), date_to=date(2026, 3, 31))
    empty = summary(db, vehicle, march)
    assert empty["coverage"]["status"] == "no_data" and empty["coverage"]["scope"] == scope
    assert empty["unjournaled"]["expected"] is (scope == BUSINESS)
    r = empty["reimbursement"]
    assert (r["mil"], r["km"], r["amount"], r["preliminary_mil"], r["preliminary_amount"]) == (0.0, 0.0, 0.0, 0.0, 0.0)
    assert r["rate_per_mil"] == 25.0 and r["currency"] == "SEK"
    full = summary(db, vehicle)["reimbursement"]
    assert (full["mil"], full["amount"]) == (6.24, 156.0)


def test_dashboard_api_exposes_scope_and_reimbursement(client, db, vehicle, trips):
    build_reimbursement_month(db, vehicle, trips)
    db.commit()
    params = {"period": "custom", **PERIOD}
    body = client.get(f"{API}/dashboard/summary", params=params).json()
    assert body["journal_scope"] == "business_only"
    assert body["coverage"]["status"] == "business_only" and body["coverage"]["scope"] == "business_only"
    assert body["unjournaled"]["expected"] is True and body["unjournaled"]["open_gap_km"] == 60.0
    assert body["reimbursement"]["mil"] == 6.24 and body["reimbursement"]["amount"] == 156.0
    assert set(body["reimbursement"]) >= {
        "rate_per_mil", "show_amount", "mil", "km", "amount", "preliminary_mil", "preliminary_amount", "currency",
    }

    assert client.patch(f"{API}/settings/general", json={"journal_scope": "complete"}).status_code == 200
    body = client.get(f"{API}/dashboard/summary", params=params).json()
    assert body["journal_scope"] == "complete" and body["coverage"]["status"] == "selection"
    assert body["unjournaled"]["expected"] is False
    assert body["reimbursement"]["amount"] == 156.0


# ----------------------------------------------------------------------------- exportkontroller


def build_every_gap_situation(db, user, vehicle, actor, trips):
    """Öppen lucka, lämnad lucka, sammanfattad privat körning, sammanslagen resa med lucka och importerat urval."""
    t1 = trips.trip(A, C, category=Category.WORK, purpose="Kundbesök", km=10.0)
    trips.skip(hours=1, km=40)
    trips.trip(C, A, category=Category.WORK, purpose="Retur", km=10.0)
    trips.skip(hours=1, km=30)
    trips.trip(A, C, category=Category.WORK, purpose="Möte", km=10.0)
    trips.skip(hours=1, km=25)
    t4 = trips.trip(C, A, category=Category.WORK, purpose="Leverans", km=10.0)
    trips.skip(minutes=10, km=20)
    t5 = trips.trip(A, C, category=Category.WORK, purpose="Leverans", km=10.0)
    gaps.recompute(db, vehicle.id)
    g_open, g_ignored, g_private, g_merged = gaps.list_gaps(db, vehicle.id)
    assert [g.km for g in (g_open, g_ignored, g_private, g_merged)] == [40.0, 30.0, 25.0, 20.0]
    gaps.ignore(db, g_ignored, actor=actor, note="Verkstad")
    gaps.resolve_as_private(db, g_private, actor=actor)
    merged = merge.merge_trips(db, [t4, t5], trigger="manual", actor=actor, acknowledge_gap=True)
    assert merged.has_gap and merged.review_status == ReviewStatus.APPROVED
    gaps.recompute(db, vehicle.id)
    batch = M.ImportBatch(user_id=user.id, vehicle_id=vehicle.id, status=BatchStatus.COMMITTED, coverage=Coverage.SELECTION)
    db.add(batch)
    db.flush()
    t1.segments[0].raw_trip.import_batch_id = batch.id
    db.flush()
    return t1


def test_gaps_are_never_errors_when_only_business_trips_are_reported(db, user, vehicle, actor, trips):
    t1 = build_every_gap_situation(db, user, vehicle, actor, trips)

    # Fullständig körjournal: varje lucksituation anmärks och exporten är inte komplett.
    complete = by_code(run(db, vehicle, scope=COMPLETE))
    assert {"open_gaps", "ignored_gaps", "incomplete_period", "import_selection", "summarized_private",
            "merged_with_gap"} <= set(complete)
    assert complete["open_gaps"]["severity"] == "error" and complete["incomplete_period"]["severity"] == "error"
    assert not checks_mod.is_complete(list(complete.values()))
    assert "selection_export" in by_code(run(db, vehicle, scope=COMPLETE, trip_ids=[t1.id]))

    # Endast tjänsteresor (standard): luckorna är privat körning – inga anmärkningar alls.
    assert run(db, vehicle) == []
    assert run(db, vehicle, scope=BUSINESS) == []
    assert run(db, vehicle, trip_ids=[t1.id]) == []  # att exportera ett urval är helt i sin ordning
    assert checks_mod.summarize(run(db, vehicle)) == {"is_complete": True, "errors": 0, "warnings": 0, "infos": 0}


def test_scope_comes_from_the_setting_and_can_be_overridden(db, user, vehicle, trips):
    two_work_trips_with_gap(db, vehicle, trips)
    assert not GAP_CODES & set(by_code(run(db, vehicle)))  # standard: business_only
    set_scope(db, user, COMPLETE)
    assert {"open_gaps", "incomplete_period"} <= set(by_code(run(db, vehicle)))
    assert not GAP_CODES & set(by_code(run(db, vehicle, scope=BUSINESS)))  # uttryckligt omfång går före
    set_scope(db, user, BUSINESS)
    assert {"open_gaps", "incomplete_period"} <= set(by_code(run(db, vehicle, scope=COMPLETE)))
    with pytest.raises(ValueError, match="journalomfång"):
        run(db, vehicle, scope="allt")


def test_odometer_is_not_reconciled_when_only_business_trips_are_reported(db, user, vehicle, trips):
    db.add(M.VehicleOdometerSnapshot(vehicle_id=vehicle.id, at=SEPT.start, odometer_km=10000, source="manual"))
    trips.trip(A, C, category=Category.WORK, purpose="Kundbesök", km=12.4)
    trips.trip(C, B, category=Category.PRIVATE, km=7.6, gap_minutes=30)
    trips.trip(B, D, category=Category.WORK, purpose="Leverans", km=25.0, gap_minutes=60)
    # Avläsningen vid periodens slut visar 80 km mer än journalen (privat körning efter sista resan).
    db.add(M.VehicleOdometerSnapshot(vehicle_id=vehicle.id, at=SEPT.end, odometer_km=trips._odo + 80, source="manual"))
    db.flush()
    rec = by_code(run(db, vehicle, scope=COMPLETE))["odometer_reconciliation"]
    assert rec["severity"] == "error" and rec["data"]["unexplained_km"] == pytest.approx(80.0)
    assert run(db, vehicle) == []


def test_undecided_and_preliminary_trips_are_warnings_that_do_not_block(db, user, vehicle, trips):
    trips.trip(A, C, category=Category.WORK, purpose="Kundbesök", km=12.0)
    und = trips.trip(C, D, category=Category.UNDECIDED, km=5.0, gap_minutes=30)
    auto = trips.trip(D, A, category=Category.WORK, purpose="Möte", km=8.0, gap_minutes=30, review_status=ReviewStatus.AUTO)
    prop = trips.trip(A, C, category=Category.WORK, km=4.0, gap_minutes=30, review_status=ReviewStatus.PROPOSED)
    rev = trips.trip(C, A, category=Category.WORK, purpose="Oklart", km=3.0, gap_minutes=30,
                     review_status=ReviewStatus.NEEDS_REVIEW)
    # En privat resa med preliminär klassning ingår aldrig i underlaget och anmärks inte.
    trips.trip(A, B, category=Category.PRIVATE, km=9.0, gap_minutes=30, review_status=ReviewStatus.AUTO)

    result = run(db, vehicle)
    codes = by_code(result)
    assert set(codes) == {"undecided_category", "preliminary_classification"}
    assert codes["undecided_category"]["severity"] == "warning"
    assert codes["undecided_category"]["trip_ids"] == [str(und.id)] and codes["undecided_category"]["data"]["km"] == 5.0
    assert "tas inte med i underlaget förrän kategorin har avgjorts" in codes["undecided_category"]["message"]
    prelim = codes["preliminary_classification"]
    assert prelim["severity"] == "warning" and prelim["count"] == 3 and prelim["data"]["km"] == 15.0
    assert set(prelim["trip_ids"]) == {str(auto.id), str(prop.id), str(rev.id)}
    assert "tas inte med i underlaget förrän de har godkänts" in prelim["message"]
    # Den ej godkända resan utan syfte blockerar inte: den ingår inte i underlaget än.
    assert checks_mod.is_complete(result)
    assert checks_mod.summarize(result) == {"is_complete": True, "errors": 0, "warnings": 2, "infos": 0}

    # Fullständig körjournal: samma lägen är fel som gör exporten ofullständig.
    complete = by_code(run(db, vehicle, scope=COMPLETE))
    assert complete["undecided_category"]["severity"] == "error"
    assert complete["preliminary_classification"]["severity"] == "error"
    assert complete["preliminary_classification"]["count"] == 4  # även den privata resan
    assert complete["missing_purpose"]["severity"] == "error"


def test_errors_remain_for_faulty_reported_business_trips(db, user, vehicle, trips):
    trips.trip(A, C, category=Category.WORK, purpose="Kundbesök", km=10.0)  # felfri
    t_missing = trips.trip(C, D, category=Category.WORK, km=5.0, gap_minutes=30)  # godkänd utan syfte
    t_unconf = trips.trip(D, A, category=Category.WORK, purpose="Förslag", purpose_confirmed=False, km=5.0, gap_minutes=30)
    t_neg = trips.trip(A, C, category=Category.WORK, purpose="Leverans", km=5.0, gap_minutes=30)
    t_neg.odometer_end = t_neg.odometer_start - 3
    raw = t_neg.segments[0].raw_trip
    t_dup = trips.trip(A, C, category=Category.WORK, purpose="Leverans", km=5.0, start_at=raw.started_at,
                       odometer_start=raw.odometer_start)
    t_dup.odometer_end = t_neg.odometer_end
    t_sum = trips.trip(C, A, category=Category.WORK, purpose="Retur", km=6.0, gap_minutes=60)
    t_sum.distance_km = 9.0  # cache som inte stämmer med segmenten
    t_noodo = trips.trip(A, C, category=Category.WORK, purpose="Möte", km=3.0, gap_minutes=60)
    t_noodo.odometer_start = None
    db.flush()

    result = run(db, vehicle)
    codes = by_code(result)
    errors = {c for c, v in codes.items() if v["severity"] == "error"}
    assert errors == {"missing_purpose", "unconfirmed_purpose", "negative_distance", "sum_mismatch",
                      "overlapping_trips", "duplicates"}
    assert codes["missing_purpose"]["trip_ids"] == [str(t_missing.id)]
    assert codes["unconfirmed_purpose"]["trip_ids"] == [str(t_unconf.id)]
    assert set(codes["negative_distance"]["trip_ids"]) == {str(t_neg.id), str(t_dup.id)}
    assert codes["sum_mismatch"]["trip_ids"] == [str(t_sum.id)]
    assert set(codes["duplicates"]["trip_ids"]) == {str(t_neg.id), str(t_dup.id)}
    assert set(codes["overlapping_trips"]["trip_ids"]) == {str(t_neg.id), str(t_dup.id)}
    # Saknad mätarställning på en redovisad tjänsteresa är en varning, inget fel.
    assert codes["missing_odometer"]["severity"] == "warning"
    assert codes["missing_odometer"]["trip_ids"] == [str(t_noodo.id)]
    assert not GAP_CODES & set(codes)
    assert not checks_mod.is_complete(result)
    assert [c["severity"] for c in result] == sorted((c["severity"] for c in result),
                                                      key=lambda s: {"error": 0, "warning": 1, "info": 2}[s])


def test_private_trips_give_no_remarks_at_all(db, user, vehicle, trips):
    trips.trip(A, C, category=Category.WORK, purpose="Kundbesök", km=10.0)  # underlaget: en felfri tjänsteresa
    # Samma fel som i testet ovan – men på privata resor.
    t_neg = trips.trip(A, C, category=Category.PRIVATE, km=5.0, gap_minutes=30)
    t_neg.odometer_end = t_neg.odometer_start - 3
    raw = t_neg.segments[0].raw_trip
    t_dup = trips.trip(A, C, category=Category.PRIVATE, km=5.0, start_at=raw.started_at, odometer_start=raw.odometer_start)
    t_dup.odometer_end = t_neg.odometer_end
    t_sum = trips.trip(C, A, category=Category.PRIVATE, km=6.0, gap_minutes=60)
    t_sum.distance_km = 9.0
    t_noodo = trips.trip(A, C, category=Category.PRIVATE, km=3.0, gap_minutes=60)
    t_noodo.odometer_start = None
    trips.trip(C, A, category=Category.PRIVATE, km=4.0, gap_minutes=60, review_status=ReviewStatus.PROPOSED)
    trips.trip(A, C, category=Category.PRIVATE, km=4.0, gap_minutes=60, source="volvo_api", start_known=False,
               possible_missed_trips=True)
    db.flush()

    assert run(db, vehicle) == []
    # Kontrollen är meningsfull: som fullständig körjournal anmärks varje privat resa.
    complete = by_code(run(db, vehicle, scope=COMPLETE))
    assert {"negative_distance", "duplicates", "overlapping_trips", "sum_mismatch", "missing_odometer",
            "preliminary_classification", "uncertain_quality"} <= set(complete)


def test_overlap_between_a_reported_business_trip_and_a_private_trip_is_an_error(db, user, vehicle, trips):
    work = trips.trip(A, C, category=Category.WORK, purpose="Kundbesök", km=10.0, minutes=30)
    private = trips.trip(C, B, category=Category.PRIVATE, km=5.0,
                         start_at=work.started_at + timedelta(minutes=10), odometer_start=work.odometer_end)
    codes = by_code(run(db, vehicle))
    assert set(codes) == {"overlapping_trips"} and codes["overlapping_trips"]["severity"] == "error"
    assert set(codes["overlapping_trips"]["trip_ids"]) == {str(work.id), str(private.id)}
    # Så länge tjänsteresan inte är godkänd ingår den inte i underlaget – då är det bara en varning om det.
    work.review_status = ReviewStatus.AUTO
    db.flush()
    assert set(by_code(run(db, vehicle))) == {"no_trips", "preliminary_classification"}


def test_quality_remarks_only_concern_reported_business_trips(db, user, vehicle, trips):
    api = trips.trip(A, C, category=Category.WORK, purpose="Kundbesök", km=10.0, source="volvo_api")
    off = trips.trip(C, A, category=Category.WORK, purpose="Retur", km=10.0, gap_minutes=30)
    off.odometer_end = off.odometer_start + 40  # 10 km enligt resan, 40 km enligt mätaren
    db.flush()
    codes = by_code(run(db, vehicle))
    # 1.1.0: en resa från bilen med kända tider och adresser ger ingen anmärkning bara för
    # att den kommer från bilen (den tidigare samlade "uncertain_quality" är borttagen).
    assert set(codes) == {"odometer_distance_mismatch"}
    assert codes["odometer_distance_mismatch"]["severity"] == "warning"
    assert codes["odometer_distance_mismatch"]["trip_ids"] == [str(off.id)]

    # Okänd starttid, uträknad tid, saknad adress och möjliga missade resor anmärks var för sig.
    raw = api.segments[0].raw_trip
    api.started_at = raw.started_at = None
    api.start_time_quality = raw.start_time_quality = "bounded"
    api.end_time_quality = raw.end_time_quality = "estimated"
    api.end_address = raw.end_address = None
    api.end_location_id = None  # underlagets cell blir tom
    api.quality_flags = ["possible_missed_trips"]
    db.flush()
    codes = by_code(run(db, vehicle))
    assert {c: codes[c]["severity"] for c in codes if c != "odometer_distance_mismatch"} == {
        "missing_start_time": "warning", "estimated_times": "info", "missing_address": "warning",
        "possible_missed_trips": "warning",
    }
    assert all(codes[c]["trip_ids"] == [str(api.id)] for c in codes if c != "odometer_distance_mismatch")
    assert "uncertain_quality" not in codes and checks_mod.is_complete(run(db, vehicle))


def test_empty_and_demo_underlag(db, user, vehicle, trips):
    result = run(db, vehicle)
    assert [(c["code"], c["severity"]) for c in result] == [("no_trips", "warning")]
    assert result[0]["message"] == "Perioden innehåller inga resor." and checks_mod.is_complete(result)

    trips.trip(A, B, category=Category.PRIVATE, km=7.0)  # bara privat körning → fortfarande inget att redovisa
    result = run(db, vehicle)
    assert [c["code"] for c in result] == ["no_trips"]
    assert result[0]["message"] == "Perioden innehåller inga godkända tjänsteresor."

    trips.trip(B, A, category=Category.WORK, purpose="Kundbesök", km=7.0, gap_minutes=60)
    assert run(db, vehicle) == []
    vehicle.is_demo = True
    db.flush()
    result = run(db, vehicle)
    assert [(c["code"], c["severity"]) for c in result] == [("demo_data", "error")]
    assert not checks_mod.is_complete(result)


def test_status_label_follows_scope():
    error = [{"severity": "error"}]
    warning = [{"severity": "warning"}]
    assert checks_mod.status_label([], BUSINESS) == "Klart att redovisa"
    assert checks_mod.status_label(warning, BUSINESS) == "Klart att redovisa"
    assert checks_mod.status_label(error, BUSINESS) == "Fel behöver åtgärdas"
    assert checks_mod.status_label(warning, COMPLETE) == "Fullständig"
    assert checks_mod.status_label(error, COMPLETE) == "OFULLSTÄNDIG"


def test_export_is_complete_despite_gaps_and_preliminary_trips(db, user, vehicle, actor, trips):
    # Samma underlag som i test_export.test_incomplete_history_is_never_marked_complete.
    t1 = trips.trip(A, C, category=Category.WORK, purpose="Kundbesök", km=12.0)
    trips.skip(hours=3, km=50)
    trips.trip(C, D, category=Category.WORK, purpose="Möte", km=8.0, review_status=ReviewStatus.AUTO)
    gaps.recompute(db, vehicle.id)
    job = export_svc.create_export(db, user_id=user.id, vehicle=vehicle, start=SEPT.start, end=SEPT.end, actor=actor,
                                   tz=TZ, now=NOW)
    assert job.status == "done" and job.is_complete
    codes = {w["code"]: w["severity"] for w in job.warnings}
    assert codes == {"preliminary_classification": "warning"}
    assert (job.summary["is_complete"], job.summary["errors"], job.summary["warnings"]) == (True, 0, 1)

    # Ett urval av resor är ett fullgott underlag.
    selection = export_svc.create_export(db, user_id=user.id, vehicle=vehicle, start=None, end=None, trip_ids=[t1.id],
                                         actor=actor, tz=TZ, now=NOW)
    assert selection.is_complete and selection.warnings == []

    # Som fullständig körjournal är samma underlag ofullständigt.
    set_scope(db, user, COMPLETE)
    job = export_svc.create_export(db, user_id=user.id, vehicle=vehicle, start=SEPT.start, end=SEPT.end, actor=actor,
                                   tz=TZ, now=NOW)
    assert not job.is_complete
    assert {"open_gaps", "incomplete_period", "preliminary_classification"} <= {w["code"] for w in job.warnings}


def test_empty_period_export_is_not_an_error(db, user, vehicle, actor):
    # Jfr test_export.test_empty_period_export: utan avstämning mot mätarställningen finns inget fel i en tom period.
    job = export_svc.create_export(db, user_id=user.id, vehicle=vehicle, start=SEPT.start, end=SEPT.end, actor=actor,
                                   tz=TZ, now=NOW)
    assert job.status == "done" and job.is_complete and job.summary["trips"] == 0
    assert [(w["code"], w["severity"]) for w in job.warnings] == [("no_trips", "warning")]


def test_precheck_api_reports_scope(client, db, vehicle, trips):
    t1, _ = two_work_trips_with_gap(db, vehicle, trips)
    missing = trips.trip(A, D, category=Category.WORK, km=5.0, gap_minutes=600)  # godkänd tjänsteresa utan syfte
    db.commit()

    body = client.get(f"{API}/exports/checks", params=PERIOD).json()
    assert body["scope"] == "business_only" and not body["is_complete"]
    assert body["status_label"] == "Fel behöver åtgärdas" and body["errors"] == 1
    assert [c["code"] for c in body["checks"]] == ["missing_purpose"]

    # Urval utan den felaktiga resan: klart att redovisa, utan anmärkning om att det är ett urval.
    body = client.get(f"{API}/exports/checks", params={"trip_ids": [str(t1.id)]}).json()
    assert body["scope"] == "business_only" and body["is_complete"] and body["checks"] == []
    assert body["status_label"] == "Klart att redovisa"
    assert (body["errors"], body["warnings"], body["infos"]) == (0, 0, 0)

    # En tom period är inget fel.
    body = client.get(f"{API}/exports/checks", params={"from": "2026-10-01", "to": "2026-10-31"}).json()
    assert body["is_complete"] and [c["code"] for c in body["checks"]] == ["no_trips"]

    created = client.post(f"{API}/exports", json={"trip_ids": [str(t1.id)]})
    assert created.status_code == 201 and created.json()["is_complete"]

    assert client.patch(f"{API}/settings/general", json={"journal_scope": "complete"}).status_code == 200
    body = client.get(f"{API}/exports/checks", params=PERIOD).json()
    assert body["scope"] == "complete" and body["status_label"] == "OFULLSTÄNDIG"
    assert {"open_gaps", "incomplete_period", "missing_purpose"} <= {c["code"] for c in body["checks"]}
    body = client.get(f"{API}/exports/checks", params={"trip_ids": [str(t1.id)]}).json()
    assert not body["is_complete"] and "selection_export" in {c["code"] for c in body["checks"]}
    del missing


# ----------------------------------------------------------------------------- avvikelser


def scan(db, user, vehicle):
    return anomalies.scan_vehicle(db, vehicle, user_id=user.id, now=NOW)


def observation(db, vehicle, at: datetime, km: float) -> None:
    db.add(M.VehicleObservation(vehicle_id=vehicle.id, fetched_at=at, last_seen_at=at, odometer_km=km,
                                content_hash=f"h{at.timestamp()}{km}"))
    db.flush()


def build_gap_anomalies(db, vehicle, trips) -> None:
    """Ett kilometergap på 700 km mellan två resor och 1 440 km som fordonet rapporterat utöver journalen."""
    trips.trip(A, C, category=Category.WORK, purpose="Kundbesök", km=10.0)
    trips.skip(hours=5, km=700)
    trips.trip(C, A, category=Category.WORK, purpose="Retur", km=10.0)  # journalen slutar på 10 720 km
    assert gaps.recompute(db, vehicle.id)["open_gap_km"] == pytest.approx(700.0)
    observation(db, vehicle, NOW - timedelta(days=2), 10720)
    observation(db, vehicle, NOW - timedelta(hours=5), 12160)


def test_gap_based_anomalies_are_not_created_when_only_business_trips_are_reported(db, user, vehicle, trips):
    build_gap_anomalies(db, vehicle, trips)
    result = scan(db, user, vehicle)
    assert result["scope"] == "business_only"
    assert result["found"] == {} and result["created_total"] == 0 and result["closed_total"] == 0
    assert review_items(db, vehicle, status=None) == []


def test_gap_based_anomalies_are_created_for_a_complete_journal(db, user, vehicle, trips, complete_scope):
    build_gap_anomalies(db, vehicle, trips)
    result = scan(db, user, vehicle)
    assert result["scope"] == "complete" and result["created"] == {"large_gap": 1, "missed_trips": 1}
    titles = {i.title for i in review_items(db, vehicle, ReviewKind.ANOMALY)}
    assert titles == {"Ovanligt stort kilometergap: 700 km", "Misstänkt missade resor: 1440 km saknar journalpost"}


def test_other_anomalies_are_kept_when_only_business_trips_are_reported(db, user, vehicle, trips):
    build_gap_anomalies(db, vehicle, trips)
    # Körsträcka som inte stämmer med mätaren, bakåtgående mätarställning, dubblett och överlapp.
    t = trips.trip(A, C, category=Category.WORK, purpose="Möte", km=10.0, gap_minutes=600, minutes=30)
    raw = t.segments[0].raw_trip
    raw.odometer_end = raw.odometer_start + 40
    back = trips.trip(C, A, category=Category.PRIVATE, km=10.0, gap_minutes=600)
    back.segments[0].raw_trip.odometer_end = back.segments[0].raw_trip.odometer_start - 5
    d1 = trips.trip(A, B, category=Category.PRIVATE, km=10.0, gap_minutes=600, minutes=30)
    raw1 = d1.segments[0].raw_trip
    trips.trip(A, B, category=Category.PRIVATE, km=10.2, start_at=raw1.started_at + timedelta(seconds=40),
               odometer_start=raw1.odometer_end + 1)
    # Onormal förbrukning: 22 resor kring 6,5–6,9 l/100 km och en på 40 l/100 km.
    raws = [trips.trip(km=20.0, gap_minutes=120).segments[0].raw_trip for _ in range(22)]
    for i, r in enumerate(raws):
        r.fuel_liters = round(20.0 * (0.065 + 0.001 * (i % 5)), 3)
    raws[5].fuel_liters = 20.0 * 0.40
    # Fordonsrapporter: orimligt hopp (+1 600 km på två timmar) och därefter lägre mätarställning.
    observation(db, vehicle, NOW - timedelta(days=4), 10700)
    observation(db, vehicle, NOW - timedelta(days=4, hours=-2), 12300)
    db.flush()

    result = scan(db, user, vehicle)
    assert {"odometer_mismatch", "odometer_backwards", "possible_duplicate", "time_overlap",
            "abnormal_consumption", "odometer_jump"} <= set(result["created"])
    assert result["created"]["odometer_jump"] == 1
    # Bakåt inom en resa, mellan två resor (efter den felaktiga sträckan) och i en fordonsrapport.
    assert result["created"]["odometer_backwards"] == 3
    assert "large_gap" not in result["found"] and "missed_trips" not in result["found"]
    kinds = {i.data.get("type") for i in review_items(db, vehicle, ReviewKind.ANOMALY)}
    assert not kinds & {"large_gap", "missed_trips"}


def test_open_gap_anomalies_are_closed_on_the_next_scan(db, user, vehicle, trips):
    set_scope(db, user, COMPLETE)
    build_gap_anomalies(db, vehicle, trips)
    t = trips.trip(A, C, category=Category.WORK, purpose="Möte", km=10.0, gap_minutes=600)
    t.segments[0].raw_trip.odometer_end = t.segments[0].raw_trip.odometer_start + 40  # en avvikelse som ska vara kvar
    db.flush()
    assert scan(db, user, vehicle)["created"] == {"large_gap": 1, "odometer_mismatch": 1, "missed_trips": 1}
    large = next(i for i in review_items(db, vehicle) if i.data["type"] == "large_gap")
    missed = next(i for i in review_items(db, vehicle) if i.data["type"] == "missed_trips")
    review.snooze(db, missed, NOW + timedelta(days=7))  # även ett uppskjutet ärende stängs

    set_scope(db, user, BUSINESS)
    result = scan(db, user, vehicle)
    assert result["closed"] == {"large_gap": 1, "missed_trips": 1} and result["closed_total"] == 2
    assert result["created_total"] == 0 and result["found"] == {"odometer_mismatch": 1}
    for item in (large, missed):
        assert item.status == ReviewState.RESOLVED and item.resolved_by == "system"
        assert item.resolution == {"auto": True, "reason": "journal_scope"}
    assert [i.data["type"] for i in review_items(db, vehicle)] == ["odometer_mismatch"]
    # Nästa körning har inget mer att stänga.
    assert scan(db, user, vehicle)["closed_total"] == 0

    # Tillbaka till fullständig körjournal: samma ärenden öppnas igen, inga dubbletter skapas.
    set_scope(db, user, COMPLETE)
    result = scan(db, user, vehicle)
    assert result["found"] == {"large_gap": 1, "odometer_mismatch": 1, "missed_trips": 1} and result["created_total"] == 0
    assert large.status == ReviewState.OPEN and missed.status == ReviewState.OPEN and large.resolution == {}
    assert len(review_items(db, vehicle, ReviewKind.ANOMALY, status=None)) == 3


def test_dismissed_gap_anomaly_is_not_reopened_by_a_scope_change(db, user, vehicle, trips):
    set_scope(db, user, COMPLETE)
    build_gap_anomalies(db, vehicle, trips)
    scan(db, user, vehicle)
    large = next(i for i in review_items(db, vehicle) if i.data["type"] == "large_gap")
    review.resolve(db, large, by="user", resolution={"dismissed": True}, dismissed=True)
    set_scope(db, user, BUSINESS)
    assert scan(db, user, vehicle)["closed"] == {"missed_trips": 1}
    set_scope(db, user, COMPLETE)
    scan(db, user, vehicle)
    assert large.status == ReviewState.DISMISSED and large.resolution == {"dismissed": True}


def test_anomaly_scan_api_follows_scope(client, db, vehicle, trips):
    trips.trip(A, C, category=Category.WORK, purpose="Kundbesök", km=10.0)
    trips.skip(hours=5, km=700)
    trips.trip(C, A, category=Category.WORK, purpose="Retur", km=10.0)
    gaps.recompute(db, vehicle.id)
    observation(db, vehicle, utcnow() - timedelta(days=2), 10720)
    observation(db, vehicle, utcnow() - timedelta(hours=5), 12160)
    db.commit()

    body = client.post(f"{API}/dashboard/anomalies/scan").json()
    assert body["scope"] == "business_only" and body["created_total"] == 0 and body["found"] == {}
    assert (body["closed"], body["closed_total"]) == ({}, 0)

    assert client.patch(f"{API}/settings/general", json={"journal_scope": "complete"}).status_code == 200
    body = client.post(f"{API}/dashboard/anomalies/scan").json()
    assert body["scope"] == "complete" and body["created"] == {"large_gap": 1, "missed_trips": 1}
    assert client.get(f"{API}/review").json()["open_counts"]["anomaly"] == 2

    # Bytet till "endast tjänsteresor" stänger ärendena direkt; nästa sökning skapar inga nya.
    assert client.patch(f"{API}/settings/general", json={"journal_scope": "business_only"}).status_code == 200
    assert client.get(f"{API}/review").json()["open_counts"]["anomaly"] == 0
    body = client.post(f"{API}/dashboard/anomalies/scan").json()
    assert body["created_total"] == 0 and body["closed_total"] == 0 and body["found"] == {}


# ----------------------------------------------------------------------------- import


def test_import_notes_are_neutral_when_only_business_trips_are_reported(db, user, vehicle, actor):
    batch = imp.create_preview(db, user_id=user.id, vehicle=vehicle, files=fx.sample_files(), actor=actor)
    s = batch.summary
    assert s["journal_scope"] == "business_only"
    assert s["odometer_gap_count"] > 0 and s["single_category_only"] is True  # uppgifterna finns kvar
    assert s["suggested_coverage"] == Coverage.SELECTION and batch.coverage == Coverage.SELECTION
    assert f"Mellan resorna finns {s['odometer_gap_km']:.0f} km som inte ingår i underlaget (övrig körning)." in s["notes"]
    assert calm(*s["notes"])
    assert not any("samma kategori" in n or "urval" in n for n in s["notes"])

    result = imp.commit(db, batch, actor=actor)
    assert result["open_gaps"] > 0 and calm(*result["notes"])


def test_import_notes_for_a_complete_journal(db, user, vehicle, actor, complete_scope):
    batch = imp.create_preview(db, user_id=user.id, vehicle=vehicle, files=fx.sample_files(), actor=actor)
    s = batch.summary
    assert s["journal_scope"] == "complete" and s["suggested_coverage"] == Coverage.SELECTION
    assert any("inte en komplett fordonsjournal" in n for n in s["notes"])
    assert any("Alla resor har samma kategori" in n for n in s["notes"])
    assert not any("som inte ingår i underlaget" in n for n in s["notes"])


def test_summary_notes_per_scope():
    stops = ("3 resor har kortare körtid än tiden mellan start och slut; "
             "källan har sannolikt redan slagit ihop delsträckor med stopp.")
    assert imp._summary_notes(4, 312.4, True, 3, scope=BUSINESS) == [
        "Mellan resorna finns 312 km som inte ingår i underlaget (övrig körning).", stops,
    ]
    complete = imp._summary_notes(4, 312.4, True, 3, scope=COMPLETE)
    assert len(complete) == 3 and complete[0].startswith("Mätarställningarna har 4 luckor på totalt 312 km")
    assert "inte en komplett fordonsjournal" in complete[0] and complete[1].startswith("Alla resor har samma kategori")
    assert imp._summary_notes(4, 312.4, True, 3) == complete  # utan angivet omfång: som tidigare
    assert imp._summary_notes(0, 0.0, True, 0, scope=BUSINESS) == []
    assert len(imp._summary_notes(0, 0.0, True, 0, scope=COMPLETE)) == 1


# ----------------------------------------------------------------------------- granskningscentret


def test_gaps_alone_never_create_review_items(db, user, vehicle, actor, trips):
    trips.trip(A, C, category=Category.WORK, purpose="Kundbesök", km=20.0)
    trips.skip(hours=5, km=700)  # långt över gränsen för "ovanligt stort kilometergap"
    trips.trip(C, A, category=Category.WORK, purpose="Retur", km=20.0)
    trips.skip(hours=5, km=45)
    trips.trip(A, B, category=Category.PRIVATE, km=8.0)
    result = pipeline.after_trips_changed(db, user_id=user.id, vehicle=vehicle, actor=actor, notify=False)
    assert result["open_gaps"] == 2 and result["open_gap_km"] == pytest.approx(745.0)  # luckorna räknas fram …
    scan(db, user, vehicle)
    assert review_items(db, vehicle, status=None) == []  # … men ger inga ärenden


def test_imported_selection_gives_no_gap_review_items(db, user, vehicle, actor):
    batch = imp.create_preview(db, user_id=user.id, vehicle=vehicle, files=fx.sample_files(), actor=actor)
    result = imp.commit(db, batch, actor=actor)
    assert result["open_gaps"] > 0
    scan(db, user, vehicle)
    kinds = {i.kind for i in review_items(db, vehicle, status=None)}
    assert not kinds & {ReviewKind.DATA_GAP, ReviewKind.ANOMALY}, kinds


def flagged_trips(trips):
    """Två rekonstruerade resor där mätarställningen visar mer än positionerna förklarar."""
    private = trips.trip(A, B, category=Category.PRIVATE, km=30.0, source="volvo_api", possible_missed_trips=True)
    work = trips.trip(B, C, category=Category.WORK, purpose="Kundbesök", km=30.0, gap_minutes=60, source="volvo_api",
                      possible_missed_trips=True)
    assert "possible_missed_trips" in private.quality_flags and "possible_missed_trips" in work.quality_flags
    return private, work


def test_suspected_gap_item_is_skipped_for_private_trips(db, user, vehicle, trips):
    private, work = flagged_trips(trips)
    assert pipeline.sync_trip_review_items(db, private) == []
    assert pipeline.sync_trip_review_items(db, work) == [ReviewKind.DATA_GAP]  # rör en redovisad tjänsteresa
    (item,) = review_items(db, vehicle, ReviewKind.DATA_GAP)
    assert item.logical_trip_id == work.id

    # En oavgjord resa kan vara en tjänsteresa – där behålls ärendet.
    undecided = trips.trip(C, A, category=Category.UNDECIDED, km=30.0, gap_minutes=60, source="volvo_api",
                           possible_missed_trips=True)
    assert ReviewKind.DATA_GAP in pipeline.sync_trip_review_items(db, undecided)
    # När föraren klassar den som privat stängs ärendet av sig självt …
    undecided.category, undecided.review_status = Category.PRIVATE, ReviewStatus.APPROVED
    seg.refresh_logical(db, undecided)
    db.flush()
    pipeline.sync_trip_review_items(db, undecided)
    closed = db.execute(select(M.ReviewItem).where(
        M.ReviewItem.dedupe_key == review.trip_key(ReviewKind.DATA_GAP, undecided.id))).scalar_one()
    assert closed.status == ReviewState.RESOLVED and closed.resolution == {"auto": True, "reason": "journal_scope"}
    # … och öppnas igen om resan ändras till tjänst.
    undecided.category, undecided.purpose, undecided.purpose_confirmed = Category.WORK, "Kundbesök", True
    seg.refresh_logical(db, undecided)
    db.flush()
    pipeline.sync_trip_review_items(db, undecided)
    assert closed.status == ReviewState.OPEN


def test_suspected_gap_item_is_kept_for_a_complete_journal(db, user, vehicle, trips, complete_scope):
    private, work = flagged_trips(trips)
    assert pipeline.sync_trip_review_items(db, private) == [ReviewKind.DATA_GAP]
    assert pipeline.sync_trip_review_items(db, work) == [ReviewKind.DATA_GAP]
    # Uttryckligt omfång går före inställningen.
    assert pipeline.sync_trip_review_items(db, private, scope=BUSINESS) == []
    assert {i.logical_trip_id for i in review_items(db, vehicle, ReviewKind.DATA_GAP)} == {work.id}


def test_manually_resolved_gap_item_stays_resolved(db, user, vehicle, trips, complete_scope):
    private, _ = flagged_trips(trips)
    pipeline.sync_trip_review_items(db, private)
    (item,) = review_items(db, vehicle, ReviewKind.DATA_GAP)
    review.resolve(db, item, by="user", resolution={"manual": True})
    set_scope(db, user, BUSINESS)
    pipeline.sync_trip_review_items(db, private)
    set_scope(db, user, COMPLETE)
    pipeline.sync_trip_review_items(db, private)
    assert item.status == ReviewState.RESOLVED and item.resolution == {"manual": True}


def test_changing_scope_updates_the_review_center_at_once(complete_scope, client, db, user, vehicle, trips):
    build_gap_anomalies(db, vehicle, trips)
    private, work = flagged_trips(trips)
    pipeline.sync_trip_review_items(db, private)
    pipeline.sync_trip_review_items(db, work)
    scan(db, user, vehicle)
    db.commit()
    before = client.get(f"{API}/review").json()
    assert before["open_counts"]["anomaly"] == 2 and before["open_counts"]["data_gap"] == 2

    assert client.patch(f"{API}/settings/general", json={"journal_scope": "business_only"}).status_code == 200
    after = client.get(f"{API}/review").json()
    assert after["open_counts"]["anomaly"] == 0
    assert after["open_counts"]["data_gap"] == 1  # bara tjänsteresans ärende är kvar
    assert [i["trip"]["id"] for i in after["items"] if i["kind"] == "data_gap"] == [str(work.id)]

    # Att ändra något annat under "general" rör inte ärendena.
    assert client.patch(f"{API}/settings/general", json={"driver_name": "Test Förare"}).status_code == 200
    assert client.get(f"{API}/review").json()["open_counts"] == after["open_counts"]

    # Tillbaka till fullständig körjournal: den privata resans ärende blir aktuellt igen direkt.
    assert client.patch(f"{API}/settings/general", json={"journal_scope": "complete"}).status_code == 200
    assert client.get(f"{API}/review").json()["open_counts"]["data_gap"] == 2


def test_gaps_endpoint_reports_scope_and_expected(client, db, vehicle, trips):
    two_work_trips_with_gap(db, vehicle, trips)
    db.commit()
    body = client.get(f"{API}/gaps").json()
    assert body["scope"] == "business_only" and body["expected"] is True
    assert body["label"] == "Övrig körning (redovisas inte)" and calm(body["label"], body["note"])
    # Listan finns kvar oförändrad – gränssnittet avgör om den ska visas.
    assert body["open_count"] == 1 and body["open_km"] == 60.0 and len(body["items"]) == 1
    assert body["items"][0]["km"] == 60.0 and body["items"][0]["status"] == GapStatus.OPEN
    assert client.get(f"{API}/gaps", params={"only_open": True}).json()["expected"] is True

    assert client.patch(f"{API}/settings/general", json={"journal_scope": "complete"}).status_code == 200
    body = client.get(f"{API}/gaps").json()
    assert body["scope"] == "complete" and body["expected"] is False and body["label"] == "Dataluckor"
    assert body["open_count"] == 1 and body["open_km"] == 60.0 and len(body["items"]) == 1


# ----------------------------------------------------------------------------- rapporter


def test_reports_do_not_warn_about_gaps_when_only_business_trips_are_reported(db, user, vehicle, trips):
    two_work_trips_with_gap(db, vehicle, trips)
    weekly = reports.weekly_report(db, vehicle=vehicle, tz=TZ, week_start=date(2026, 9, 16), now=NOW)
    assert weekly["journal_scope"] == "business_only"
    assert weekly["message"] == "Veckans körjournal: 4,0 mil i tjänsten, 0,0 mil privat och 0 resor som behöver granskas."
    assert weekly["data_gaps"] == {"count": 1, "km": 60.0}  # uppgiften finns kvar som data
    daily = reports.daily_reconciliation(db, vehicle=vehicle, tz=TZ, day=date(2026, 9, 14), now=NOW)
    assert daily["journal_scope"] == "business_only" and daily["all_clear"] and daily["problems"] == []
    assert daily["message"] == "Dagens 2 resor (4,0 mil) är klara."

    set_scope(db, user, COMPLETE)
    weekly = reports.weekly_report(db, vehicle=vehicle, tz=TZ, week_start=date(2026, 9, 16), now=NOW)
    assert weekly["journal_scope"] == "complete"
    assert weekly["message"].endswith("1 dataluckor (60 km) saknar journalpost.")
    daily = reports.daily_reconciliation(db, vehicle=vehicle, tz=TZ, day=date(2026, 9, 14), now=NOW)
    assert not daily["all_clear"] and daily["message"].startswith("Dagens körjournal: 2 resor, 4,0 mil.")


def test_daily_report_skips_suspected_gap_for_private_trips(db, user, vehicle, trips):
    private, work = flagged_trips(trips)
    daily = reports.daily_reconciliation(db, vehicle=vehicle, tz=TZ, day=date(2026, 9, 14), now=NOW)
    assert {p["id"]: p["issues"] for p in daily["problems"]} == {str(work.id): ["Misstänkt datalucka"]}
    set_scope(db, user, COMPLETE)
    daily = reports.daily_reconciliation(db, vehicle=vehicle, tz=TZ, day=date(2026, 9, 14), now=NOW)
    assert {p["id"]: p["issues"] for p in daily["problems"]} == {
        str(private.id): ["Misstänkt datalucka"], str(work.id): ["Misstänkt datalucka"],
    }


def test_reports_api_exposes_scope(client, db, vehicle, trips):
    two_work_trips_with_gap(db, vehicle, trips)
    db.commit()
    weekly = client.get(f"{API}/reports/weekly", params={"week_start": "2026-09-16"}).json()
    assert weekly["journal_scope"] == "business_only" and "dataluckor" not in weekly["message"]
    daily = client.get(f"{API}/reports/daily", params={"day": "2026-09-14"}).json()
    assert daily["journal_scope"] == "business_only" and daily["all_clear"] is True
