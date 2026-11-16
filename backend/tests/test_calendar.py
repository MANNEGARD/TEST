"""Outlook/Microsoft Graph: OAuth, synk, matchning mot resor."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy import select

import app.models as M
from app.models.enums import (
    Category,
    IntegrationProvider,
    IntegrationStatus,
    LocationType,
    ReviewKind,
    ReviewStatus,
)
from app.services import audit, integrations, oauth_state
from app.services import settings as settings_svc
from app.services.calendar import graph, matching, sync
from app.services.classify import engine
from tests.conftest import BASE_TIME

HOME = "Hemvägen 1, Hemby"
BRANCH = "Verkstadsvägen 1, Sjöstad"
OTHER = "Hamngatan 5, Västerstad"


def event(db, user, subject, start, minutes=120, **kw) -> M.CalendarEvent:
    ev = M.CalendarEvent(
        user_id=user.id, calendar_id="cal", external_id=f"ev-{subject}-{start.isoformat()}", subject=subject,
        start_at=start, end_at=start + timedelta(minutes=minutes), **kw,
    )
    db.add(ev)
    db.flush()
    return ev


def match(db, user, trip):
    return matching.match_trip(db, trip, user_id=user.id, is_demo=False)


@pytest.fixture()
def places(trips):
    return {
        "home": trips.location(HOME, name="Hemmet", type=LocationType.HOME),
        "branch": trips.location(BRANCH, name="Sjöstadskontoret", type=LocationType.OFFICE),
    }


# ----------------------------------------------------------------------------- matchning


def test_arrival_before_meeting_at_matching_place_is_strong_signal(db, user, trips, places):
    """Spec 9.2: körning som slutar 09:40, möte 10:00–12:00 på samma ort."""
    t = trips.trip(HOME, BRANCH, km=200, minutes=130, start_at=BASE_TIME)  # slutar 08:10 UTC
    ev = event(db, user, "Verksamhetsmöte Sjöstad", t.ended_at + timedelta(minutes=20), location_name="Sjöstadskontoret")
    (m,) = match(db, user, t)
    assert m.is_selected and not m.is_conflict and m.relation == "arrival"
    assert m.time_score == 1.0 and m.geo_score >= 0.6 and m.score > 0.8

    a = engine.classify_trip(db, t, user_id=user.id, actor=audit.SYSTEM)
    kinds = {s.kind for s in a.signals}
    assert "calendar_match" in kinds and "known_office" in kinds
    assert a.category == Category.WORK and a.review_status == ReviewStatus.AUTO
    assert t.purpose == "Verksamhetsmöte Sjöstad."  # rubriken nämner redan orten
    assert t.purpose_confirmed is False  # kalendern ger kontext – användaren bekräftar
    assert ev.id == m.calendar_event_id


def test_purpose_combines_subject_and_place(db, user, trips, places):
    t = trips.trip(HOME, BRANCH, km=200, minutes=130)
    event(db, user, "Personalmöte", t.ended_at + timedelta(minutes=15), location_city="Sjöstad")
    match(db, user, t)
    engine.classify_trip(db, t, user_id=user.id, actor=audit.SYSTEM)
    assert t.purpose == "Personalmöte på Sjöstadskontoret."


def test_same_day_meeting_far_in_time_is_not_matched(db, user, trips, places):
    t = trips.trip(HOME, BRANCH, km=200, minutes=130)
    event(db, user, "Kvällsmöte Sjöstad", t.ended_at + timedelta(hours=7), location_name="Sjöstadskontoret")
    assert match(db, user, t) == []


def test_meeting_at_other_city_is_a_conflict_not_a_match(db, user, vehicle, trips, places):
    t = trips.trip(HOME, BRANCH, km=200, minutes=130)
    event(db, user, "Kundmöte", t.ended_at + timedelta(minutes=10), location_name="Hamnkontoret",
          location_address="Hamngatan 5, Västerstad", location_city="Västerstad")
    (m,) = match(db, user, t)
    assert m.is_conflict and not m.is_selected and m.score == 0
    item = db.execute(select(M.ReviewItem).where(M.ReviewItem.kind == ReviewKind.CALENDAR_CONFLICT)).scalar_one()
    assert item.logical_trip_id == t.id and "annan plats" in item.detail
    a = engine.classify_trip(db, t, user_id=user.id, actor=audit.SYSTEM)
    assert a.requires_review and a.review_status != ReviewStatus.AUTO
    assert not any(s.kind == "calendar_match" for s in a.signals)


def test_online_meeting_is_too_weak_to_match(db, user, trips, places):
    t = trips.trip(HOME, BRANCH, km=200, minutes=130)
    event(db, user, "Teams-avstämning", t.ended_at + timedelta(minutes=10), is_online=True,
          location_name="Microsoft Teams-möte")
    rows = match(db, user, t)
    assert not any(m.is_selected for m in rows)


def test_meeting_without_location_gives_only_moderate_score(db, user, trips, places):
    t = trips.trip(HOME, BRANCH, km=200, minutes=130)
    event(db, user, "Avstämning", t.ended_at + timedelta(minutes=10))
    (m,) = match(db, user, t)
    assert m.is_selected and m.geo_basis == "no_location" and m.score < 0.6


def test_cancelled_free_and_all_day_events_are_ignored(db, user, trips, places):
    t = trips.trip(HOME, BRANCH, km=200, minutes=130)
    start = t.ended_at + timedelta(minutes=10)
    event(db, user, "Inställt Sjöstad", start, is_cancelled=True, location_name="Sjöstadskontoret")
    event(db, user, "Ledig Sjöstad", start, show_as="free", location_name="Sjöstadskontoret")
    event(db, user, "Heldag Sjöstad", start, is_all_day=True, location_name="Sjöstadskontoret")
    assert match(db, user, t) == []


def test_best_of_several_candidates_is_selected(db, user, trips, places):
    t = trips.trip(HOME, BRANCH, km=200, minutes=130)
    weak = event(db, user, "Avstämning", t.ended_at + timedelta(minutes=10))
    strong = event(db, user, "Ledningsgrupp Sjöstadskontoret", t.ended_at + timedelta(minutes=30),
                   location_name="Sjöstadskontoret")
    rows = {m.calendar_event_id: m for m in match(db, user, t)}
    assert rows[strong.id].is_selected and not rows[weak.id].is_selected
    assert rows[strong.id].score > rows[weak.id].score


def test_user_choice_is_preserved_on_rematch(db, user, trips, places):
    t = trips.trip(HOME, BRANCH, km=200, minutes=130)
    weak = event(db, user, "Avstämning", t.ended_at + timedelta(minutes=10))
    strong = event(db, user, "Ledningsgrupp Sjöstadskontoret", t.ended_at + timedelta(minutes=30),
                   location_name="Sjöstadskontoret")
    rows = {m.calendar_event_id: m for m in match(db, user, t)}
    matching.set_user_choice(db, t, rows[weak.id], selected=True)
    again = {m.calendar_event_id: m for m in match(db, user, t)}
    assert again[weak.id].is_selected and again[weak.id].decided_by == "user"
    assert not again[strong.id].is_selected


def test_rejected_match_is_not_reselected(db, user, trips, places):
    t = trips.trip(HOME, BRANCH, km=200, minutes=130)
    event(db, user, "Möte Sjöstadskontoret", t.ended_at + timedelta(minutes=10), location_name="Sjöstadskontoret")
    (m,) = match(db, user, t)
    matching.set_user_choice(db, t, m, selected=False)
    (again,) = match(db, user, t)
    assert not again.is_selected


def test_departure_after_meeting_supports_return_trip(db, user, trips, places):
    out = trips.trip(HOME, BRANCH, km=200, minutes=130, category=Category.WORK, purpose="Möte på Sjöstadskontoret")
    ev = event(db, user, "Möte Sjöstadskontoret", out.ended_at + timedelta(minutes=10), minutes=180,
               location_name="Sjöstadskontoret")
    back = trips.trip(BRANCH, HOME, km=200, minutes=130, start_at=ev.end_at + timedelta(minutes=15))
    (m,) = match(db, user, back)
    assert m.relation == "departure" and m.is_selected


def test_private_calendar_event_points_to_private(db, user, trips, places):
    t = trips.trip(HOME, OTHER, km=30, minutes=30)
    event(db, user, "Tandläkare", t.ended_at + timedelta(minutes=10), sensitivity="private", location_city="Västerstad")
    match(db, user, t)
    a = engine.classify_trip(db, t, user_id=user.id, actor=audit.SYSTEM)
    assert any(s.kind == "calendar_private" and s.direction == Category.PRIVATE for s in a.signals)
    assert a.suggested_category == Category.PRIVATE and t.purpose is None


def test_abbreviations_are_expanded_when_matching(db, user, trips, places):
    settings_svc.update_section(db, user.id, "abbreviations", {"map": {"SJST": "Sjöstad"}})
    t = trips.trip(HOME, "Okända vägen 3, Sjöstad", km=200, minutes=130)
    event(db, user, "APT i SJST", t.ended_at + timedelta(minutes=10))
    (m,) = match(db, user, t)
    assert m.geo_basis == "subject_city" and m.is_selected  # 1.3.0: orten står i rubriken


def test_classification_works_without_any_calendar(db, user, trips, places):
    t = trips.trip(HOME, BRANCH, km=200, minutes=130)
    assert match(db, user, t) == []
    assert engine.classify_trip(db, t, user_id=user.id, actor=audit.SYSTEM).category == Category.WORK


# ----------------------------------------------------------------------------- OAuth + synk


def connect(db, user) -> M.IntegrationConnection:
    conn = sync.save_credentials(db, user.id, client_id="app-id", client_secret="hemligt", tenant="common")
    integrations.set_secrets(conn, {"access_token": "at-1", "refresh_token": "rt-1"})
    conn.status = IntegrationStatus.CONNECTED
    conn.token_expires_at = datetime.now(UTC) + timedelta(hours=1)
    db.flush()
    return conn


def test_authorize_url_uses_pkce_and_minimal_scopes(db, user):
    sync.save_credentials(db, user.id, client_id="app-id", client_secret="hemligt", tenant="common")
    url = sync.start_authorization(db, user.id)
    q = parse_qs(urlparse(url).query)
    assert url.startswith("https://login.microsoftonline.com/common/oauth2/v2.0/authorize")
    assert q["code_challenge_method"] == ["S256"] and len(q["code_challenge"][0]) >= 43
    assert set(q["scope"][0].split()) == {"offline_access", "Calendars.Read", "openid", "email"}
    assert q["redirect_uri"] == ["http://testserver/api/v1/integrations/microsoft/callback"]
    state = db.execute(select(M.OAuthState)).scalar_one()
    assert q["state"][0] not in (state.state_hash,)  # endast hash lagras


def test_callback_exchanges_code_and_stores_tokens_encrypted(db, user):
    sync.save_credentials(db, user.id, client_id="app-id", client_secret="hemligt", tenant="common")
    state = parse_qs(urlparse(sync.start_authorization(db, user.id)).query)["state"][0]
    seen = {}
    ACCESS, REFRESH = "atkomsttoken-hemlig-0123456789", "uppdateringstoken-hemlig-9876543210"

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(parse_qs(request.content.decode()))
        return httpx.Response(200, json={"access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 3600,
                                         "scope": "Calendars.Read offline_access"})

    conn = sync.complete_authorization(db, user.id, code="kod", state=state, transport=httpx.MockTransport(handler))
    assert conn.status == IntegrationStatus.CONNECTED
    assert seen["grant_type"] == ["authorization_code"] and "code_verifier" in seen
    assert ACCESS not in (conn.secrets_encrypted or "") and REFRESH not in conn.secrets_encrypted
    assert integrations.secrets(conn)["refresh_token"] == REFRESH
    with pytest.raises(oauth_state.OAuthStateError):  # state är förbrukat
        sync.complete_authorization(db, user.id, code="kod", state=state, transport=httpx.MockTransport(handler))


def test_wrong_state_is_rejected(db, user):
    sync.save_credentials(db, user.id, client_id="app-id", client_secret="hemligt", tenant="common")
    with pytest.raises(oauth_state.OAuthStateError):
        sync.complete_authorization(db, user.id, code="kod", state="påhittat")


def graph_transport(pages: list[list[dict]], calls: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "AT2", "refresh_token": "RT2", "expires_in": 3600})
        if request.url.path.endswith("/me/calendars"):
            return httpx.Response(200, json={"value": [{"id": "cal-1", "name": "Kalender", "isDefaultCalendar": True}]})
        idx = int(request.url.params.get("page", "0"))
        body: dict = {"value": pages[idx]}
        if idx + 1 < len(pages):
            body["@odata.nextLink"] = f"https://graph.microsoft.com/v1.0/me/calendars/cal-1/calendarView?page={idx + 1}"
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler)


def graph_event(i: int, start: datetime, **kw) -> dict:
    return {
        "id": f"id-{i}", "subject": f"Möte {i}", "iCalUId": f"uid-{i}",
        "start": {"dateTime": start.strftime("%Y-%m-%dT%H:%M:%S.0000000"), "timeZone": "UTC"},
        "end": {"dateTime": (start + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.0000000"), "timeZone": "UTC"},
        "location": {"displayName": "Sjöstadskontoret", "address": {"street": "Verkstadsvägen 1", "city": "Sjöstad"},
                     "coordinates": {"latitude": 57.7, "longitude": 14.1}},
        "isOnlineMeeting": False, "showAs": "busy", "sensitivity": "normal", "bodyPreview": "HEMLIG TEXT", **kw,
    }


def test_sync_follows_paging_and_does_not_store_body_by_default(db, user):
    connect(db, user)
    now = datetime.now(UTC)
    calls: list[httpx.Request] = []
    pages = [[graph_event(1, now), graph_event(2, now + timedelta(days=1))], [graph_event(3, now + timedelta(days=2))]]
    result = sync.sync_user(db, user.id, now=now, transport=graph_transport(pages, calls))
    assert result["status"] == "ok" and result["created"] == 3
    events = db.execute(select(M.CalendarEvent).order_by(M.CalendarEvent.start_at)).scalars().all()
    assert [e.subject for e in events] == ["Möte 1", "Möte 2", "Möte 3"]
    assert events[0].location_city == "Sjöstad" and events[0].latitude == 57.7
    assert all(e.body_preview is None for e in events)
    view_calls = [c for c in calls if "calendarView" in c.url.path]
    assert "bodyPreview" not in view_calls[0].url.params["$select"]  # hämtas inte ens
    assert view_calls[0].headers["Prefer"] == 'outlook.timezone="UTC"'
    assert view_calls[0].url.params["startDateTime"].endswith("Z")
    run = db.execute(select(M.IntegrationSyncRun)).scalar_one()
    assert run.status == "ok" and run.stats["created"] == 3


def test_sync_updates_and_marks_removed_events(db, user):
    connect(db, user)
    now = datetime.now(UTC)
    sync.sync_user(db, user.id, now=now, transport=graph_transport([[graph_event(1, now), graph_event(2, now)]], []))
    moved = graph_event(1, now + timedelta(hours=3))
    result = sync.sync_user(db, user.id, now=now, transport=graph_transport([[moved]], []))
    assert result["updated"] == 1 and result["removed"] == 1
    events = {e.external_id: e for e in db.execute(select(M.CalendarEvent)).scalars()}
    assert events["id-2"].removed_at is not None
    assert events["id-1"].start_at == (now + timedelta(hours=3)).replace(microsecond=0)


def test_expired_token_is_refreshed_and_rotated(db, user):
    conn = connect(db, user)
    conn.token_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    calls: list[httpx.Request] = []
    sync.sync_user(db, user.id, transport=graph_transport([[]], calls))
    assert calls[0].url.host == "login.microsoftonline.com"
    assert parse_qs(calls[0].content.decode())["refresh_token"] == ["rt-1"]
    sec = integrations.secrets(conn)
    assert sec["access_token"] == "AT2" and sec["refresh_token"] == "RT2"  # roterad token sparad
    assert calls[1].headers["Authorization"] == "Bearer AT2"


def test_invalid_grant_requires_reauthentication(db, user):
    conn = connect(db, user)
    conn.token_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    transport = httpx.MockTransport(lambda r: httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(sync.CalendarError) as err:
        sync.sync_user(db, user.id, transport=transport)
    assert err.value.code == "needs_reauth" and conn.status == IntegrationStatus.NEEDS_REAUTH


def test_throttling_sets_backoff(db, user):
    conn = connect(db, user)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/me/calendars"):
            return httpx.Response(200, json={"value": [{"id": "cal-1", "isDefaultCalendar": True}]})
        return httpx.Response(429, headers={"Retry-After": "120"}, json={})

    now = datetime.now(UTC)
    result = sync.sync_user(db, user.id, now=now, transport=httpx.MockTransport(handler))
    assert result["status"] == "throttled"
    assert conn.backoff_until >= now + timedelta(seconds=119)
    assert sync.sync_user(db, user.id, now=now + timedelta(seconds=10))["status"] == "throttled"  # inget nytt anrop


def test_disconnect_removes_tokens_and_events(db, user):
    conn = connect(db, user)
    event(db, user, "Möte", datetime.now(UTC))
    sync.disconnect(db, user.id)
    sec = integrations.secrets(conn)
    assert "access_token" not in sec and "refresh_token" not in sec
    assert conn.status == IntegrationStatus.CONFIGURED
    assert db.execute(select(M.CalendarEvent)).scalars().all() == []


def test_graph_datetime_parsing():
    assert graph.parse_graph_datetime({"dateTime": "2026-09-14T08:00:00.0000000", "timeZone": "UTC"}) == datetime(
        2026, 9, 14, 8, 0, tzinfo=UTC
    )
    assert graph.parse_graph_datetime({"dateTime": "2026-09-14T10:00:00+02:00"}) == datetime(2026, 9, 14, 8, 0, tzinfo=UTC)
    assert graph.parse_graph_datetime(None) is None and graph.parse_graph_datetime({"dateTime": "skräp"}) is None


def test_not_connected_is_reported(db, user):
    with pytest.raises(sync.CalendarError) as err:
        sync.sync_user(db, user.id)
    assert err.value.code == "not_connected"
    assert integrations.get(db, user.id, IntegrationProvider.MICROSOFT) is None
    assert json.dumps({}) == "{}"
