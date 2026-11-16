"""DEL 11 (1.3.0): företagslogga i underlaget.

Uppladdning av PNG och JPEG; SVG, HTML, för stor fil och fel innehåll avvisas; loggan finns i den
skapade filen och saknas när den är borttagen eller avslagen; en annan användare kan inte hämta
den; filen följer med i säkerhetskopian.
"""

from __future__ import annotations

import base64
import struct
import zipfile
import zlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

import app.models as M
from app.config import get_config
from app.models.enums import Category
from app.security import passwords
from app.services import audit, backup
from app.services import settings as settings_svc
from app.services.export import logo as logo_svc
from app.services.export import service as export_svc
from app.services.export import simple
from tests.conftest import TEST_PASSWORD

SEPT_START, SEPT_END = datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC)
NOW = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDABALDA4MChAODQ4SERATGCgaGBYWGDEjJR0oOjM9PDkzODdASFxOQERXRTc4UG1RV19iZ2hnPk1xeXBk"
    "eFxlZ2P/2wBDARESEhgVGC8aGi9jQjhCY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2P/wAARCAAEAAgD"
    "ASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKB"
    "kaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZ"
    "mqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQF"
    "BgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5"
    "OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX"
    "2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDHooor0DhP/9k="
)


def png(width: int = 300, height: int = 100) -> bytes:
    """En riktig (enfärgad) PNG, byggd utan bildbibliotek."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + b"\x2a\x5a\x8c" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def upload(client, data: bytes, name: str = "logga.png", content_type: str = "image/png"):
    return client.post("/api/v1/settings/logo", files={"file": (name, data, content_type)})


def work_trip(trips):
    trips.trip("Kontorsgatan 1, Stad", "Kundvägen 2, Stad", category=Category.WORK, purpose="Kundbesök",
               start_at=datetime(2026, 9, 15, 7, 0, tzinfo=UTC))


def media_in(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as zf:
        return [n for n in zf.namelist() if n.startswith("xl/media/")]


def make_export(db, user, vehicle) -> M.ExportJob:
    return export_svc.create_export(db, user_id=user.id, vehicle=vehicle, start=SEPT_START, end=SEPT_END,
                                    actor=audit.user_actor(user.id), now=NOW, layout="simple")


# ----------------------------------------------------------------------------- uppladdning


@pytest.mark.parametrize("data,content_type,size", [(png(300, 100), "image/png", (300, 100)),
                                                    (JPEG, "image/jpeg", (8, 4))])
def test_upload_png_and_jpeg(client, db, user, data, content_type, size):
    resp = upload(client, data, content_type="application/octet-stream")  # typen avgörs av innehållet
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["has_logo"] is True and out["content_type"] == content_type
    assert (out["width"], out["height"]) == size and out["size_bytes"] == len(data)
    assert out["show_in_export"] is True
    got = client.get("/api/v1/settings/logo")
    assert got.status_code == 200 and got.content == data
    assert got.headers["content-type"] == content_type and got.headers["cache-control"] == "no-store"
    files = list((get_config().data_dir / "branding").iterdir())
    assert len(files) == 1 and files[0].stat().st_mode & 0o777 == 0o600
    assert "logga.png" not in files[0].name
    meta = client.get("/api/v1/settings").json()["settings"]["export"]["logo"]
    assert meta["content_type"] == content_type


@pytest.mark.parametrize(
    "data,name",
    [
        (b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', "logga.svg"),
        (b"<!doctype html><html><body>hej</body></html>", "logga.png"),
        (b"GIF89a\x01\x00\x01\x00", "logga.gif"),
        (b"\x89PNG\r\n\x1a\nnot really a png", "logga.png"),
        (b"", "logga.png"),
    ],
)
def test_wrong_content_is_rejected(client, data, name):
    resp = upload(client, data, name=name)
    assert resp.status_code == 400 and resp.json()["code"] == "invalid_logo"
    assert client.get("/api/v1/settings/logo").status_code == 404


def test_too_large_file_and_too_large_image_are_rejected(client):
    big = png(10, 10) + b"\x00" * (logo_svc.MAX_BYTES + 1)
    resp = upload(client, big)
    assert resp.status_code == 400 and resp.json()["detail"] == "Loggan får vara högst 1 MB."
    huge = png(1, 1)[:16] + struct.pack(">II", 5000, 100) + png(1, 1)[24:]
    resp = upload(client, huge)
    assert resp.status_code == 400 and "4000 × 4000" in resp.json()["detail"]


def test_replacing_removes_the_old_file_and_delete_removes_the_logo(client):
    upload(client, png(300, 100))
    upload(client, JPEG, name="ny.jpg", content_type="image/jpeg")
    folder = get_config().data_dir / "branding"
    assert [p.suffix for p in folder.iterdir()] == [".jpg"]
    out = client.delete("/api/v1/settings/logo").json()
    assert out["has_logo"] is False and list(folder.iterdir()) == []
    assert client.get("/api/v1/settings/logo").status_code == 404


def test_logo_is_read_only_in_settings(client):
    resp = client.patch("/api/v1/settings/export", json={"logo": {"file": "../../etc/passwd.png"}})
    assert resp.status_code == 400


def test_other_user_cannot_get_the_logo(client, db, engine):
    from fastapi.testclient import TestClient

    from app.main import create_app

    upload(client, png())
    other = M.User(email="annan@example.com", display_name="Annan", password_hash=passwords.hash_password(TEST_PASSWORD))
    db.add(other)
    db.commit()
    with TestClient(create_app()) as c:
        assert c.post("/api/v1/auth/login", json={"email": other.email, "password": TEST_PASSWORD}).status_code == 200
        assert c.get("/api/v1/settings/logo").status_code == 404
    assert client.get("/api/v1/settings/logo").status_code == 200


def test_logo_requires_login(anon_client):
    assert anon_client.get("/api/v1/settings/logo").status_code == 401


# ----------------------------------------------------------------------------- i underlaget


def test_logo_is_in_the_created_file_and_gone_when_removed_or_switched_off(db, user, vehicle, trips):
    work_trip(trips)
    without = make_export(db, user, vehicle)
    assert media_in(Path(without.file_path)) == []

    logo_svc.save(db, user.id, png(600, 200))
    with_logo = make_export(db, user, vehicle)
    assert media_in(Path(with_logo.file_path)) == ["xl/media/image1.png"]
    assert with_logo.rows_fingerprint == without.rows_fingerprint  # raderna är desamma
    assert with_logo.filename == without.filename

    settings_svc.update_section(db, user.id, "export", {"show_logo": False})
    assert media_in(Path(make_export(db, user, vehicle).file_path)) == []

    settings_svc.update_section(db, user.id, "export", {"show_logo": True})
    logo_svc.delete(db, user.id)
    assert media_in(Path(make_export(db, user, vehicle).file_path)) == []


def test_logo_sits_top_right_in_the_title_row_and_keeps_its_proportions():
    place = simple.logo_placement(600, 200)
    assert place["x_scale"] == place["y_scale"]
    assert 600 * place["x_scale"] <= simple.LOGO_MAX_WIDTH_PX and 200 * place["y_scale"] <= simple.LOGO_MAX_HEIGHT_PX
    table_px = sum(simple._column_px(w) for _, _, w in simple.COLUMNS)
    right_edge = place["x_offset"] + 600 * place["x_scale"]
    assert table_px - 10 <= right_edge <= table_px  # slutar vid tabellens högra kant
    small = simple.logo_placement(8, 4)
    assert small["x_scale"] == 1.0  # små loggor förstoras aldrig


def test_logo_in_the_sheet_is_anchored_in_the_first_row(db, user, vehicle, trips):
    import openpyxl

    work_trip(trips)
    logo_svc.save(db, user.id, png(600, 200))
    job = make_export(db, user, vehicle)
    ws = openpyxl.load_workbook(job.file_path).active
    with zipfile.ZipFile(job.file_path) as zf:
        drawing = zf.read("xl/drawings/drawing1.xml").decode()
    assert "<xdr:row>0</xdr:row>" in drawing  # titelraden
    assert ws.cell(1, 1).value  # titeln står kvar


def test_month_close_file_gets_the_logo_too(db, user, vehicle, trips):
    work_trip(trips)
    logo_svc.save(db, user.id, png())
    job = export_svc.create_export(db, user_id=user.id, vehicle=vehicle, start=SEPT_START, end=SEPT_END,
                                   actor=audit.SYSTEM, now=NOW, layout="simple", origin="month_close")
    assert media_in(Path(job.file_path)) == ["xl/media/image1.png"]


# ----------------------------------------------------------------------------- säkerhetskopian


def test_logo_follows_the_backup(db, user):
    logo_svc.save(db, user.id, png())
    db.commit()
    name = next((get_config().data_dir / "branding").iterdir()).name
    path = backup.create_backup(db)
    with zipfile.ZipFile(path) as zf:
        assert f"files/branding/{name}" in zf.namelist()
    (get_config().data_dir / "branding" / name).unlink()
    backup.restore_backup(db, path, force=True)
    restored = get_config().data_dir / "branding" / name
    assert restored.is_file() and restored.stat().st_mode & 0o777 == 0o600
    assert logo_svc.read(db, user.id) is not None
