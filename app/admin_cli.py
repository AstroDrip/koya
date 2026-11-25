"""Offline SQLite migration and local staff session provisioning."""

import argparse
from contextlib import closing
from pathlib import Path
import sqlite3

from .config import get_db_path
from .database import database_ready, migrate_db
from .postgres import database_url
from .staff_auth import issue_owner_session, revoke_owner_sessions


def migrate_with_backup(backup: Path):
    if database_url():
        raise ValueError("Clear MRCARE_DATABASE_URL before upgrading a SQLite copy.")
    source = get_db_path().resolve()
    backup = backup.resolve()
    if not source.is_file():
        raise ValueError("The configured database does not exist.")
    if source == backup:
        raise ValueError("The backup needs a separate path.")
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as current:
        tables = {row[0] for row in current.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if not {"customers", "products", "approved_prices", "service_requests"} <= tables:
            raise ValueError("The database does not contain the expected MR CARE tables.")
        with backup.open("xb"):
            pass
        with closing(sqlite3.connect(backup)) as copy:
            current.backup(copy)
            if copy.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise RuntimeError("The backup failed SQLite integrity verification.")
    migrate_db()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    migrate = commands.add_parser("migrate")
    migrate.add_argument("--backup", type=Path, required=True)
    commands.add_parser("issue-owner-session").add_argument("username")
    commands.add_parser("revoke-owner-sessions").add_argument("username")
    args = parser.parse_args()
    if args.command == "migrate":
        migrate_with_backup(args.backup)
        print("Verified backup created and SQLite migrations completed.")
    elif not database_ready():
        parser.error("The configured database must be initialized and up to date.")
    elif args.command == "issue-owner-session":
        print(issue_owner_session(args.username))
    else:
        revoke_owner_sessions(args.username)


if __name__ == "__main__":
    main()
