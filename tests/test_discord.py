import asyncio
import json
import time
from datetime import datetime, timezone
from decimal import Decimal

import httpx
from fastapi.testclient import TestClient
from nacl.signing import SigningKey
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base
from app.models import BankTransaction, P2POrder, OrderConfirmation
from app import discord_commands as commands, discord_notifications as discord, notifications
from app.main import app


def test_webhook_payload_routing_chunks_and_rate_limit(monkeypatch):
    monkeypatch.setattr(settings, "discord_webhook_review_required", "https://discord.com/api/webhooks/123/test_token")
    requests = []
    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, json={"retry_after": 0})
        return httpx.Response(200, json={"id": "1"})
    original = httpx.AsyncClient
    monkeypatch.setattr(discord.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    async def no_sleep(_):
        pass
    monkeypatch.setattr(discord.asyncio, "sleep", no_sleep)
    text = "@everyone ngân hàng " + "😀" * 2100
    assert asyncio.run(discord.send_discord_message("UNMATCHED", text))
    payloads = [json.loads(r.content) for r in requests[1:]]
    assert "".join(p["content"] for p in payloads) == text
    assert all(p["allowed_mentions"] == {"parse": []} for p in payloads)
    assert all(len(p["content"].encode("utf-16-le")) // 2 <= 1900 for p in payloads)
    assert all(r.url.params["wait"] == "true" for r in requests)
    assert not discord.valid_webhook("https://evil.example/api/webhooks/123/token")


def test_fallback_and_disabled_fallback(monkeypatch):
    calls = []
    async def fail(*args):
        return False
    async def telegram(*args):
        calls.append(args)
        return True
    monkeypatch.setattr(notifications, "send_discord_message", fail)
    monkeypatch.setattr(notifications, "send_telegram_message", telegram)
    monkeypatch.setattr(settings, "notification_provider", "discord")
    monkeypatch.setattr(settings, "telegram_fallback_enabled", True)
    assert asyncio.run(notifications.notify_event("CONFIRMED", "test"))
    monkeypatch.setattr(settings, "telegram_fallback_enabled", False)
    assert not asyncio.run(notifications.notify_event("CONFIRMED", "test"))
    assert len(calls) == 1


def test_match_data_and_payment_detected(monkeypatch):
    calls = []
    async def capture(event, text):
        calls.append((event, text))
        return True
    monkeypatch.setattr(notifications, "notify_event", capture)
    tx = BankTransaction(bank="MB", transaction_id="FT123", amount=Decimal("1000"),
        description="NGUYEN VAN A | memo đầy đủ", occurred_at=datetime(2026, 10, 5, 0, tzinfo=timezone.utc))
    order = P2POrder(order_code="P2P12345", status="PAYMENT_DETECTED")
    asyncio.run(notifications.notify_match("AUTO_MATCHED", order, tx, 95, ["Số tiền khớp chính xác"]))
    assert [c[0] for c in calls] == ["AUTO_MATCHED", "PAYMENT_DETECTED"]
    assert "07:00:00 05/10/2026" in calls[0][1]
    assert "P2P12345" in calls[0][1] and "memo đầy đủ" in calls[0][1]


def test_signed_interactions_permissions_confirmation_replay(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'discord.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(commands, "SessionLocal", sessions)
    key = SigningKey.generate()
    for field, value in {"discord_commands_enabled": True, "discord_public_key": key.verify_key.encode().hex(),
                         "discord_application_id": "app", "discord_guild_id": "guild",
                         "discord_command_channel_ids": "channel", "discord_allowed_user_ids": "operator",
                         "discord_allowed_role_ids": ""}.items():
        monkeypatch.setattr(settings, field, value)
    events = []
    async def capture(event, text):
        events.append(event)
        return True
    monkeypatch.setattr(notifications, "notify_event", capture)
    original = httpx.AsyncClient
    monkeypatch.setattr(commands.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(lambda req: httpx.Response(200)), **kw))
    client = TestClient(app)
    def post(payload, timestamp=None):
        raw = json.dumps(payload).encode()
        stamp = str(int(time.time()) if timestamp is None else timestamp)
        sig = key.sign(stamp.encode() + raw).signature.hex()
        return client.post("/webhooks/discord", content=raw, headers={"X-Signature-Timestamp": stamp, "X-Signature-Ed25519": sig})
    assert client.post("/webhooks/discord", json={"type": 1}).status_code == 401
    assert post({"type": 1}).json() == {"type": 1}
    assert post({"type": 1}, timestamp=1).status_code == 401
    payload = {"type": 2, "id": "123", "token": "fake", "application_id": "app", "guild_id": "guild", "channel_id": "channel",
               "member": {"user": {"id": "operator", "username": "test"}, "roles": []},
               "data": {"name": "confirm", "options": [{"name": "order_code", "value": "P2P12345"}]}}
    with sessions() as db:
        db.add(P2POrder(order_code="P2P12345", fiat_amount=1000, counterparty_name="TEST", status="PAYMENT_DETECTED"))
        db.commit()
    denied = dict(payload, guild_id="wrong")
    assert post(denied).json()["type"] == 4
    denied = dict(payload, channel_id="wrong")
    assert post(denied).json()["type"] == 4
    denied = dict(payload, member={"user": {"id": "stranger"}, "roles": []})
    assert post(denied).json()["type"] == 4
    assert post(payload).json()["type"] == 5
    assert post(payload).json()["type"] == 5
    with sessions() as db:
        assert db.scalar(select(P2POrder)).status == "CONFIRMED"
        assert db.scalar(select(OrderConfirmation)).telegram_user_id == "discord:operator"
    assert events == ["CONFIRMED"]
    engine.dispose()


def test_bot_command_mapping():
    assert commands.command_text({"name": "confirm", "options": [{"name": "order_code", "value": "ABC12345"}]}) == "/xacnhan ABC12345"
    assert commands.command_text({"name": "p2p", "options": [{"name": "command", "value": "baocao"}]}) == "/baocao"
