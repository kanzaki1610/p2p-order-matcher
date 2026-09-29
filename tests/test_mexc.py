import asyncio
import hashlib
import hmac
from decimal import Decimal
from urllib.parse import parse_qs

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import Base
from app.mexc import MexcP2PClient
from app.mexc_sync import sync_mexc_orders
from app.models import P2POrder


def test_mexc_signature_is_hmac_sha256_hex():
    client = MexcP2PClient(api_key="key", api_secret="secret")
    query = "advOrderNo=123&recvWindow=10000&timestamp=1700000000000"
    expected = hmac.new(b"secret", query.encode(), hashlib.sha256).hexdigest()
    assert client._signature(query) == expected


def test_mexc_client_uses_read_only_p2p_endpoints():
    requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/paginationV2"):
            return httpx.Response(200, json={"code": 0, "msg": "success", "data": []})
        return httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "success",
                "data": {"advOrderNo": "123", "state": "PAID"},
            },
        )

    client = MexcP2PClient(
        api_key="key",
        api_secret="secret",
        transport=httpx.MockTransport(handler),
    )
    asyncio.run(client.get_orders(start_time=1, end_time=2, side="SELL"))
    asyncio.run(client.get_order_detail("123"))

    assert requests[0].method == "GET"
    assert requests[0].url.path == "/api/v3/fiat/merchant/order/paginationV2"
    assert requests[1].url.path == "/api/v3/fiat/order/detail"
    assert requests[0].headers["X-MEXC-APIKEY"] == "key"
    assert parse_qs(requests[0].url.query.decode())["side"] == ["SELL"]
    assert "orderDealState" not in parse_qs(requests[0].url.query.decode())


class FakeMexcClient:
    def __init__(self, state="PAID"):
        self.state = state

    async def get_orders(self, **kwargs):
        if kwargs.get("maker_view"):
            return [{"advOrderNo": "a1370592216728096768"}]
        return []

    async def get_order_detail(self, order_code):
        return {
            "advOrderNo": order_code,
            "tradableQuantity": "100.5",
            "price": "25800",
            "amount": "2592900",
            "coinName": "USDT",
            "state": self.state,
            "payTimeLimit": 1737880040000,
            "side": "SELL",
            "fiatUnit": "VND",
            "createTime": 1737879140000,
            "confirmPaymentInfo": {"bankName": "VPBank"},
            "userInfo": {"realName": "DUONG DUC HUY"},
        }


def test_sync_mexc_creates_waiting_order_for_sepay_matching(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(settings, "mexc_p2p_enabled", True)
    monkeypatch.setattr(settings, "mexc_p2p_live_writes", False)
    monkeypatch.setattr(settings, "mexc_p2p_incoming_side", "SELL")

    with Session(engine) as db:
        result = asyncio.run(sync_mexc_orders(db, FakeMexcClient()))
        order = db.scalar(select(P2POrder))

    assert result["orders_created"] == 1
    assert result["live_action_performed"] is False
    assert order.order_code == "a1370592216728096768"
    assert order.fiat_amount == Decimal("2592900")
    assert order.crypto_amount == Decimal("100.5")
    assert order.counterparty_name == "DUONG DUC HUY"
    assert order.expected_bank == "VPBANK"
    assert order.payment_note == "96768"
    assert order.status == "WAITING_PAYMENT"


def test_sync_mexc_imports_done_order_for_late_sepay_reconciliation(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(settings, "mexc_p2p_enabled", True)
    monkeypatch.setattr(settings, "mexc_p2p_live_writes", False)
    monkeypatch.setattr(settings, "mexc_p2p_incoming_side", "SELL")

    with Session(engine) as db:
        result = asyncio.run(sync_mexc_orders(db, FakeMexcClient(state="DONE")))
        order = db.scalar(select(P2POrder))

    assert result["orders_received"] == 1
    assert order.status == "WAITING_PAYMENT"
