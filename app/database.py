from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def ensure_runtime_schema() -> None:
    """Add backward-compatible columns that create_all cannot add to an existing table."""
    inspector = inspect(engine)
    if "arbitrage_config" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("arbitrage_config")}
    additions = {
        "target_trade_vnd": "NUMERIC(20, 0) NOT NULL DEFAULT 0",
        "min_available_usdt": "NUMERIC(20, 8) NOT NULL DEFAULT 0",
    }
    with engine.begin() as connection:
        for name, definition in additions.items():
            if name not in existing:
                connection.execute(text(f"ALTER TABLE arbitrage_config ADD COLUMN {name} {definition}"))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
