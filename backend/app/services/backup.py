"""Säkerhetskopiering och återställning på applikationsnivå.

Kopian är en zip-fil med en JSON-rad per databasrad (tabell för tabell) samt de
uppladdade originalfilerna. Hemligheter ligger kvar krypterade – samma
DRIVELOG_ENCRYPTION_KEYS krävs för att kunna använda dem efter återställning.

För drift rekommenderas dessutom ``deploy/backup.sh`` (pg_dump), se docs/backup.md.
I enprocessläget (SQLite) tar ``sqlite_file_backup`` en konsekvent kopia av själva
databasfilen medan appen kör (``python -m app.cli sqlite-backup <målfil>``).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Date, Uuid, delete, func, insert, select
from sqlalchemy.orm import Session

import app.models  # noqa: F401 - säkerställer komplett metadata
from app import __version__
from app.config import get_config
from app.db import SQLITE_BUSY_TIMEOUT_SECONDS, Base, UTCDateTime, sqlite_file_path

FORMAT_VERSION = 1
_NAME = re.compile(r"^drivelog-backup-\d{8}T\d{6}Z(-[a-z0-9]{6})?\.zip$")
# Kortlivade tabeller som inte ska följa med.
SKIP_TABLES = {"auth_sessions", "oauth_states"}


class BackupError(Exception):
    pass


def _encode(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat() if value.tzinfo else value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _decode(column, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(column.type, UTCDateTime):
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    if isinstance(column.type, Uuid):
        return uuid.UUID(value)
    if isinstance(column.type, Date):
        return date.fromisoformat(value)
    return value


def create_backup(db: Session, *, include_files: bool = True) -> Path:
    cfg = get_config()
    cfg.backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = cfg.backups_dir / f"drivelog-backup-{stamp}-{uuid.uuid4().hex[:6]}.zip"
    counts: dict[str, int] = {}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for table in Base.metadata.sorted_tables:
            if table.name in SKIP_TABLES:
                continue
            lines = []
            for row in db.execute(select(table)).mappings():
                lines.append(json.dumps({k: _encode(v) for k, v in row.items()}, ensure_ascii=False, default=str))
            counts[table.name] = len(lines)
            zf.writestr(f"tables/{table.name}.jsonl", "\n".join(lines))
        files = 0
        if include_files and cfg.uploads_dir.exists():
            for f in sorted(cfg.uploads_dir.iterdir()):
                if f.is_file():
                    zf.write(f, f"files/imports/{f.name}")
                    files += 1
        zf.writestr(
            "manifest.json",
            json.dumps(
                {"format": FORMAT_VERSION, "app_version": __version__, "created_at": datetime.now(UTC).isoformat(),
                 "tables": counts, "files": files,
                 "note": "Hemligheter är krypterade; samma DRIVELOG_ENCRYPTION_KEYS krävs vid återställning."},
                ensure_ascii=False, indent=1,
            ),
        )
    path.chmod(0o600)
    return path


def list_backups() -> list[dict[str, Any]]:
    cfg = get_config()
    if not cfg.backups_dir.exists():
        return []
    out = []
    for f in sorted(cfg.backups_dir.iterdir(), reverse=True):
        if f.is_file() and _NAME.match(f.name):
            st = f.stat()
            out.append({"name": f.name, "size_bytes": st.st_size, "created_at": datetime.fromtimestamp(st.st_mtime, UTC)})
    return out


def backup_path(name: str) -> Path:
    """Sökväg till en kopia; namnet valideras strikt (ingen sökvägsmanipulation)."""
    if not _NAME.match(name):
        raise BackupError("Ogiltigt filnamn.")
    path = (get_config().backups_dir / name).resolve()
    if path.parent != get_config().backups_dir.resolve() or not path.is_file():
        raise BackupError("Säkerhetskopian hittades inte.")
    return path


def delete_backup(name: str) -> None:
    backup_path(name).unlink()


def read_manifest(path: Path) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(path) as zf:
            manifest = json.loads(zf.read("manifest.json"))
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError) as exc:
        raise BackupError("Filen är inte en giltig DriveLog-säkerhetskopia.") from exc
    if manifest.get("format") != FORMAT_VERSION:
        raise BackupError("Säkerhetskopian har ett format som den här versionen inte kan läsa.")
    return manifest


def restore_backup(db: Session, path: Path, *, force: bool = False) -> dict[str, int]:
    """Återställer en kopia. Kräver tom databas om inte ``force`` anges (då töms den först)."""
    cfg = get_config()
    manifest = read_manifest(path)
    tables = [t for t in Base.metadata.sorted_tables]
    non_empty = [t.name for t in tables if (db.scalar(select(func.count()).select_from(t)) or 0) > 0]
    if non_empty and not force:
        raise BackupError(
            "Databasen innehåller redan data (" + ", ".join(non_empty[:5]) + " …). Använd --force för att ersätta allt."
        )
    for t in reversed(tables):
        db.execute(delete(t))
    restored: dict[str, int] = {}
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        for t in tables:
            member = f"tables/{t.name}.jsonl"
            if member not in names:
                continue
            rows = []
            for line in zf.read(member).decode("utf-8").splitlines():
                if not line.strip():
                    continue
                raw = json.loads(line)
                rows.append({c.name: _decode(c, raw.get(c.name)) for c in t.columns if c.name in raw})
            # Självrefererande tabeller (t.ex. logical_trips.merged_into_id) kräver två steg.
            self_refs = [c.name for c in t.columns if any(fk.column.table is t for fk in c.foreign_keys)]
            deferred: list[tuple[Any, dict[str, Any]]] = []
            if self_refs and rows:
                pk = next(iter(t.primary_key.columns)).name
                for r in rows:
                    refs = {c: r[c] for c in self_refs if r.get(c) is not None}
                    if refs:
                        deferred.append((r[pk], refs))
                        for c in refs:
                            r[c] = None
            for i in range(0, len(rows), 500):
                db.execute(insert(t), rows[i : i + 500])
            for pk_value, refs in deferred:
                db.execute(t.update().where(next(iter(t.primary_key.columns)) == pk_value).values(**refs))
            restored[t.name] = len(rows)
        cfg.uploads_dir.mkdir(parents=True, exist_ok=True)
        for member in names:
            if member.startswith("files/imports/") and not member.endswith("/"):
                name = Path(member).name
                if re.fullmatch(r"[0-9a-f]{64}\.(xlsx|csv|txt|bin)", name):  # endast förväntade filnamn
                    target = cfg.uploads_dir / name
                    target.write_bytes(zf.read(member))
                    target.chmod(0o600)
    db.flush()
    expected = manifest.get("tables", {})
    mismatched = {k: (v, restored.get(k, 0)) for k, v in expected.items() if restored.get(k, 0) != v}
    if mismatched:
        raise BackupError(f"Återställningen stämmer inte med manifestet: {mismatched}")
    return restored


def sqlite_file_backup(target: Path, *, database_url: str | None = None) -> Path:
    """Konsekvent kopia av SQLite-databasfilen, tagen med SQLites backup-API.

    Fungerar medan appen kör (WAL): kopian är en ögonblicksbild där alla genomförda
    transaktioner – även de som ännu bara finns i WAL-filen – ingår. Kopian skrivs först
    till en temporär fil bredvid målet, kontrolleras och flyttas sedan på plats, så att
    ``target`` aldrig blir en halvfärdig fil. Kopian är en fristående fil (inte WAL-läge)
    med rättigheterna 600.
    """
    url = database_url or get_config().database_url
    source_path = sqlite_file_path(url)
    if source_path is None:
        raise BackupError("sqlite-backup kräver en filbaserad SQLite-databas (DRIVELOG_DATABASE_URL=sqlite:///…).")
    if not source_path.is_file():
        raise BackupError(f"Databasfilen {source_path} finns inte.")
    target = Path(target)
    if target.resolve() == source_path.resolve():
        raise BackupError("Målfilen får inte vara själva databasfilen.")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex[:8]}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    try:
        # Vanlig anslutning (backup-API:t läser bara); skrivskyddat läge fungerar inte
        # alltid med WAL-filer.
        src = sqlite3.connect(source_path, timeout=SQLITE_BUSY_TIMEOUT_SECONDS)
        try:
            dst = sqlite3.connect(tmp)
            try:
                src.backup(dst)
                dst.execute("PRAGMA journal_mode=DELETE")
                result = dst.execute("PRAGMA quick_check").fetchone()
                if not result or result[0] != "ok":
                    raise BackupError("Kopian klarade inte SQLites integritetskontroll.")
            finally:
                dst.close()
        finally:
            src.close()
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except sqlite3.Error as exc:
        raise BackupError(f"Kopieringen misslyckades: {exc}") from exc
    finally:
        if tmp.exists():
            tmp.unlink()
    return target
