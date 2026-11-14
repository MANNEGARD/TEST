"""REST-API:t från början till slut: inloggning, import, journal, granskning, säkerhet."""

from __future__ import annotations

import io

import pytest
from sqlalchemy import select

import app.models as M
from app.security import passwords
from tests.conftest import TEST_PASSWORD
from tests.fixtures import volvo_exports as fx

API = "/api/v1"


def upload(client, files: list[tuple[str, bytes]]):
    payload = [("files", (name, io.BytesIO(data), "application/octet-stream")) for name, data in files]
    return client.post(f"{API}/imports/preview", files=payload)


def import_sample(client, **commit):
    batch = upload(client, fx.sample_files()).json()
    result = client.post(f"{API}/imports/{batch['id']}/commit", json=commit)
    assert result.status_code == 200, result.text
    return batch, result.json()


# ----------------------------------------------------------------------------- autentisering


def test_first_run_setup_then_login(anon_client):
    c = anon_client
    assert c.get(f"{API}/auth/setup-status").json() == {"needs_setup": True}
    assert c.get(f"{API}/trips").status_code == 401
    weak = c.post(f"{API}/auth/setup", json={"email": "jag@example.com", "password": "kort"})
    assert weak.status_code == 400 and weak.json()["code"] == "weak_password"
    ok = c.post(f"{API}/auth/setup", json={"email": "Jag@Example.com", "password": TEST_PASSWORD})
    assert ok.status_code == 200 and ok.json()["user"]["email"] == "jag@example.com"
    assert c.get(f"{API}/auth/setup-status").json() == {"needs_setup": False}
    again = c.post(f"{API}/auth/setup", json={"email": "annan@example.com", "password": TEST_PASSWORD})
    assert again.status_code == 409  # bara första kontot kan skapas utan inloggning
    cookie = ok.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert c.get(f"{API}/vehicles").status_code == 200  # standardfordon finns


def test_login_failures_are_generic_and_rate_limited(anon_client, db, user):
    db.commit()
    c = anon_client
    bad = c.post(f"{API}/auth/login", json={"email": user.email, "password": "fel-lösenord"})
    unknown = c.post(f"{API}/auth/login", json={"email": "ingen@example.com", "password": "fel-lösenord"})
    assert bad.status_code == unknown.status_code == 401
    assert bad.json()["detail"] == unknown.json()["detail"]  # avslöjar inte om kontot finns
    for _ in range(10):
        last = c.post(f"{API}/auth/login", json={"email": user.email, "password": "fel-lösenord"})
    assert last.status_code == 429


def test_csrf_token_is_required_for_changes(client):
    token = client.headers.pop("X-CSRF-Token")
    r = client.patch(f"{API}/settings/merge", json={"auto_max_minutes": 10})
    assert r.status_code == 403 and r.json()["code"] == "csrf_failed"
    client.headers["X-CSRF-Token"] = "fel"
    assert client.patch(f"{API}/settings/merge", json={"auto_max_minutes": 10}).status_code == 403
    client.headers["X-CSRF-Token"] = token
    assert client.patch(f"{API}/settings/merge", json={"auto_max_minutes": 10}).status_code == 200


def test_logout_invalidates_session(client):
    assert client.post(f"{API}/auth/logout").status_code == 204
    assert client.get(f"{API}/auth/me").status_code == 401


def test_password_change_and_hashing(client, db, user):
    r = client.post(f"{API}/auth/change-password", json={"current_password": "fel", "new_password": "nytt-långt-lösenord-1"})
    assert r.status_code == 400
    r = client.post(f"{API}/auth/change-password",
                    json={"current_password": TEST_PASSWORD, "new_password": "nytt-långt-lösenord-1"})
    assert r.status_code == 204
    db.expire_all()
    stored = db.get(M.User, user.id).password_hash
    assert stored.startswith("$argon2") and "nytt-långt" not in stored
    assert passwords.verify_password(stored, "nytt-långt-lösenord-1")


def test_api_token_scopes(client, anon_client):
    created = client.post(f"{API}/auth/tokens", json={"name": "Home Assistant", "scopes": ["ha"]})
    assert created.status_code == 201
    token = created.json()["token"]
    listed = client.get(f"{API}/auth/tokens").json()
    assert "token" not in listed[0] and listed[0]["token_prefix"] == token[:10]  # klartexten visas bara en gång
    headers = {"Authorization": f"Bearer {token}"}
    assert anon_client.get(f"{API}/auth/whoami", headers=headers).json()["auth"] == "token"
    # Maskintoken får inte hantera konton eller säkerhetskopior.
    assert anon_client.get(f"{API}/auth/tokens", headers=headers).status_code == 403
    assert anon_client.post(f"{API}/backups", headers=headers).status_code == 403
    assert anon_client.get(f"{API}/auth/whoami", headers={"Authorization": "Bearer dl_fel"}).status_code == 401
    client.delete(f"{API}/auth/tokens/{created.json()['id']}")
    assert anon_client.get(f"{API}/auth/whoami", headers=headers).status_code == 401


def test_security_headers_and_error_shape(client):
    r = client.get(f"{API}/trips/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404 and set(r.json()) == {"detail", "code"}
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-store"
    bad = client.get(f"{API}/trips?sort=drop+table")
    assert bad.status_code == 422 and bad.json()["code"] == "validation_error"


# ----------------------------------------------------------------------------- import → journal


def test_import_flow_and_journal(client):
    preview = upload(client, fx.sample_files())
    assert preview.status_code == 201, preview.text
    batch = preview.json()
    assert batch["status"] == "preview" and batch["summary"]["rows_new"] == 32
    assert client.get(f"{API}/trips").json()["total"] == 0  # inget skapas vid förhandsgranskning
    rows = client.get(f"{API}/imports/{batch['id']}/rows?page_size=5").json()
    assert rows["total"] == 32 and rows["items"][0]["raw"]["Anteckningar"]

    result = client.post(f"{API}/imports/{batch['id']}/commit", json={"coverage": "selection"}).json()
    assert result["imported"] == 32 and result["auto_merged"] >= 1 and result["open_gaps"] > 0
    journal = client.get(f"{API}/trips?page_size=100").json()
    assert journal["total"] == 32 - result["auto_merged"]
    expected_km = sum(fx.totals(fx.month_rows(2026, m, 0))["km"] for m in (6, 7))
    assert journal["totals"]["distance_km"] == pytest.approx(expected_km, abs=1.0)
    assert journal["totals"]["work_km"] == journal["totals"]["distance_km"]

    # filter, sökning och sortering
    assert client.get(f"{API}/trips?from=2026-07-01&to=2026-07-31").json()["total"] < journal["total"]
    hits = client.get(f"{API}/trips?q=hemresa").json()
    assert hits["total"] >= 1 and all("hemresa" in (t["purpose"] or "").lower() for t in hits["items"])
    by_km = client.get(f"{API}/trips?sort=distance&order=desc&page_size=3").json()["items"]
    assert by_km[0]["distance_km"] >= by_km[1]["distance_km"] >= by_km[2]["distance_km"]
    assert client.get(f"{API}/trips?category=private").json()["total"] == 0

    # dubbel uppladdning ger inga nya resor
    again = upload(client, fx.sample_files()).json()
    assert again["summary"]["rows_new"] == 0

    # ångra
    undone = client.post(f"{API}/imports/{batch['id']}/undo").json()
    assert undone["voided"] == 32 and client.get(f"{API}/trips").json()["total"] == 0


def test_trip_detail_shows_originals_and_never_claims_actual_route(client):
    import_sample(client)
    merged = next(t for t in client.get(f"{API}/trips?page_size=100").json()["items"] if t["segment_count"] > 1)
    d = client.get(f"{API}/trips/{merged['id']}").json()
    assert len(d["raw_trips"]) == merged["segment_count"] == len(d["merged_from"])
    assert d["raw_trips"][0]["import_origin"]["file"].endswith(".xlsx")
    assert d["raw_trips"][0]["original_notes"]
    assert d["map"]["route_is_actual"] is False and "inte den faktiskt körda" in d["map"]["route_note"]
    assert sum(s["distance_km"] for s in d["segments"]) == pytest.approx(merged["distance_km"], abs=0.01)
    assert d["merge_operations"][0]["trigger"] == "rule_a"
    assert merged["merge_kind"] == "auto" and len(merged["via"]) == merged["segment_count"] - 1


def test_correct_trip_with_one_call_and_undo(client):
    import_sample(client)
    trip = client.get(f"{API}/trips?page_size=1").json()["items"][0]
    r = client.patch(f"{API}/trips/{trip['id']}", json={"category": "private"})
    assert r.status_code == 200 and r.json()["category"] == "private" and r.json()["review_status"] == "approved"
    assert r.json()["private_km"] == pytest.approx(trip["distance_km"])
    detail = client.get(f"{API}/trips/{trip['id']}").json()
    fb = detail["feedback"][0]
    assert fb["original_category"] == "work" and fb["new_category"] == "private" and fb["kind"] == "correction"
    assert any(h["action"] == "trip.update" for h in detail["history"])
    undone = client.post(f"{API}/learning/feedback/{fb['id']}/undo").json()
    assert undone["category"] == "work" and undone["review_status"] == "imported"


def test_manual_merge_split_and_allocation(client):
    import_sample(client, apply_merge_rules=False)
    items = client.get(f"{API}/trips?sort=date&order=asc&page_size=100").json()["items"]
    a, b = items[0], items[1]
    merged = client.post(f"{API}/trips/merge", json={"trip_ids": [a["id"], b["id"]]})
    assert merged.status_code == 200, merged.text
    m = merged.json()
    assert m["distance_km"] == pytest.approx(a["distance_km"] + b["distance_km"], abs=0.01)
    not_adjacent = client.post(f"{API}/trips/merge", json={"trip_ids": [m["id"], items[5]["id"]]})
    assert not_adjacent.status_code == 409 and not_adjacent.json()["code"] in ("not_adjacent", "gap_requires_ack")

    detail = client.get(f"{API}/trips/{m['id']}").json()
    raw_id = detail["raw_trips"][0]["id"]
    km = detail["raw_trips"][0]["distance_km"]
    bad = client.post(f"{API}/trips/{m['id']}/allocate",
                      json={"raw_trip_id": raw_id, "parts": [{"distance_km": km - 5, "category": "work"}]})
    assert bad.status_code == 409 and bad.json()["code"] == "allocation_sum_mismatch"
    ok = client.post(
        f"{API}/trips/{m['id']}/allocate",
        json={"raw_trip_id": raw_id, "parts": [{"distance_km": km - 5, "category": "work"},
                                              {"distance_km": 5, "category": "private"}]},
    ).json()
    assert ok["private_km"] == pytest.approx(5) and ok["has_mixed_categories"] is True
    assert ok["distance_km"] == pytest.approx(m["distance_km"], abs=0.01)

    parts = client.post(f"{API}/trips/{m['id']}/unmerge").json()
    assert {p["id"] for p in parts} == {a["id"], b["id"]}


def test_review_center_merge_question_and_gaps(client):
    _, result = import_sample(client)
    review = client.get(f"{API}/review").json()
    assert review["open_counts"]["merge"] == result["merge_questions"] >= 1
    q = next(i for i in review["items"] if i["kind"] == "merge")
    assert q["first_trip"] and q["second_trip"] and "Stillestånd" in q["title"]
    before = client.get(f"{API}/trips").json()["total"]
    answered = client.post(f"{API}/merge-questions/{q['id']}/answer", json={"accept": True}).json()
    assert answered["merged"] is True and client.get(f"{API}/trips").json()["total"] == before - 1
    assert client.post(f"{API}/merge-questions/{q['id']}/answer", json={"accept": True}).status_code == 400

    gaps = client.get(f"{API}/gaps").json()
    assert gaps["open_count"] == result["open_gaps"] and gaps["open_km"] == result["open_gap_km"]
    gap = gaps["items"][0]
    private = client.post(f"{API}/gaps/{gap['id']}/resolve-private", json={"note": "Privat"}).json()
    assert private["category"] == "private" and private["distance_km"] == gap["km"]
    assert private["started_at"] is None and private["start_time_quality"] == "bounded"
    after = client.get(f"{API}/gaps").json()
    assert after["open_count"] == gaps["open_count"] - 1 and after["resolved_private_km"] == gap["km"]


def test_locations_suggestions_and_confirmation(client):
    import_sample(client)
    sugg = client.get(f"{API}/locations/suggestions").json()
    home = next(s for s in sugg["types"] if s["suggested_type"] == "home")
    assert home["address"] == fx.HOME and home["confirmed"] is False
    accepted = client.post(f"{API}/locations/{home['id']}/accept-suggestion").json()
    assert accepted["type"] == "home" and accepted["confirmed"] is True and accepted["name"] == "Hemmet"
    meta = client.get(f"{API}/locations/meta").json()
    assert meta["has_confirmed_home"] is True
    locs = client.get(f"{API}/locations").json()
    office = next(loc for loc in locs if loc["address"] == fx.OFFICE_A)
    assert office["stats"]["arrivals"] > 0 and office["stats"]["most_common_category"] == "work"
    r = client.patch(f"{API}/locations/{office['id']}", json={"name": "Storstadskontoret", "type": "office",
                                                               "latitude": 59.27, "longitude": 15.21})
    assert r.status_code == 200 and r.json()["coordinate_source"] == "user"
    second_home = client.patch(f"{API}/locations/{office['id']}", json={"type": "home"})
    assert second_home.status_code == 409 and second_home.json()["code"] == "home_exists"
    assert client.delete(f"{API}/locations/{office['id']}").status_code == 409  # används av resor


def test_manual_trip_and_settings_validation(client):
    r = client.post(f"{API}/trips/manual", json={
        "started_at": "2026-09-14T08:00:00+02:00", "ended_at": "2026-09-14T08:30:00+02:00",
        "start_address": "Hemvägen 1, Hemby", "end_address": "Kundgatan 2, Stad", "distance_km": 25.5,
        "odometer_start": 1000, "odometer_end": 1026, "category": "work", "purpose": "Kundbesök",
    })
    assert r.status_code == 201, r.text
    trip = r.json()
    assert trip["source"] == "manual" and trip["review_status"] == "approved" and "user_entered" in trip["quality_flags"]
    assert client.delete(f"{API}/trips/{trip['id']}").status_code == 204

    bad = client.patch(f"{API}/settings/merge", json={"auto_max_minutes": 90, "ask_max_minutes": 30})
    assert bad.status_code == 400
    assert client.patch(f"{API}/settings/okänd", json={}).status_code == 404
    assert client.patch(f"{API}/settings/ai", json={"provider": "skynet"}).status_code == 400
    ok = client.patch(f"{API}/settings/merge", json={"auto_max_minutes": 10, "ask_max_minutes": 45}).json()
    assert (ok["auto_max_minutes"], ok["ask_max_minutes"]) == (10, 45)


def test_upload_limits_and_bad_files(client, monkeypatch):
    monkeypatch.setenv("DRIVELOG_MAX_UPLOAD_BYTES", "2000")
    from app.config import reset_config_cache

    reset_config_cache()
    big = upload(client, [("stor.xlsx", b"x" * 5000)])
    assert big.status_code == 413
    reset_config_cache()
    monkeypatch.delenv("DRIVELOG_MAX_UPLOAD_BYTES")
    reset_config_cache()
    junk = upload(client, [("skrap.xlsx", b"inte excel"), ("makro.xlsm", b"x")]).json()
    assert {f["status"] for f in junk["summary"]["files"]} == {"error"}
    assert client.post(f"{API}/imports/{junk['id']}/commit", json={}).json()["imported"] == 0


def test_users_cannot_reach_each_others_data(client, db):
    import_sample(client)
    trip_id = client.get(f"{API}/trips?page_size=1").json()["items"][0]["id"]
    other = M.User(email="annan@example.com", password_hash=passwords.hash_password(TEST_PASSWORD))
    db.add(other)
    db.commit()
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as c2:
        login = c2.post(f"{API}/auth/login", json={"email": "annan@example.com", "password": TEST_PASSWORD})
        c2.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        assert c2.get(f"{API}/trips").json()["total"] == 0
        assert c2.get(f"{API}/trips/{trip_id}").status_code == 404
        assert c2.patch(f"{API}/trips/{trip_id}", json={"category": "private"}).status_code == 404
        vehicle_id = client.get(f"{API}/vehicles").json()[0]["id"]
        assert c2.get(f"{API}/trips?vehicle_id={vehicle_id}").status_code == 404
        assert c2.get(f"{API}/locations").json() == []
        batch_id = client.get(f"{API}/imports").json()["items"][0]["id"]
        assert c2.get(f"{API}/imports/{batch_id}").status_code == 404


def test_integrations_overview_never_claims_connection_or_leaks_secrets(client):
    data = client.get(f"{API}/integrations").json()
    assert set(data) == {"volvo", "microsoft", "home_assistant", "ai"}
    assert all(v["status"] == "not_configured" and v["is_connected"] is False for v in data.values())
    saved = client.put(f"{API}/integrations/ai", json={"provider": "anthropic", "model": "", "api_key": "sk-ant-hemlig-nyckel"})
    assert saved.status_code == 200
    body = saved.text
    assert "sk-ant-hemlig-nyckel" not in body
    assert saved.json()["status"] == "configured" and saved.json()["is_connected"] is False if "is_connected" in saved.json() else True
    assert saved.json()["has_key"]["anthropic"] is True
    assert "sk-ant-hemlig-nyckel" not in client.get(f"{API}/integrations").text
    removed = client.delete(f"{API}/integrations/ai").json()
    assert removed["has_key"]["anthropic"] is False and removed["rule_engine_only"] is True


def test_secrets_are_encrypted_at_rest(client, db, user):
    client.put(f"{API}/integrations/ai", json={"provider": "openai", "model": "", "api_key": "sk-openai-hemlig"})
    db.expire_all()
    conn = db.execute(select(M.IntegrationConnection).where(M.IntegrationConnection.provider == "ai")).scalar_one()
    assert "sk-openai-hemlig" not in (conn.secrets_encrypted or "") and conn.secrets_encrypted.startswith("gAAAA")


def test_backup_roundtrip(client, db, engine):
    from app.db import Base
    from app.services import backup

    import_sample(client)
    created = client.post(f"{API}/backups").json()
    assert created["name"].startswith("drivelog-backup-")
    assert client.get(f"{API}/backups/..%2F..%2Fetc%2Fpasswd/download").status_code == 404
    listed = client.get(f"{API}/backups").json()
    assert listed[0]["name"] == created["name"]
    path = backup.backup_path(created["name"])
    counts = backup.read_manifest(path)["tables"]
    assert counts["raw_trips"] == 32 and "auth_sessions" not in counts

    with pytest.raises(backup.BackupError):
        backup.restore_backup(db, path)  # vägrar skriva över befintliga data utan --force
    db.rollback()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    restored = backup.restore_backup(db, path)
    db.commit()
    assert restored["raw_trips"] == 32 and restored["logical_trips"] == counts["logical_trips"]
    merged = db.execute(select(M.LogicalTrip).where(M.LogicalTrip.merged_into_id.is_not(None))).scalars().all()
    assert merged  # självreferenser återställda


def test_delete_history_requires_confirmation(client):
    import_sample(client)
    r = client.post(f"{API}/settings/delete-history", json={"scope": "trips", "confirm": "ja"})
    assert r.status_code == 400 and client.get(f"{API}/trips").json()["total"] > 0
    r = client.post(f"{API}/settings/delete-history", json={"scope": "trips", "confirm": "RADERA"})
    assert r.status_code == 200 and r.json()["removed"]["raw_trips"] == 32
    assert client.get(f"{API}/trips").json()["total"] == 0
    assert client.get(f"{API}/imports").json()["items"] == []


def test_openapi_is_documented(client):
    spec = client.get(f"{API}/openapi.json").json()
    tags = {t for p in spec["paths"].values() for op in p.values() for t in op.get("tags", [])}
    for expected in ("Authentication", "Vehicles", "Vehicle telemetry", "Trips", "Trip merging", "Classification",
                     "Learning feedback", "Calendar matching", "Locations", "Imports", "Settings",
                     "Integration health"):
        assert expected in tags, expected
    assert client.get(f"{API}/health").json()["status"] == "ok"
    assert client.get(f"{API}/health/ready").json()["checks"]["database"] == "ok"
