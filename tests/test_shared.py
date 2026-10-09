import importlib
import sys
import types
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.shared import compose, load_secondary_config, validate_account


def accounts():
    primary = Settings(_env_file=None, database_url="postgresql+psycopg://owner:test@db/main",
                       ingest_api_key="p" * 32, sepay_webhook_secret="q" * 32, dashboard_admin_key="r" * 32)
    secondary = Settings(_env_file=None, database_url="postgresql+psycopg://kayhap:test@db/main",
                         database_schema="kayhap", ingest_api_key="a" * 32,
                         sepay_webhook_secret="b" * 32, dashboard_admin_key="c" * 32,
                         telegram_fallback_enabled=False, telegram_commands_enabled=False,
                         okx_p2p_enabled=False,
                         discord_webhook_orders="https://discord.com/api/webhooks/test/orders",
                         discord_webhook_payment_detected="https://discord.com/api/webhooks/test/payments",
                         discord_webhook_review_required="https://discord.com/api/webhooks/test/review",
                         discord_webhook_confirmed="https://discord.com/api/webhooks/test/confirmed",
                         discord_webhook_system_alerts="https://discord.com/api/webhooks/test/system")
    return primary, secondary


def test_valid_shared_database_configuration():
    validate_account(*accounts())


def test_secondary_mexc_allowed_with_separate_credentials():
    primary, secondary = accounts()
    primary.mexc_p2p_api_key = "owner-mexc"
    secondary.mexc_p2p_enabled = True
    secondary.mexc_p2p_api_key = "friend-mexc"
    secondary.mexc_p2p_api_secret = "friend-secret"
    secondary.mexc_p2p_live_writes = True
    secondary.mexc_auto_release_enabled = True
    validate_account(primary, secondary)
    secondary.mexc_p2p_api_key = primary.mexc_p2p_api_key
    with pytest.raises(ValueError, match="cannot share"):
        validate_account(primary, secondary)


@pytest.mark.parametrize("field,value", [
    ("mexc_p2p_enabled", True),
    ("mexc_auto_release_enabled", True),
    ("okx_auto_release_enabled", True),
])
def test_secondary_exchange_cannot_start_without_its_credentials(field, value):
    primary, secondary = accounts()
    setattr(secondary, field, value)
    with pytest.raises(ValueError):
        validate_account(primary, secondary)


@pytest.mark.parametrize("field,value", [
    ("database_url", "postgresql+psycopg://owner:test@db/main"),
    ("database_url", "postgresql+psycopg://kayhap:test@db/another"),
    ("database_url", "sqlite://"),
    ("database_schema", "public"),
    ("database_schema", "kayhap,public"),
    ("sepay_webhook_secret", "q" * 32),
    ("sepay_webhook_secret", "a" * 32),
    ("dashboard_admin_key", ""),
    ("discord_webhook_review_required", ""),
    ("telegram_fallback_enabled", True),
    ("discord_commands_enabled", True),
    ("okx_p2p_enabled", True),
])
def test_unsafe_configuration_refused(field, value):
    primary, secondary = accounts()
    setattr(secondary, field, value)
    with pytest.raises(ValueError):
        validate_account(primary, secondary)


def test_primary_webhook_and_okx_key_cannot_be_reused():
    primary, secondary = accounts()
    primary.discord_webhook_confirmed = secondary.discord_webhook_orders
    with pytest.raises(ValueError):
        validate_account(primary, secondary)
    primary.discord_webhook_confirmed = ""
    primary.okx_api_key = secondary.okx_api_key = "same-key"
    with pytest.raises(ValueError):
        validate_account(primary, secondary)


def test_secondary_settings_never_inherit_primary_environment(monkeypatch):
    secondary = load_secondary_config()
    config_module = importlib.import_module("app_kayhap.config")
    monkeypatch.setenv("OKX_API_KEY", "PRIMARY-SECRET")
    monkeypatch.setenv("DISCORD_WEBHOOK_ORDERS", "PRIMARY-WEBHOOK")
    monkeypatch.setenv("KAYHAP_OKX_API_KEY", "SECONDARY-SECRET")
    fresh = config_module.Settings(_env_prefix="KAYHAP_", _env_file=None)
    assert fresh.okx_api_key == "SECONDARY-SECRET"
    assert fresh.discord_webhook_orders == ""
    assert secondary is not importlib.import_module("app.config").settings


def test_both_account_lifespans_start_and_stop():
    events = []
    def make_account(name):
        @asynccontextmanager
        async def lifespan(app):
            events.append("start-" + name)
            yield
            events.append("stop-" + name)
        return FastAPI(lifespan=lifespan)
    with TestClient(compose(make_account("original"), make_account("kayhap"))):
        assert events == ["start-original", "start-kayhap"]
    assert events == ["start-original", "start-kayhap", "stop-kayhap", "stop-original"]


def test_real_routes_identical_order_and_bank_ids_are_isolated(monkeypatch):
    """Exercise real matching/routes in two SQL namespaces on one test engine.

    SQLite attached namespaces simulate schema separation. PostgreSQL role and
    search_path enforcement must additionally be verified before deployment.
    """
    import app.main as original
    import app.database as original_database
    from app.models import P2POrder
    primary, secondary = accounts()
    secondary_config = load_secondary_config()
    for name in Settings.model_fields:
        monkeypatch.setattr(secondary_config, name, getattr(secondary, name))
    for key in ("ingest_api_key", "sepay_webhook_secret", "dashboard_admin_key"):
        monkeypatch.setattr(original.settings, key, getattr(primary, key))
    monkeypatch.setattr(original.settings, "okx_p2p_enabled", False)
    monkeypatch.setattr(original.settings, "bitget_p2p_enabled", False)
    monkeypatch.setattr(original.settings, "mexc_p2p_enabled", False)

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    @event.listens_for(engine, "connect")
    def attach(connection, record):
        connection.execute("ATTACH DATABASE ':memory:' AS original")
        connection.execute("ATTACH DATABASE ':memory:' AS kayhap")
    primary_engine = engine.execution_options(schema_translate_map={None: "original"})
    secondary_engine = engine.execution_options(schema_translate_map={None: "kayhap"})
    original_database.Base.metadata.create_all(primary_engine)
    class SecondaryBase(DeclarativeBase):
        pass
    fake = types.ModuleType("app_kayhap.database")
    fake.Base, fake.engine = SecondaryBase, secondary_engine
    fake.SessionLocal = sessionmaker(bind=secondary_engine, expire_on_commit=False)
    fake.ensure_runtime_schema = lambda: None
    def secondary_db():
        with fake.SessionLocal() as db:
            yield db
    fake.get_db = secondary_db
    monkeypatch.setitem(sys.modules, "app_kayhap.database", fake)
    secondary_module = importlib.import_module("app_kayhap.main")
    for exchange in ("okx", "mexc"):
        coordinator = importlib.import_module("app_kayhap." + exchange + "_release")
        reader = importlib.import_module("app_kayhap." + exchange + "_sync")
        assert reader.SessionLocal is fake.SessionLocal
        assert coordinator.Base is SecondaryBase
        assert coordinator.settings is secondary_config
        assert coordinator.P2POrder is not P2POrder
    friend_mexc = importlib.import_module("app_kayhap.mexc")
    owner_mexc = importlib.import_module("app.mexc")
    monkeypatch.setattr(secondary_config, "mexc_p2p_api_key", "FRIEND-MEXC-TEST")
    monkeypatch.setattr(secondary_config, "mexc_p2p_api_secret", "FRIEND-MEXC-SECRET-TEST")
    monkeypatch.setattr(original.settings, "mexc_p2p_api_key", "OWNER-MEXC-TEST")
    assert friend_mexc.MexcP2PClient().credentials.api_key == "FRIEND-MEXC-TEST"
    assert owner_mexc.MexcP2PClient().credentials.api_key == "OWNER-MEXC-TEST"
    primary_factory = sessionmaker(bind=primary_engine, expire_on_commit=False)
    def primary_db():
        with primary_factory() as db:
            yield db
    original.app.dependency_overrides[original_database.get_db] = primary_db
    primary_notice, secondary_notice = AsyncMock(), AsyncMock()
    monkeypatch.setattr(original, "notify_match", primary_notice)
    monkeypatch.setattr(secondary_module, "notify_match", secondary_notice)
    client = TestClient(compose(original.app, secondary_module.app))
    order = {"order_code": "IDENTICAL-001", "fiat_amount": 100000,
             "counterparty_name": "NGUYEN VAN BA", "expected_bank": "OCB"}
    try:
        for prefix, key in [("", primary.ingest_api_key), ("/accounts/kayhap", secondary.ingest_api_key)]:
            assert client.post(prefix + "/orders", json=order, headers={"X-API-Key": key}).status_code == 201
        assert client.get("/accounts/kayhap/orders", headers={"X-API-Key": primary.ingest_api_key}).status_code == 401
        assert client.get("/orders", headers={"X-API-Key": secondary.ingest_api_key}).status_code == 401
        payment = {"id": "SAME-BANK-ID", "gateway": "OCB", "transferAmount": 100000,
                   "content": "BA VAN NGUYEN", "transferType": "in",
                   "transactionDate": datetime.now(timezone.utc).isoformat()}
        assert client.post("/accounts/kayhap/webhooks/sepay", json=payment,
                           headers={"X-API-Key": primary.sepay_webhook_secret}).status_code == 401
        response = client.post("/accounts/kayhap/webhooks/sepay", json=payment,
                               headers={"X-API-Key": secondary.sepay_webhook_secret})
        assert response.json()["decision"] == "AUTO_MATCHED"
        with primary_factory() as db:
            assert db.scalar(select(P2POrder)).status == "WAITING_PAYMENT"
        secondary_notice.assert_awaited_once()
        primary_notice.assert_not_awaited()
        assert client.post("/webhooks/sepay", json=payment,
                           headers={"X-API-Key": primary.sepay_webhook_secret}).json()["decision"] == "AUTO_MATCHED"
        assert client.post("/accounts/kayhap/webhooks/sepay", json=payment,
                           headers={"X-API-Key": secondary.sepay_webhook_secret}).json()["decision"] == "DUPLICATE"
        assert client.get("/accounts/kayhap/", follow_redirects=False).headers["location"] == "/accounts/kayhap/dashboard"
        primary_notice.assert_awaited_once()
    finally:
        original.app.dependency_overrides.pop(original_database.get_db, None)
        engine.dispose()
