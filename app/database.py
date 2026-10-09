import re

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
if settings.database_schema:
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", settings.database_schema):
        raise ValueError("Invalid account database schema")
    if not settings.database_url.startswith("postgresql"):
        raise ValueError("Account schemas require PostgreSQL")
    # A connection always has exactly one account schema in its search path.
    # Never fall back to public when a tenant schema is missing.
    connect_args["options"] = f"-csearch_path={settings.database_schema}"
engine = create_engine(settings.database_url, connect_args=connect_args)
if settings.database_schema:
    with engine.connect() as connection:
        if connection.scalar(text("SELECT current_schema()")) != settings.database_schema:
            raise RuntimeError("Account database schema is missing or inaccessible")
        if settings.database_schema != "public":
            forbidden = connection.scalar(text("""
                SELECT EXISTS (
                    SELECT 1 FROM pg_tables WHERE schemaname = 'public'
                    AND has_table_privilege(current_user,
                        format('%I.%I', schemaname, tablename), 'SELECT,INSERT,UPDATE,DELETE')
                )
            """))
            if forbidden:
                raise RuntimeError("Account role must not access original account tables")
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
