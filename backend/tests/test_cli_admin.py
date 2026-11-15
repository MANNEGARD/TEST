"""python -m app.cli create-admin: skapar eller återställer ett konto (används av Home Assistant-tillägget).

Vid återställning avslutas kontots inloggade sessioner – precis som vid lösenordsbyte i webbappen.
"""

from __future__ import annotations

from sqlalchemy import func, select

import app.models as M
from app import cli
from app.security import passwords
from tests.conftest import TEST_PASSWORD

API = "/api/v1"
NYTT = "ett-helt-nytt-losenord-42"


def test_aterstallning_avslutar_sessioner_och_byter_losenord(client, anon_client, db, user, monkeypatch, capsys):
    assert client.get(f"{API}/auth/me").status_code == 200
    monkeypatch.setenv("DRIVELOG_ADMIN_PASSWORD", NYTT)
    assert cli.main(["create-admin", user.email.upper()]) == 0
    utdata = capsys.readouterr()
    assert NYTT not in utdata.out + utdata.err
    assert f"Lösenordet för {user.email} är återställt." in utdata.out
    # Den inloggade sessionen är avslutad.
    assert client.get(f"{API}/auth/me").status_code == 401
    db.expire_all()
    assert passwords.verify_password(db.get(M.User, user.id).password_hash, NYTT)
    assert db.scalar(select(func.count()).select_from(M.AuthSession).where(M.AuthSession.user_id == user.id)) == 0
    assert db.scalar(select(func.count()).select_from(M.AuditEvent).where(M.AuditEvent.action == "auth.password_reset")) == 1
    # Det gamla lösenordet fungerar inte längre, det nya gör det.
    gammalt = anon_client.post(f"{API}/auth/login", json={"email": user.email, "password": TEST_PASSWORD})
    assert gammalt.status_code == 401
    nytt = anon_client.post(f"{API}/auth/login", json={"email": user.email, "password": NYTT})
    assert nytt.status_code == 200


def test_skapar_administrator_om_kontot_saknas(engine, db, monkeypatch, capsys):
    monkeypatch.setenv("DRIVELOG_ADMIN_PASSWORD", NYTT)
    assert cli.main(["create-admin", "Agare@Example.se"]) == 0
    assert "Kontot agare@example.se skapades." in capsys.readouterr().out
    db.expire_all()
    ny = db.execute(select(M.User).where(M.User.email == "agare@example.se")).scalar_one()
    assert ny.is_admin and ny.is_active
    assert passwords.verify_password(ny.password_hash, NYTT)


def test_svagt_losenord_och_ogiltig_adress_avvisas(engine, db, monkeypatch, capsys):
    monkeypatch.setenv("DRIVELOG_ADMIN_PASSWORD", "kort")
    assert cli.main(["create-admin", "agare@example.se"]) == 1
    monkeypatch.setenv("DRIVELOG_ADMIN_PASSWORD", NYTT)
    assert cli.main(["create-admin", "inte-en-adress"]) == 1
    utdata = capsys.readouterr()
    assert "e-postadress" in utdata.err and NYTT not in utdata.err
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(M.User)) == 0
