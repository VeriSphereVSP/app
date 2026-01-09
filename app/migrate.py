from pathlib import Path
from sqlalchemy import create_engine, text
from .config import DATABASE_URL

def main():
    if not DATABASE_URL:
        raise SystemExit("DATABASE_URL is not set")
    engine = create_engine(DATABASE_URL, future=True)
    root = Path(__file__).resolve().parents[1]
    mig = root / "ops" / "compose" / "migrations"
    sql_files = sorted(mig.glob("*.sql"))
    with engine.begin() as conn:
        for p in sql_files:
            sql = p.read_text(encoding="utf-8").strip()
            if not sql:
                continue
            try:
                conn.execute(text(sql))
                print("applied", p.name)
            except Exception as e:
                print("warn", p.name, "->", e)
if __name__ == "__main__":
    main()
