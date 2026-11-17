"""1.2.2: avrundning till hela kilometer i mätarställningen ger aldrig ärende eller lucka.

Bilen och Volvos körjournal anger hela kilometer. Två resor i följd kan därför överlappa
eller glappa med en kilometer av ren avrundning – oavsett hur låg sammanslagningens
tolerans är sparad. Verkliga dubbletter och överlapp ger fortfarande ärende, och öppna
ärenden som inte längre gäller stängs av sig själva (hanterade ärenden rörs inte).
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

import app.models as M
from app.models.enums import GapKind, ReviewKind, ReviewState
from app.services import audit
from app.services import settings as settings_svc
from app.services.stats import anomalies
from app.services.trips import gaps, merge, pipeline, review
from app.services.trips import segments as seg

A, B, C = "Startgatan 1, Påhittby", "Mellanvägen 2, Påhittby", "Slutgatan 3, Påhittby"


def _tolerance(db, user, km: float) -> float:
    settings_svc.update_section(db, user.id, "merge", {"odometer_tolerance_km": km})
    db.flush()
    return km


def _overlap_items(db, vehicle, *, status=(ReviewState.OPEN, ReviewState.SNOOZED)) -> list[M.ReviewItem]:
    return list(
        db.execute(
            select(M.ReviewItem).where(
                M.ReviewItem.vehicle_id == vehicle.id,
                M.ReviewItem.dedupe_key.like("odo-overlap:%"),
                M.ReviewItem.status.in_(status),
            )
        ).scalars()
    )


def _current_gaps(db, vehicle) -> list[M.DataGap]:
    return list(
        db.execute(
            select(M.DataGap).where(
                M.DataGap.vehicle_id == vehicle.id, M.DataGap.is_current.is_(True), M.DataGap.kind == GapKind.ODOMETER
            )
        ).scalars()
    )


def _pair(trips, first_end: float, second_start: float, *, km2: float = 25.0):
    """Två resor i följd: den första slutar på ``first_end``, den andra börjar på ``second_start``."""
    a = trips.trip(A, B, km=36.0, odometer_start=first_end - 36)
    trips.skip(hours=3)
    b = trips.trip(B, C, km=km2, odometer_start=second_start)
    return a, b


def test_exempel_fran_utredningen(db, user, vehicle, trips, actor):
    """Ärendet hos ägaren: 197736 km respektive 197735 km. Det uppstår i 1.2.1 när toleransen
    är sparad under 1 km (t.ex. 0) – med standardvärdet 2 km kan texten aldrig skapas."""
    _tolerance(db, user, 0.0)
    _pair(trips, 197736, 197735)
    pipeline.after_trips_changed(db, user_id=user.id, vehicle=vehicle, actor=actor, notify=False)
    assert _overlap_items(db, vehicle) == []
    # Inte heller som lucka eller som fråga om överlappande resor.
    assert _current_gaps(db, vehicle) == []
    overlap = db.execute(select(M.ReviewItem).where(M.ReviewItem.dedupe_key.like("overlap:%"))).scalars().all()
    assert overlap == []


@pytest.mark.parametrize("tolerance", [0.0, 0.5, 1.0, 2.0])
@pytest.mark.parametrize("second_start", [197735, 197735.4, 197735.6, 197736, 197737])
def test_avrundning_larmar_aldrig(db, user, vehicle, trips, tolerance, second_start):
    _pair(trips, 197736, second_start)
    out = gaps.recompute(db, vehicle.id, _tolerance(db, user, tolerance))
    assert out["odometer_overlaps"] == 0 and _overlap_items(db, vehicle) == []
    assert _current_gaps(db, vehicle) == []  # ett glapp på 1 km är ingen lucka


@pytest.mark.parametrize("tolerance", [0.0, 2.0])
def test_verkligt_overlapp_och_verklig_lucka_finns_kvar(db, user, vehicle, trips, tolerance):
    _pair(trips, 197736, 197726)  # 10 km överlapp
    trips.skip(hours=3)
    trips.trip(C, A, km=12.0, odometer_start=197800)  # 49 km lucka
    gaps.recompute(db, vehicle.id, _tolerance(db, user, tolerance))
    (item,) = _overlap_items(db, vehicle)
    # 1.3.2: samma typ, tal och klartext som avvikelsesökningen (samma nyckel).
    assert item.data["type"] == "odometer_backwards"
    assert (item.data["first_odometer_end"], item.data["second_odometer_start"]) == (197736, 197726)
    assert "10 km överlapp" in item.detail
    (gap,) = _current_gaps(db, vehicle)
    assert gap.km == pytest.approx(49.0)


def test_verklig_dubblett_ger_arende(db, user, vehicle, trips):
    trips.trip(A, B, km=30.0, odometer_start=50000)
    trips.trip(A, B, km=30.0, odometer_start=50000)  # samma resa två gånger
    gaps.recompute(db, vehicle.id, _tolerance(db, user, 0.0))
    assert len(_overlap_items(db, vehicle)) == 1


def test_resa_inuti_en_annan_ger_overlapp_men_ingen_falsk_lucka(db, user, vehicle, trips):
    trips.trip(A, B, km=40.0, odometer_start=1000)  # 1000–1040
    trips.trip(B, B, km=10.0, odometer_start=1010)  # 1010–1020 ligger inuti
    trips.trip(B, C, km=20.0, odometer_start=1040)  # fortsätter där den långa resan slutade
    out = gaps.recompute(db, vehicle.id, 2.0)
    assert out["odometer_overlaps"] == 1 and len(_overlap_items(db, vehicle)) == 1
    assert _current_gaps(db, vehicle) == []  # 1.2.1 såg en lucka 1020–1040


def test_inaktuella_arenden_stangs_men_hanterade_rors_inte(db, user, vehicle, trips):
    a, b = _pair(trips, 197736, 197735)
    trips.skip(hours=3)
    c = trips.trip(C, A, km=5.0, odometer_start=197761)
    (ra,), (rb,), (rc,) = (seg.ordered_raws(t) for t in (a, b, c))

    def old_item(first, second, status=ReviewState.OPEN) -> M.ReviewItem:
        item, _ = review.upsert(
            db, vehicle_id=vehicle.id, kind=ReviewKind.ANOMALY, dedupe_key=f"odo-overlap:{first.id}:{second.id}",
            title="Motstridiga mätarställningar", detail="Två resor överlappar i mätarställning.",
            data={"type": "odometer_overlap", "first_raw_trip_id": str(first.id), "second_raw_trip_id": str(second.id)},
        )
        item.status = status
        return item

    open_item = old_item(ra, rb)
    snoozed = old_item(rb, rc, ReviewState.SNOOZED)
    snoozed.snoozed_until = snoozed.created_at + timedelta(days=1)
    dismissed = old_item(rc, ra, ReviewState.DISMISSED)
    db.flush()

    gaps.recompute(db, vehicle.id, _tolerance(db, user, 0.0))
    for item in (open_item, snoozed):
        assert item.status == ReviewState.RESOLVED and item.resolved_by == "system"
        assert item.resolution["reason"] == review.NO_LONGER_REASON
        assert "avrundningen" in item.resolution["note"]
    assert dismissed.status == ReviewState.DISMISSED and dismissed.resolution == {}


def test_omrakningen_stanger_arendet_hos_en_befintlig_installation(db, user, vehicle, trips, actor):
    """Uppgradering: ärendet skapades med tolerans 0 i 1.2.1 och stängs vid första omräkningen."""
    _tolerance(db, user, 0.0)
    a, b = _pair(trips, 197736, 197735)
    ra, rb = seg.ordered_raws(a)[0], seg.ordered_raws(b)[0]
    item, _ = review.upsert(
        db, vehicle_id=vehicle.id, kind=ReviewKind.ANOMALY, dedupe_key=f"odo-overlap:{ra.id}:{rb.id}",
        title="Motstridiga mätarställningar",
        detail="Två resor överlappar i mätarställning (197736 km respektive 197735 km).",
        data={"type": "odometer_overlap", "first_raw_trip_id": str(ra.id), "second_raw_trip_id": str(rb.id)},
    )
    pipeline.after_trips_changed(db, user_id=user.id, vehicle=vehicle, actor=actor, notify=False)
    assert item.status == ReviewState.RESOLVED and item.resolution["reason"] == review.NO_LONGER_REASON


# ----------------------------------------------------------------------------- sammanslagningen


@pytest.mark.parametrize("odo_gap", [-1.0, -0.5, 0.0, 0.5, 1.0])
def test_sammanslagningen_bortser_fran_avrundning(odo_gap):
    gap = merge.StopGap(10.0, 10.0, True, odo_gap)
    assert merge.decide(gap, auto_max=15, ask_max=60, odometer_tolerance=0.0) == "auto"


@pytest.mark.parametrize(("odo_gap", "verdict"), [(-5.0, "overlap"), (5.0, "odometer_gap")])
def test_sammanslagningen_ser_verkliga_avvikelser(odo_gap, verdict):
    gap = merge.StopGap(10.0, 10.0, True, odo_gap)
    assert merge.decide(gap, auto_max=15, ask_max=60, odometer_tolerance=0.0) == verdict


def test_inaktuell_fraga_om_overlappande_resor_stangs(db, user, vehicle, trips, actor):
    a, b = _pair(trips, 197736, 197735)
    ra, rb = seg.ordered_raws(a)[0], seg.ordered_raws(b)[0]
    item, _ = review.upsert(
        db, vehicle_id=vehicle.id, kind=ReviewKind.ANOMALY, dedupe_key=f"overlap:{ra.id}:{rb.id}",
        title="Överlappande resor", detail="Två resor överlappar varandra i tid eller mätarställning.",
        logical_trip_id=b.id, data={"first_trip_id": str(a.id), "second_trip_id": str(b.id), "type": "overlap"},
    )
    cfg = {**settings_svc.get_section(db, user.id, "merge"), "odometer_tolerance_km": 0.0}
    merge.evaluate_vehicle(db, vehicle_id=vehicle.id, merge_settings=cfg, actor=actor, apply_auto=False)
    assert item.status == ReviewState.RESOLVED and item.resolution["reason"] == review.NO_LONGER_REASON


def test_verkligt_overlapp_i_tid_ger_fortfarande_fraga(db, user, vehicle, trips, actor):
    a = trips.trip(A, B, km=20.0, minutes=60)
    b = trips.trip(B, C, km=20.0, minutes=30, start_at=a.ended_at - timedelta(minutes=30))
    cfg = settings_svc.get_section(db, user.id, "merge")
    merge.evaluate_vehicle(db, vehicle_id=vehicle.id, merge_settings=cfg, actor=actor, apply_auto=False)
    items = db.execute(select(M.ReviewItem).where(M.ReviewItem.dedupe_key.like("overlap:%"))).scalars().all()
    assert [i.status for i in items] == [ReviewState.OPEN]
    assert b.id == items[0].logical_trip_id


# ----------------------------------------------------------------------------- nattliga avvikelsesökningen


def test_avvikelsesokningen_larmar_inte_for_avrundning_och_stanger_gamla(db, user, vehicle, trips):
    _tolerance(db, user, 0.0)
    a, b = _pair(trips, 197736, 197735)
    ra, rb = seg.ordered_raws(a)[0], seg.ordered_raws(b)[0]
    old, _ = review.upsert(
        db, vehicle_id=vehicle.id, kind=ReviewKind.ANOMALY, dedupe_key=f"odo-overlap:{ra.id}:{rb.id}",
        title="Motstridiga mätarställningar", detail="En senare resa börjar på 197735 km.",
        data={"type": "odometer_backwards", "first_raw_trip_id": str(ra.id), "second_raw_trip_id": str(rb.id)},
    )
    anomalies.scan_vehicle(db, vehicle, user_id=user.id)
    assert old.status == ReviewState.RESOLVED and old.resolution["reason"] == review.NO_LONGER_REASON
    assert _overlap_items(db, vehicle) == []


def test_avvikelsesokningen_ser_verkligt_overlapp(db, user, vehicle, trips):
    _tolerance(db, user, 0.0)
    _pair(trips, 197736, 197720)
    anomalies.scan_vehicle(db, vehicle, user_id=user.id)
    assert len(_overlap_items(db, vehicle)) == 1


def test_installningen_skrivs_inte_over(db, user, vehicle, trips, actor):
    """Ett lågt sparat värde kan vara ägarens eget val: det ändras inte, men larmar inte längre."""
    _tolerance(db, user, 0.0)
    _pair(trips, 197736, 197735)
    pipeline.after_trips_changed(db, user_id=user.id, vehicle=vehicle, actor=audit.SYSTEM, notify=False)
    assert settings_svc.get_section(db, user.id, "merge")["odometer_tolerance_km"] == 0.0


# ----------------------------------------------------------------------------- efter granskningen


@pytest.mark.parametrize("tolerance", [0.0, 1.0])
def test_tva_km_ar_inte_avrundning(db, user, vehicle, trips, tolerance):
    """Två avläsningar i hela km kan tillsammans avrunda 1 km fel, aldrig 2 km: med en låg
    sparad tolerans ger 2 km fortfarande ärende och lucka som i 1.2.1."""
    _pair(trips, 1036, 1034, km2=20.0)  # 2 km överlapp (1034–1054)
    trips.skip(hours=3)
    trips.trip(C, A, km=5.0, odometer_start=1056)  # 2 km glapp efter 1034–1054
    gaps.recompute(db, vehicle.id, _tolerance(db, user, tolerance))
    assert len(_overlap_items(db, vehicle)) == 1
    (gap,) = _current_gaps(db, vehicle)
    assert gap.km == pytest.approx(2.0)


def test_kort_resa_importerad_tva_ganger_ger_arende(db, user, vehicle, trips):
    trips.trip(A, B, km=2.0, odometer_start=5000)
    trips.skip(hours=3)
    trips.trip(A, B, km=2.0, odometer_start=5000)  # samma korta resa, annan tid
    gaps.recompute(db, vehicle.id, _tolerance(db, user, 0.0))
    assert len(_overlap_items(db, vehicle)) == 1


@pytest.mark.parametrize(("odo_gap", "verdict"), [(-2.0, "overlap"), (2.0, "odometer_gap")])
def test_sammanslagningen_ser_tva_km_med_lag_tolerans(odo_gap, verdict):
    gap = merge.StopGap(10.0, 10.0, True, odo_gap)
    assert merge.decide(gap, auto_max=15, ask_max=60, odometer_tolerance=0.0) == verdict


def test_en_felskriven_slutstallning_ger_ett_arende_och_luckorna_finns_kvar(db, user, vehicle, trips):
    """En slutställning med en siffra för mycket får inte göra varje senare resa till ett
    överlapp eller dölja verkliga luckor (som i 1.2.1: ett ärende, luckan finns kvar)."""
    first = trips.trip(A, B, km=30.0, odometer_start=10000)
    seg.ordered_raws(first)[0].odometer_end = 100030
    odo = 10030
    for i in range(10):
        trips.skip(hours=3)
        start = odo + (25 if i == 4 else 0)  # en verklig lucka på 25 km
        trips.trip(B, C, km=20.0, odometer_start=start)
        odo = start + 20
    db.flush()
    out = gaps.recompute(db, vehicle.id, _tolerance(db, user, 2.0))
    assert out["odometer_overlaps"] == 1 and len(_overlap_items(db, vehicle)) == 1
    assert [(g.odometer_start, g.odometer_end) for g in _current_gaps(db, vehicle)] == [(10110, 10135)]


@pytest.mark.parametrize("scope", ["complete", "business_only"])
def test_intygad_privat_lucka_inom_avrundningen_behalls(db, user, vehicle, trips, actor, monkeypatch, scope):
    """En lucka på 1 km som ägaren i 1.2.1 (tolerans 0) intygade som privat körning: posten
    behålls efter uppgraderingen och inget ärende om ändrad privat körning skapas."""
    settings_svc.update_section(db, user.id, "general", {"journal_scope": scope})
    _pair(trips, 1036, 1037)
    monkeypatch.setattr(seg, "ODOMETER_ROUNDING_KM", 0.0)  # så räknade 1.2.1
    gaps.recompute(db, vehicle.id, _tolerance(db, user, 0.0), scope=scope)
    (gap,) = _current_gaps(db, vehicle)
    fill = gaps.resolve_as_private(db, gap, actor=actor)
    db.flush()
    monkeypatch.undo()
    pipeline.after_trips_changed(db, user_id=user.id, vehicle=vehicle, actor=audit.SYSTEM, notify=False)
    db.refresh(fill)
    db.refresh(gap)
    assert fill.state == "active" and gap.status == "resolved_private" and gap.is_current
    stale = db.execute(select(M.ReviewItem).where(M.ReviewItem.dedupe_key.like("gap-fill-stale:%"))).scalars().all()
    assert stale == []


def test_avvikelsesokningens_arende_stangs_vid_omrakningen(db, user, vehicle, trips, actor):
    """Nattsökningen och omräkningen delar nyckeln odo-overlap:. Har nattsökningen skrivit
    ärendet senast stängs det ändå vid nästa omräkning när överlappet är borta."""
    a, b = _pair(trips, 1036, 1030)
    pipeline.after_trips_changed(db, user_id=user.id, vehicle=vehicle, actor=actor, notify=False)
    anomalies.scan_vehicle(db, vehicle, user_id=user.id)
    (item,) = _overlap_items(db, vehicle)
    assert item.data["type"] == "odometer_backwards"
    rb = seg.ordered_raws(b)[0]
    rb.odometer_start, rb.odometer_end = 1036, 1056  # ägaren rättar mätarställningen
    db.flush()
    pipeline.after_trips_changed(db, user_id=user.id, vehicle=vehicle, actor=actor, notify=False)
    assert item.status == ReviewState.RESOLVED and item.resolution["reason"] == review.NO_LONGER_REASON
