"""1.3.1, säkerhet: appens kärna – mellanlager, loggar, nycklar.

behorighet-1, -4, -6, -8, hemligheter-1, -2, -3, -4, -n1. Inga riktiga hemligheter, VIN eller
adresser: allt nedan är påhittat.
"""

from __future__ import annotations

import io
import logging

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import Column, Integer, MetaData, String, Table, insert
from sqlalchemy.exc import IntegrityError
from starlette.requests import Request

import app.models as M
from app import db as dbm
from app.config import AppConfig
from app.logging_setup import RedactingFilter, redact
from app.services.export import links

API = "/api/v1"
FAKE_VIN = "YV1ZZ9ZZ0Z1234567"  # påhittat, rätt format
FAKE_TOKEN = "abcdefghijklmnop0123456789"


# ----------------------------------------------------------------------------- behorighet-1


def _no_json(monkeypatch) -> list[int]:
    """Spionerar på Request.json: kroppen får inte tolkas när den är för stor."""
    calls: list[int] = []
    real = Request.json

    async def spy(self):  # type: ignore[no-untyped-def]
        calls.append(1)
        return await real(self)

    monkeypatch.setattr(Request, "json", spy)
    return calls


def test_large_json_body_is_refused_before_it_is_parsed(anon_client, monkeypatch):
    calls = _no_json(monkeypatch)
    body = b"[" + b"{}," * (1024 * 1024) + b"{}]"  # drygt 3 MB
    r = anon_client.post(f"{API}/trips/bulk", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    assert r.json()["code"] == "payload_too_large"
    assert calls == []


def test_large_chunked_json_body_is_refused(anon_client, monkeypatch):
    calls = _no_json(monkeypatch)

    def chunks():
        for _ in range(40):
            yield b"{}," * 30000  # 90 kB per bit, cirka 3,6 MB totalt, utan Content-Length

    r = anon_client.post(f"{API}/auth/login", content=chunks(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    assert calls == []


def test_ordinary_json_calls_and_large_uploads_still_work(client):
    # En stor exportlista (5000 id) ligger långt under taket.
    ids = [f"{i:032x}" for i in range(5000)]
    r = client.post(f"{API}/trips/bulk", json={"trip_ids": ids, "category": "work"})
    assert r.status_code != 413
    # Uppladdningen av gamla körjournaler (multipart) har kvar sitt tak: kroppen på 5 MB når routen.
    big = b"datum;km;syfte\n" * (400 * 1024)  # drygt 5 MB
    r = client.post(f"{API}/imports/preview", files=[("files", ("gammal.csv", io.BytesIO(big), "text/csv"))])
    assert r.status_code != 413


# ----------------------------------------------------------------------------- behorighet-4


def _volvo_connected(db, user) -> None:
    from app.services.volvo import oauth

    conn = oauth.connection(db, user.id, create=True)
    conn.config = {"vehicles": [{"ref": "r1", "model": "Testbil"}], "vehicle_ref": "r1"}
    db.commit()


@pytest.mark.parametrize(("header", "refreshed"), [
    ("cross-site", False),
    ("same-site", True),
    ("same-origin", True),
    ("none", True),
    (None, True),
])
def test_volvo_refresh_from_other_site_is_skipped(client, db, user, monkeypatch, header, refreshed):
    from app.services.volvo import collector

    _volvo_connected(db, user)
    calls: list[int] = []
    monkeypatch.setattr(collector, "discover_vehicles", lambda db_, conn: calls.append(1))
    headers = {"Sec-Fetch-Site": header} if header else {}
    r = client.get(f"{API}/integrations/volvo/vehicles", params={"refresh": "true"}, headers=headers)
    assert r.status_code == 200
    assert r.json() == {"vehicles": [{"ref": "r1", "model": "Testbil"}], "selected_ref": "r1"}
    assert calls == ([1] if refreshed else [])


# ----------------------------------------------------------------------------- behorighet-6


def _job_with_file(db, user, vehicle, downloads: int) -> M.ExportJob:
    from app.config import get_config

    exports_dir = get_config().exports_dir
    exports_dir.mkdir(parents=True, exist_ok=True)
    path = exports_dir / "underlag.xlsx"
    path.write_bytes(b"PK-test")
    job = M.ExportJob(user_id=user.id, vehicle_id=vehicle.id, status="done", filename="Körjournal.xlsx",
                      file_path=str(path), sha256="0" * 64, link_downloads=downloads)
    db.add(job)
    db.commit()
    return job


def test_link_download_counter_is_atomic(anon_client, db, user, vehicle, monkeypatch):
    """Ett samtidigt anrop som läste räknaren innan den sista hämtningen räknades får inte filen."""
    job = _job_with_file(db, user, vehicle, links.LINK_MAX_DOWNLOADS - 1)
    token = f"{job.id.hex}.1900000000.signatur"

    def stale_verify(db_, tok, now):
        found = db_.get(M.ExportJob, job.id)
        # Det andra anropet läste värdet innan det första hann skriva: verify har redan godkänt.
        return found

    monkeypatch.setattr(links, "verify", stale_verify)
    assert anon_client.get(f"{API}/public/exports/{token}").status_code == 200
    db.expire_all()
    assert db.get(M.ExportJob, job.id).link_downloads == links.LINK_MAX_DOWNLOADS
    r = anon_client.get(f"{API}/public/exports/{token}")
    assert r.status_code == 404 and r.json()["code"] == "link_invalid"
    db.expire_all()
    assert db.get(M.ExportJob, job.id).link_downloads == links.LINK_MAX_DOWNLOADS


# ----------------------------------------------------------------------------- behorighet-8


def _prod(**kw) -> AppConfig:
    base = dict(environment="production", secret_key="s" * 40, encryption_keys=Fernet.generate_key().decode(),
                base_url="https://korjournal.example", jobs_eager=True, database_url="sqlite:///./x.sqlite3")
    base.update(kw)
    return AppConfig(_env_file=None, **base)


@pytest.mark.parametrize("origins", ["*", "http://elak.example", "https://app.example, *"])
def test_production_refuses_wildcard_or_plain_http_cors(origins):
    problems = _prod(cors_origins=origins).validate_for_production()
    assert any("DRIVELOG_CORS_ORIGINS" in p for p in problems)


def test_production_accepts_https_cors_and_empty():
    assert _prod(cors_origins="https://app.example").validate_for_production() == []
    assert _prod(cors_origins="").validate_for_production() == []
    # Utvecklingsläget berörs inte.
    assert AppConfig(_env_file=None, cors_origins="*").validate_for_production() == []


# ----------------------------------------------------------------------------- hemligheter-2


@pytest.mark.parametrize("keys", [" , ", " ", "inte-en-nyckel"])
def test_unusable_encryption_keys_warn_but_never_stop_start(keys):
    cfg = _prod(encryption_keys=keys)
    # En installation som startar i dag ska starta likadant: inget nytt stopp, bara en varning.
    assert cfg.validate_for_production() == []
    assert any("DRIVELOG_ENCRYPTION_KEYS" in w for w in cfg.production_warnings())


def test_valid_encryption_keys_give_no_warning():
    two = Fernet.generate_key().decode() + ", " + Fernet.generate_key().decode()
    assert _prod(encryption_keys=two).production_warnings() == []
    assert AppConfig(_env_file=None, encryption_keys="").production_warnings() == []  # utveckling


# ----------------------------------------------------------------------------- hemligheter-1


def _logged(fn) -> str:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    handler.addFilter(RedactingFilter())
    logger = logging.getLogger("drivelog.test.karna")
    logger.propagate = False
    logger.addHandler(handler)
    try:
        fn(logger)
    finally:
        logger.removeHandler(handler)
    return stream.getvalue()


def test_traceback_is_redacted():
    def emit(logger):
        try:
            raise RuntimeError(f'[parameters: ("{FAKE_VIN}",)] Authorization: Bearer {FAKE_TOKEN}')
        except RuntimeError:
            logger.exception("Jobbet %s misslyckades", "tick")

    out = _logged(emit)
    assert "Traceback" in out and "Jobbet tick misslyckades" in out
    assert FAKE_VIN not in out and FAKE_TOKEN not in out
    assert "[VIN …4567]" in out


def test_stack_info_is_redacted():
    out = _logged(lambda logger: logger.warning("hej %s", "du", stack_info=True, extra={}))
    assert "Stack (most recent call last)" in out
    # Stackinformationen går genom samma maskering (en hemlighet i en rad skulle döljas).
    rec = logging.LogRecord("x", logging.WARNING, __file__, 1, "ok", None, None, sinfo=f"rad med {FAKE_VIN}")
    RedactingFilter().filter(rec)
    assert FAKE_VIN not in rec.stack_info


# ----------------------------------------------------------------------------- hemligheter-3


@pytest.mark.parametrize("url", [
    f"https://p42-caldav.icloud.example/published/2/{FAKE_TOKEN}",
    f"https://moln.example/remote.php/dav/public-calendars/{FAKE_TOKEN}?export",
    f"https://kalender.example/calendar/ical/x%40example/private-{FAKE_TOKEN}/basic",
])
def test_redact_hides_more_shared_calendar_links(url):
    out = redact(f"hämtar {url} nu")
    assert "[KALENDERLÄNK DOLD]" in out and FAKE_TOKEN not in out


# ----------------------------------------------------------------------------- hemligheter-4


def test_redact_hides_oauth_code_and_state_in_query():
    line = "GET /api/v1/integrations/volvo/callback?code=abc123def456&state=xyz789 HTTP/1.1"
    assert redact(line) == "GET /api/v1/integrations/volvo/callback?code=[DOLD]&state=[DOLD] HTTP/1.1"
    # Felkoder i vanlig text rörs inte.
    assert redact("code=invalid_credentials") == "code=invalid_credentials"


def test_access_log_tuple_keeps_its_length():
    args = ("10.0.0.1:5000", "GET", "/api/v1/calendar/microsoft/callback?code=hemlig-kod-1&state=s1", "1.1", 302)
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d', args, None)
    RedactingFilter().filter(rec)
    assert isinstance(rec.args, tuple) and len(rec.args) == 5
    assert "hemlig-kod-1" not in rec.getMessage() and "code=[DOLD]" in rec.getMessage()


# ----------------------------------------------------------------------------- hemligheter-n1


def test_database_errors_do_not_carry_bound_values():
    engine = dbm._build_engine("sqlite://")
    md = MetaData()
    t = Table("t", md, Column("id", Integer, primary_key=True), Column("vin", String, unique=True))
    md.create_all(engine)
    with engine.begin() as conn:
        conn.execute(insert(t).values(id=1, vin=FAKE_VIN))
    with pytest.raises(IntegrityError) as exc_info, engine.begin() as conn:
        conn.execute(insert(t).values(id=2, vin=FAKE_VIN))
    assert FAKE_VIN not in str(exc_info.value)
    engine.dispose()
