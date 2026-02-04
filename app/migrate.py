from pathlib import Path
import sys

from sqlalchemy import create_engine, text
from .config import DATABASE_URL


def _get_engine():
    if not DATABASE_URL:
        raise SystemExit("DATABASE_URL is not set")
    return create_engine(DATABASE_URL, future=True)


def reset_db():
    """
    Drop and recreate the public schema.
    DESTRUCTIVE. Intended for dev/test only.
    """
    engine = _get_engine()
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    print("database reset complete")


def run_migrations():
    engine = _get_engine()

    root = Path(__file__).resolve().parents[1]
    mig = root / "ops" / "compose" / "migrations"
    sql_files = sorted(mig.glob("*.sql"))

    if not sql_files:
        print("no migrations found")
        return

    with engine.begin() as conn:
        for p in sql_files:
            sql = p.read_text(encoding="utf-8").strip()
            if not sql:
                continue
            try:
                conn.execute(text(sql))
                print("applied", p.name)
            except Exception as e:
                # migrations are intentionally idempotent-ish
                print("warn", p.name, "->", e)


def main():
    """
    Usage:
      python -m app.migrate        # run migrations
      python -m app.migrate reset  # DROP + recreate schema, then exit
    """
    if len(sys.argv) > 1 and sys.argv[1] == "reset":
        reset_db()
        return

    run_migrations()


if __name__ == "__main__":
    main()

