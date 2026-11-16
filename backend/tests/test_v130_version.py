"""DEL 9 (1.3.0): en notis när DriveLog har startat med en ny version, och "Om DriveLog".

Versionsbyte → en notis med de första nyheterna; omstart utan byte → ingen; ny installation →
ingen men värdet sätts; två starter i rad → en notis; notisvalet av eller ingen kanal → ingen
men värdet sätts; ändringsloggen saknas → notis utan nyheter; ett fel stoppar aldrig starten.
"""

# ruff: noqa: F811  (fixturen fake_ha importeras från test_ha_support)

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

import app.models as M
from app.models.enums import NotificationKind, NotificationStatus
from app.services import release
from app.services import settings as settings_svc
from app.services.ha import notices, notifier
from tests.test_ha_support import fake_ha, setup_push  # noqa: F401

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)  # mitt på dagen (inte tysta timmar)

CHANGELOG = """# Ändringar

## 1.3.0

- **Egna gränser för
  sammanslagning.** Text som fortsätter
  på flera rader, med **mer fetstil** längre in.
- **Varning när hämtningen har legat nere.** Text.
- Punkt utan fetstil räknas inte.
- **Ta bort importerade resor.** Text.
- **Månadens underlag i förväg.** Text.

## 1.2.3

- **Gammal nyhet.** Text.
"""


@pytest.fixture()
def changelog(tmp_path, monkeypatch):
    path = tmp_path / "CHANGELOG.md"
    path.write_text(CHANGELOG, encoding="utf-8")
    monkeypatch.setattr(release, "CHANGELOG_PATHS", (tmp_path / "saknas.md", path))
    return path


@pytest.fixture()
def version(monkeypatch):
    def set_version(value: str) -> None:
        monkeypatch.setattr(release, "__version__", value)

    set_version("1.3.0")
    return set_version


@pytest.fixture()
def phone(db, user, fake_ha):
    setup_push(db, user)
    return fake_ha


def set_last(db, user, value: str) -> None:
    settings_svc.update_section(db, user.id, "system", {"last_version": value}, allow_read_only=True)


def last(db, user) -> str:
    db.expire_all()
    return settings_svc.get_section(db, user.id, "system")["last_version"]


def release_rows(db) -> list[M.NotificationRequest]:
    db.expire_all()
    return list(db.execute(
        select(M.NotificationRequest).where(M.NotificationRequest.kind == NotificationKind.RELEASE)
    ).scalars())


def start(db) -> int:
    """Som vid start: allt sparat först, sedan release.on_startup i egna sessioner."""
    db.commit()
    return release.on_startup(now=NOW)


# ----------------------------------------------------------------------------- ändringsloggen


def test_changelog_items_are_the_bold_lead_ins(changelog):
    assert release.changelog_section("1.3.0") == [
        "Egna gränser för sammanslagning",
        "Varning när hämtningen har legat nere",
        "Ta bort importerade resor",
    ]
    assert release.changelog_section("1.3.0", limit=None)[-1] == "Månadens underlag i förväg"
    assert release.changelog_section("1.2.3") == ["Gammal nyhet"]
    assert release.changelog_section("9.9.9") == []


def test_repo_changelog_has_news_for_the_running_version():
    """Ändringsloggen i repot (samma fil som kopieras in i avbilden) har nyheter för versionen."""
    assert release.changelog_section(release.current_version())


# ----------------------------------------------------------------------------- vid start


def test_version_change_gives_one_notice_with_news(db, user, phone, changelog, version):
    set_last(db, user, "1.2.3")
    assert start(db) == 1
    rows = release_rows(db)
    assert len(rows) == 1
    req = rows[0]
    assert req.title == "DriveLog är uppdaterad till 1.3.0"
    assert req.message == (
        "Från 1.2.3. Nytt: Egna gränser för sammanslagning; Varning när hämtningen har legat nere; "
        "Ta bort importerade resor."
    )
    assert req.dedupe_key == "release:1.3.0"
    assert req.actions == [{"key": "OPEN", "title": "Öppna", "uri": "/settings?sektion=om"}]
    assert (req.expires_at - req.created_at).days == 7
    assert last(db, user) == "1.3.0"


def test_notice_reaches_the_phone_with_link_to_about(db, user, phone, changelog, version):
    set_last(db, user, "1.2.3")
    start(db)
    notifier.deliver_user(db, user.id, NOW)
    db.flush()
    sent = [c for c in phone.calls if c[0] == "POST" and "notify" in c[1]]
    assert len(sent) == 1
    data = sent[0][2]
    assert data["title"] == "DriveLog är uppdaterad till 1.3.0"
    assert data["data"]["actions"][0]["uri"].endswith("/settings?sektion=om")
    assert release_rows(db)[0].status == NotificationStatus.SENT


def test_restart_without_change_gives_no_notice(db, user, phone, changelog, version):
    set_last(db, user, "1.3.0")
    assert start(db) == 0
    assert release_rows(db) == []


def test_new_installation_gives_no_notice_but_remembers_version(db, user, phone, changelog, version):
    assert last(db, user) == ""
    assert start(db) == 0
    assert release_rows(db) == []
    assert last(db, user) == "1.3.0"


def test_two_starts_in_a_row_give_one_notice(db, user, phone, changelog, version):
    set_last(db, user, "1.2.3")
    assert start(db) == 1
    assert start(db) == 0
    assert len(release_rows(db)) == 1


def test_redone_upgrade_never_gives_a_second_notice_for_the_same_version(db, user, phone, changelog, version):
    """Uppgraderingen görs om (t.ex. en återställd säkerhetskopia): dedupe-nyckeln stoppar."""
    set_last(db, user, "1.2.3")
    start(db)
    set_last(db, user, "1.2.3")
    assert start(db) == 0
    assert len(release_rows(db)) == 1
    assert last(db, user) == "1.3.0"


def test_next_version_gives_its_own_notice(db, user, phone, changelog, version):
    set_last(db, user, "1.2.3")
    start(db)
    version("1.3.1")
    assert start(db) == 1
    assert sorted(r.dedupe_key for r in release_rows(db)) == ["release:1.3.0", "release:1.3.1"]
    newest = next(r for r in release_rows(db) if r.dedupe_key == "release:1.3.1")
    assert newest.message == "Från 1.3.0."  # inget avsnitt för 1.3.1 i ändringsloggen


def test_release_notices_switched_off_gives_none_but_remembers(db, user, phone, changelog, version):
    settings_svc.update_section(db, user.id, "notifications", {"kinds": {"release": False}})
    set_last(db, user, "1.2.3")
    assert start(db) == 0
    assert release_rows(db) == []
    assert last(db, user) == "1.3.0"


def test_without_home_assistant_channel_no_notice_but_remembers(db, user, changelog, version):
    set_last(db, user, "1.2.3")
    assert start(db) == 0
    assert release_rows(db) == []
    assert last(db, user) == "1.3.0"


def test_missing_changelog_gives_notice_without_news(db, user, phone, tmp_path, monkeypatch, version):
    monkeypatch.setattr(release, "CHANGELOG_PATHS", (tmp_path / "finns-inte.md",))
    set_last(db, user, "1.2.3")
    assert start(db) == 1
    assert release_rows(db)[0].message == "Från 1.2.3."


def test_an_error_never_stops_the_start(db, user, phone, changelog, version, monkeypatch, caplog):
    def broken(*args, **kwargs):
        raise RuntimeError("hemlig text som inte får loggas")

    monkeypatch.setattr(notices, "queue_release", broken)
    set_last(db, user, "1.2.3")
    assert start(db) == 0
    assert "hemlig text" not in caplog.text
    assert last(db, user) == "1.2.3"  # rullades tillbaka: prövas igen vid nästa start


def test_startup_of_the_app_queues_the_notice(engine, db, user, phone, changelog, version):
    """Hela vägen: appens start (lifespan) jämför versionen."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    set_last(db, user, "1.2.3")
    db.commit()
    with TestClient(create_app()) as c:
        assert c.get("/api/v1/health").status_code == 200
    assert [r.dedupe_key for r in release_rows(db)] == ["release:1.3.0"]
    assert last(db, user) == "1.3.0"


# ----------------------------------------------------------------------------- Om DriveLog


def test_about_shows_version_and_whats_new(client, changelog, version):
    data = client.get("/api/v1/about").json()
    assert data["version"] == "1.3.0"
    assert data["whats_new"]["version"] == "1.3.0"
    assert data["whats_new"]["items"][0] == "Egna gränser för sammanslagning"
    assert len(data["whats_new"]["items"]) == 4  # alla nyheter i Om DriveLog, tre i notisen


def test_about_without_changelog_section(client, version):
    version("9.9.9")
    assert client.get("/api/v1/about").json() == {"version": "9.9.9", "whats_new": None}


def test_about_requires_login(anon_client):
    assert anon_client.get("/api/v1/about").status_code == 401
