"""Kommandoradsverktyg: python -m app.cli <kommando>."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

from sqlalchemy import select

from app.db import session_scope


def cmd_create_admin(args: argparse.Namespace) -> int:
    from app.models import User
    from app.security import passwords, sessions
    from app.services import audit, vehicles

    email = args.email.strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@") or " " in email:
        print(f"'{email}' är inte en giltig e-postadress.", file=sys.stderr)
        return 1
    # Lösenordet kan ges via miljövariabel (automatiserad installation, Home Assistant-tillägget)
    # så att det inte syns i processlistan; annars frågar kommandot efter det.
    password = args.password or os.environ.get("DRIVELOG_ADMIN_PASSWORD") or getpass.getpass("Lösenord: ")
    problem = passwords.validate_password_strength(password)
    if problem:
        print(problem, file=sys.stderr)
        return 1
    with session_scope() as db:
        user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
        created = user is None
        if user is None:
            user = User(email=email, display_name=email.split("@")[0], password_hash="", is_admin=True)
            db.add(user)
        user.password_hash = passwords.hash_password(password)
        user.is_active = True
        db.flush()
        if not created:
            # Som vid lösenordsbyte i webbappen: alla inloggade sessioner för kontot avslutas.
            sessions.destroy_all_for_user(db, user.id)
        audit.record(db, audit.SYSTEM, "auth.admin_created" if created else "auth.password_reset", "user", user.id)
        vehicles.default_vehicle(db, user.id)
        vehicles.default_driver(db, user.id, user.display_name)
    print(f"Kontot {email} skapades." if created else f"Lösenordet för {email} är återställt.")
    return 0


def cmd_generate_key(_: argparse.Namespace) -> int:
    from app.security import crypto

    print(crypto.generate_key())
    return 0


def cmd_backup(_: argparse.Namespace) -> int:
    from app.services import backup

    with session_scope() as db:
        path = backup.create_backup(db)
    print(path)
    return 0


def cmd_sqlite_backup(args: argparse.Namespace) -> int:
    from app.services import backup

    try:
        path = backup.sqlite_file_backup(Path(args.target))
    except backup.BackupError as exc:
        print(f"Säkerhetskopieringen misslyckades: {exc}", file=sys.stderr)
        return 1
    print(path)
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    from app.services import backup

    try:
        with session_scope() as db:
            restored = backup.restore_backup(db, Path(args.file), force=args.force)
    except backup.BackupError as exc:
        print(f"Återställningen avbröts: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(restored, indent=1))
    return 0


def cmd_openapi(args: argparse.Namespace) -> int:
    from app.main import create_app

    spec = json.dumps(create_app().openapi(), ensure_ascii=False, indent=1)
    if args.output:
        Path(args.output).write_text(spec, encoding="utf-8")
    else:
        print(spec)
    return 0


def cmd_init_db(_: argparse.Namespace) -> int:
    from app.main import init_schema

    init_schema()
    print("Tabellerna är skapade.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="drivelog", description="SMGD DriveLog – administrationsverktyg")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("create-admin", help="Skapa eller återställ ett administratörskonto")
    p.add_argument("email")
    p.add_argument("--password")
    p.set_defaults(func=cmd_create_admin)
    sub.add_parser("generate-key", help="Skapa en krypteringsnyckel (DRIVELOG_ENCRYPTION_KEYS)").set_defaults(func=cmd_generate_key)
    sub.add_parser("backup", help="Skapa en säkerhetskopia (zip)").set_defaults(func=cmd_backup)
    p = sub.add_parser(
        "sqlite-backup", help="Konsekvent kopia av SQLite-databasfilen medan appen kör (enprocessläget)"
    )
    p.add_argument("target", help="Målfil, t.ex. /data/backups/drivelog-db.sqlite3")
    p.set_defaults(func=cmd_sqlite_backup)
    p = sub.add_parser("restore", help="Återställ från en säkerhetskopia")
    p.add_argument("file")
    p.add_argument("--force", action="store_true", help="Ersätt befintliga data")
    p.set_defaults(func=cmd_restore)
    p = sub.add_parser("openapi", help="Skriv ut OpenAPI-specifikationen")
    p.add_argument("--output")
    p.set_defaults(func=cmd_openapi)
    sub.add_parser("init-db", help="Skapa tabeller utan migrationer (utveckling)").set_defaults(func=cmd_init_db)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
