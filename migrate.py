"""
Database migration runner.

Behavior (post-bundle-3 rewrite):

  - Maintains a `schema_migrations` table tracking which .sql files have
    been applied (filename, checksum, applied_at).
  - Each migration runs in its OWN transaction. On error: rollback that
    migration only, print the full error, sys.exit(1). No silent skip.
  - On success: insert into schema_migrations and commit.
  - If a previously-applied migration's file content has changed
    (checksum mismatch), abort with exit code 2. Edited migrations are
    almost always a mistake — write a new migration instead.
  - Re-running the runner with all migrations already applied is a fast
    no-op (one SELECT, no DDL locks).

Subcommands:
  python migrate.py             run unapplied migrations
  python migrate.py bootstrap   record every .sql as already-applied
                                without running it. Use ONLY on first
                                deploy of this runner against a DB
                                whose schema already matches all files.
  python migrate.py reset       DROP + CREATE schema public. Destructive.
  python migrate.py status      list applied/pending migrations and exit
"""

from pathlib import Path
import hashlib
import sys

from sqlalchemy import create_engine, text
from config import DATABASE_URL


def _get_engine():
    if not DATABASE_URL:
        raise SystemExit("DATABASE_URL is not set")
    return create_engine(DATABASE_URL, future=True)


def _migrations_dir() -> Path:
    return Path(__file__).resolve().parent / "ops" / "compose" / "migrations"


def _sql_files() -> list[Path]:
    return sorted(_migrations_dir().glob("*.sql"))


def _checksum(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _ensure_tracking_table(conn) -> None:
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            filename    TEXT PRIMARY KEY,
            checksum    TEXT NOT NULL,
            applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """))


def _applied_map(conn) -> dict:
    rows = conn.execute(text("SELECT filename, checksum FROM schema_migrations")).all()
    return {r[0]: r[1] for r in rows}


def reset_db():
    """Drop and recreate the public schema. DESTRUCTIVE. Dev/test only."""
    engine = _get_engine()
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    print("database reset complete")


def status():
    engine = _get_engine()
    with engine.begin() as conn:
        _ensure_tracking_table(conn)
        applied = _applied_map(conn)
    files = _sql_files()
    print(f"Migration status — {len(files)} files, {len(applied)} applied")
    for p in files:
        cs = _checksum(p.read_bytes())
        if p.name in applied:
            tag = "ok " if applied[p.name] == cs else "EDITED"
            print(f"  [{tag}] {p.name}")
        else:
            print(f"  [pending] {p.name}")


def bootstrap():
    """Record every existing .sql as applied without running it."""
    engine = _get_engine()
    files = _sql_files()
    with engine.begin() as conn:
        _ensure_tracking_table(conn)
        applied = _applied_map(conn)
        added = 0
        for p in files:
            if p.name in applied:
                continue
            cs = _checksum(p.read_bytes())
            conn.execute(
                text("INSERT INTO schema_migrations (filename, checksum) "
                     "VALUES (:f, :c)"),
                {"f": p.name, "c": cs},
            )
            added += 1
            print(f"  bootstrap-recorded: {p.name}")
    print(f"bootstrap complete — {added} newly recorded, "
          f"{len(applied)} already present")


def run_migrations():
    engine = _get_engine()
    files = _sql_files()

    with engine.begin() as conn:
        _ensure_tracking_table(conn)
        applied = _applied_map(conn)

    if not files:
        print("no migrations found")
        return

    edited = []
    for p in files:
        cs = _checksum(p.read_bytes())
        if p.name in applied and applied[p.name] != cs:
            edited.append((p.name, applied[p.name][:12], cs[:12]))
    if edited:
        print("ERROR: previously-applied migrations have been edited:")
        for name, was, now in edited:
            print(f"  {name}: stored={was} current={now}")
        print("Write a new migration instead. Aborting.")
        sys.exit(2)

    pending = [p for p in files if p.name not in applied]
    if not pending:
        print(f"no pending migrations ({len(applied)} already applied)")
        return

    print(f"applying {len(pending)} pending migration(s)...")
    for p in pending:
        sql = p.read_text(encoding="utf-8").strip()
        cs = _checksum(p.read_bytes())
        if not sql:
            with engine.begin() as conn:
                conn.execute(
                    text("INSERT INTO schema_migrations (filename, checksum) "
                         "VALUES (:f, :c)"),
                    {"f": p.name, "c": cs},
                )
            print(f"  applied (empty): {p.name}")
            continue
        try:
            with engine.begin() as conn:
                conn.execute(text(sql))
                conn.execute(
                    text("INSERT INTO schema_migrations (filename, checksum) "
                         "VALUES (:f, :c)"),
                    {"f": p.name, "c": cs},
                )
            print(f"  applied: {p.name}")
        except Exception as e:
            print(f"  FAILED: {p.name}")
            print(f"    {type(e).__name__}: {e}")
            sys.exit(1)


def main():
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "reset":
            reset_db(); return
        if cmd == "bootstrap":
            bootstrap(); return
        if cmd == "status":
            status(); return
        print(f"unknown command: {cmd}")
        print(__doc__)
        sys.exit(2)
    run_migrations()


if __name__ == "__main__":
    main()
