import json
from typing import Generator, Optional, List
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from .config import DATABASE_URL

_engine = None
_SessionLocal = None

def get_engine():
    global _engine
    if _engine is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is not set")
        _engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
    return _engine

def get_session_factory():
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), autoflush=False, autocommit=False, future=True)
    return _SessionLocal

def get_db() -> Generator[Session, None, None]:
    db = get_session_factory()()
    try:
        yield db
    finally:
        db.close()

def decode_embedding(db: Session, value) -> Optional[List[float]]:
    if value is None: return None
    if db.bind.dialect.name == "sqlite":
        return json.loads(value)
    if isinstance(value, str) and value.strip().startswith("["):
        inner = value.strip()[1:-1].strip()
        return [] if not inner else [float(x) for x in inner.split(",")]
    try:
        return list(value)
    except TypeError:
        return None
