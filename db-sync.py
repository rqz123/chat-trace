from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def verify(path: Path) -> None:
    if not path.is_file():
        raise SystemExit(f"Database does not exist: {path}")
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
    finally:
        connection.close()
    if not result or result[0] != "ok":
        raise SystemExit(f"SQLite integrity_check failed: {result!r}")


def backup(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise SystemExit(f"Source database does not exist: {source}")
    if destination.exists():
        destination.unlink()
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
        # A single self-contained file: no -wal/-shm sidecars travel with it.
        destination_connection.execute("PRAGMA journal_mode=DELETE")
    finally:
        destination_connection.close()
        source_connection.close()
    verify(destination)


def schema(path: Path) -> None:
    """Print the database's ChatTrace schema version (1 when unrecorded)."""
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        row = connection.execute("SELECT value FROM app_meta WHERE key='schema_version'").fetchone()
    except sqlite3.OperationalError:
        row = None
    finally:
        connection.close()
    print(int(row[0]) if row else 1)


def code_schema() -> None:
    """Print the schema version this checkout of ChatTrace writes."""
    from chattrace.storage import SCHEMA_VERSION

    print(SCHEMA_VERSION)


def main() -> None:
    parser = argparse.ArgumentParser(description="Safe SQLite backup and verification helper.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup_parser = subparsers.add_parser("backup")
    backup_parser.add_argument("source", type=Path)
    backup_parser.add_argument("destination", type=Path)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("database", type=Path)
    schema_parser = subparsers.add_parser("schema")
    schema_parser.add_argument("database", type=Path)
    subparsers.add_parser("code-schema")
    args = parser.parse_args()
    if args.command == "backup":
        backup(args.source.resolve(), args.destination.resolve())
    elif args.command == "verify":
        verify(args.database.resolve())
    elif args.command == "schema":
        schema(args.database.resolve())
    else:
        code_schema()


if __name__ == "__main__":
    main()
