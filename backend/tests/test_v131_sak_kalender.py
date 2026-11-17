"""1.3.1 – säkerhetsrättningar i kalendertolkningen.

Allt är påhittat och offline: domäner på example/angripare-ab.se, inga riktiga adresser.
Varje test prövar en brist ur säkerhetsgranskningen och att det vanliga flödet är oförändrat.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import icalendar
from sqlalchemy import select

import app.models as M
from app.services.calendar import calendar_text, ics
from tests.test_calendar_ics import LINK, calendar, serve, vevent

API = "/api/v1"
STHLM = ZoneInfo("Europe/Stockholm")
W_START = datetime(2026, 9, 1, tzinfo=UTC)
W_END = datetime(2026, 9, 21, tzinfo=UTC)
NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


def parse(data: bytes, *, include_body: bool = True) -> ics.ParseResult:
    return ics.parse(data, window_start=W_START, window_end=W_END, tz=STHLM, include_body=include_body)


def person(name: str, email: str, role: str = "attendee") -> dict[str, str]:
    return {"name": name, "email": email, "role": role}


# ----------------------------------------------------------------------------- utgaende-7 / behorighet-3


def _callback(client: Any, query: str) -> dict[str, list[str]]:
    resp = client.get(f"{API}/integrations/microsoft/callback?{query}", follow_redirects=False)
    assert resp.status_code == 302
    return parse_qs(urlparse(resp.headers["location"]).query, keep_blank_values=True)


def test_microsoft_callback_error_cannot_inject_parameters_or_log_lines(client, caplog):
    caplog.set_level(logging.INFO, logger="app.api.routes.calendar")
    q = _callback(client, "error=x%26volvo%3Dconnected%0A2026-11-16%20ERROR%20falsk")
    assert "volvo" not in q
    assert q["microsoft"] == ["error"] and q["reason"] == ["denied"]
    assert not any("\n" in r.getMessage() for r in caplog.records)


def test_microsoft_callback_real_error_codes_are_unchanged(client):
    for code in ("access_denied", "consent_required", "invalid_client", "interaction_required"):
        assert _callback(client, f"error={code}")["reason"] == [code]
    assert _callback(client, "")["reason"] == ["missing_code"]
    assert _callback(client, "error=access_denied&error_description=AADSTS65004")["reason"] == ["access_denied"]


# ----------------------------------------------------------------------------- indata-1


def _rule(text: str) -> Any:
    return icalendar.vRecur.from_ical(text)


def test_never_occurs_catches_monthday_with_ordinal_weekday():
    def never(text: str) -> bool:
        rule = _rule(text)
        freq = str(rule["FREQ"][0])
        return ics._never_occurs(rule, freq, ics._per_period(rule, freq))

    assert never("FREQ=MONTHLY;BYMONTHDAY=1;BYDAY=2MO")  # den 1:a är aldrig andra måndagen
    assert never("FREQ=MONTHLY;BYMONTHDAY=1;BYDAY=-1MO")
    assert never("FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=29;BYDAY=1MO")
    assert not never("FREQ=MONTHLY;BYMONTHDAY=8;BYDAY=2MO")
    assert not never("FREQ=MONTHLY;BYMONTHDAY=13;BYDAY=FR")  # fredagen den 13:e finns
    assert not never("FREQ=MONTHLY;BYMONTHDAY=-1;BYDAY=-1FR")
    assert not never("FREQ=MONTHLY;BYMONTHDAY=29,30,31;BYDAY=5SU")
    assert not never("FREQ=MONTHLY;BYDAY=2MO")
    assert not never("FREQ=YEARLY;BYMONTHDAY=1;BYDAY=1MO")  # ordningstal räknas i året


def _with_rules(event: str, rules: list[str]) -> str:
    """Lägger till RRULE-rader sist i en VEVENT."""
    extra = "".join(f"\r\nRRULE:{rule}" for rule in rules)
    return event.replace("\r\nEND:VEVENT", extra + "\r\nEND:VEVENT")


def _hostile_series(rules: int, uid: str = "elak@angripare.example") -> str:
    return _with_rules(
        vevent(uid, DTSTART="20260901T080000Z", DTEND="20260901T090000Z", SUMMARY="Inbjudan"),
        [f"FREQ=MONTHLY;INTERVAL={1 + i // 21};BYMONTHDAY={1 + i % 7};BYDAY={2 + (i // 7) % 3}MO"
         for i in range(rules)],
    )


def test_series_that_never_occurs_does_not_stall_parse_or_drop_later_events():
    data = calendar(
        _hostile_series(40),
        vevent("vanlig@example.invalid", DTSTART="20260910T080000Z", DTEND="20260910T090000Z",
               SUMMARY="Kundbesök", LOCATION="Hamngatan 5"),
    )
    began = time.monotonic()
    result = parse(data, include_body=False)
    assert time.monotonic() - began < 5
    assert not result.truncated
    assert [e["subject"] for e in result.events] == ["Kundbesök"]
    assert result.skipped >= 1


def test_vevent_with_many_rules_is_skipped_but_ordinary_series_kept():
    many = _with_rules(
        vevent("manga@angripare.example", DTSTART="20260901T080000Z", DTEND="20260901T090000Z",
               SUMMARY="Många regler"),
        [f"FREQ=WEEKLY;INTERVAL={i + 1}" for i in range(ics.MAX_RULES + 1)],
    )
    weekly = vevent("vecka@example.invalid", DTSTART="20260901T080000Z", DTEND="20260901T090000Z",
                    SUMMARY="Veckomöte", RRULE="FREQ=WEEKLY", LOCATION="Rum 1")
    result = parse(calendar(many, weekly), include_body=False)
    assert {e["subject"] for e in result.events} == {"Veckomöte"}
    assert len(result.events) == 3  # 1, 8 och 15 september
    assert ics.MAX_RULES >= 10  # långt över en riktig kalender (en regel per händelse)


# ----------------------------------------------------------------------------- indata-n1 (och indata-1 punkt 3)


def _sync(db, user, *events: str) -> dict[str, Any]:
    return ics.sync_user(db, user.id, now=NOW, transport=serve(calendar(*events)))


def _active(db, user) -> list[str]:
    rows = db.execute(select(M.CalendarEvent).where(
        M.CalendarEvent.user_id == user.id, M.CalendarEvent.removed_at.is_(None))).scalars()
    return sorted(e.subject for e in rows)


def _day(i: int) -> str:
    return vevent(f"d{i}@example.invalid", DTSTART=f"202609{10 + i}T080000Z", DTEND=f"202609{10 + i}T090000Z",
                  SUMMARY=f"Möte {i}", LOCATION="Rum 1")


def test_sync_cut_by_time_budget_does_not_mark_events_removed(db, user, monkeypatch):
    days = [_day(i) for i in range(4)]
    ics.save_link(db, user.id, LINK, now=NOW, transport=serve(calendar(*days)))
    assert _active(db, user) == ["Möte 0", "Möte 1", "Möte 2", "Möte 3"]
    monkeypatch.setattr(ics, "EXPAND_SECONDS", -1.0)  # tidsbudgeten slut innan första serien
    out = _sync(db, user, *days)
    assert out["truncated"] is True and out["removed"] == 0
    assert _active(db, user) == ["Möte 0", "Möte 1", "Möte 2", "Möte 3"]


def test_sync_cut_by_occurrence_cap_keeps_events_after_the_cut(db, user, monkeypatch):
    days = [_day(i) for i in range(4)]
    ics.save_link(db, user.id, LINK, now=NOW, transport=serve(calendar(*days)))
    monkeypatch.setattr(ics, "MAX_OCCURRENCES", 2)
    # Möte 0 är borttaget ur länken; Möte 3 räknades aldrig fram (efter kapningen).
    out = _sync(db, user, *days[1:])
    assert out["truncated"] is True and out["removed"] == 1
    assert _active(db, user) == ["Möte 1", "Möte 2", "Möte 3"]


def test_complete_sync_still_removes_deleted_events(db, user):
    days = [_day(i) for i in range(4)]
    ics.save_link(db, user.id, LINK, now=NOW, transport=serve(calendar(*days)))
    out = _sync(db, user, days[0], days[2])
    assert out["removed"] == 2 and _active(db, user) == ["Möte 0", "Möte 2"]


# ----------------------------------------------------------------------------- indata-2


def test_huge_description_in_daily_series_is_cleaned_once():
    huge = ("Agenda " + "x" * 70 + "\\n") * 14_000  # drygt 1 MB
    data = calendar(vevent("stor@angripare.example", DTSTART="20260901T080000Z", DTEND="20260901T090000Z",
                           SUMMARY="Daglig", LOCATION="Rum 1", RRULE="FREQ=DAILY", DESCRIPTION=huge))
    began = time.monotonic()
    result = parse(data)
    elapsed = time.monotonic() - began
    assert len(result.events) == 20  # 1–20 september
    expected = calendar_text.store_body(huge.replace("\\n", "\n"))
    assert all(e["body_preview"] == expected for e in result.events)
    assert len(expected) == calendar_text.BODY_STORE_LIMIT
    assert elapsed < 1.5  # före rättningen drygt 3 s


def test_store_body_unchanged_for_long_blank_preamble():
    text = "\n" * 10_000 + "  \t" * 3000 + "Offert till kunden\n\nPlan"
    assert calendar_text.store_body(text) == "Offert till kunden\n\nPlan"
    assert ics._text({"SUMMARY": " " * 7000 + "Kundbesök"}, "SUMMARY", 500) == "Kundbesök"


# ----------------------------------------------------------------------------- lucka1-…-1 och -2


def test_learned_domain_rejects_second_level_suffix():
    events = [{"attendees": [person("X", "x@co.uk", "organizer"), person("Ägaren", "agaren@gmail.com")]}]
    assert calendar_text.learn_owner(events)[1] is None
    events = [{"attendees": [person("X", "x@com.au", "organizer"), person("Ägaren", "agaren@gmail.com")]}]
    assert calendar_text.learn_owner(events)[1] is None


def test_cancelled_invites_do_not_teach_owner_or_domain():
    events = [
        {"attendees": [person("Ägaren", "agaren@gmail.com", "organizer"), person("Vän", "van@gmail.com")]},
        {"attendees": [person("Ägaren", "agaren@gmail.com", "organizer"), person("Syster", "syster@icloud.com")]},
        {"attendees": [person("Lennart", "lennart@angripare-ab.se", "organizer"),
                       person("Ägaren", "agaren@gmail.com")], "is_cancelled": True},
    ]
    assert calendar_text.learn_owner(events) == ("agaren@gmail.com", None)


def test_tied_self_email_is_not_guessed():
    events = [{"attendees": [person("Lennart", "lennart@angripare-ab.se", "organizer"),
                             person("Ägaren", "agaren@exempelakeri.se")]}]
    self_email, _ = calendar_text.learn_owner(events)
    assert self_email is None
    # Lika antal inom samma domän: domänen är ändå säker.
    events = [{"attendees": [person("Testa", "testa@exempelakeri.se", "organizer"),
                             person("Kim", "kim@exempelakeri.se")]}]
    assert calendar_text.learn_owner(events) == (None, "exempelakeri.se")


def test_ordinary_calendars_learn_exactly_as_before():
    owner = person("Testa", "testa@exempelakeri.se")
    events = [
        {"attendees": [{**owner, "role": "organizer"}, person("Kim", "kim@kund.example.com")]},
        {"attendees": [owner, person("Lennart", "lennart@exempelakeri.se", "organizer")]},
        {"attendees": [owner, person("Sara", "sara@kund.example.com", "organizer")]},
    ]
    assert calendar_text.learn_owner(events) == ("testa@exempelakeri.se", "exempelakeri.se")
    gmail = person("Ägaren", "agaren@gmail.com")
    events = [
        {"attendees": [gmail, person("Chef", "chef@riktigtjobb.se", "organizer")]},
        {"attendees": [gmail, person("Kollega", "kollega@riktigtjobb.se", "organizer")]},
        {"attendees": [gmail, person("Chef", "chef@riktigtjobb.se", "organizer")], "is_cancelled": False},
    ]
    assert calendar_text.learn_owner(events) == ("agaren@gmail.com", "riktigtjobb.se")


def test_user_set_work_domain_is_never_overwritten(db, user):
    from app.services import integrations
    from app.services import settings as settings_svc

    settings_svc.update_section(db, user.id, "calendar", {"work_domain": "minfirma.example"})
    conn = integrations.get_or_create(db, user.id, ics.PROVIDER)
    events = [{"attendees": [person("Lennart", "lennart@angripare-ab.se", "organizer"),
                             person("Ägaren", "agaren@gmail.com")]}] * 3
    ics.learn_owner(db, user.id, conn, events)
    assert settings_svc.get_section(db, user.id, "calendar")["work_domain"] == "minfirma.example"
