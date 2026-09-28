import base64
import asyncio
import hashlib
import hmac
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.bitget import BitgetAPIError, BitgetP2PClient, normalize_bitget_offers
from app.bitget_sync import sync_bitget_p2p
from app.config import settings
from app.database import Base
from app.models import ExchangeMarketSnapshot


def test_bitget_signature_uses_timestamp_method_path_and_body():
    client = BitgetP2PClient(api_key="key", api_secret="secret", passphrase="pass")
    expected = base64.b64encode(
        hmac.new(
            b"secret",
            b"1700000000000GET/api/v3/p2p/balance?token=USDT",
            hashlib.sha256,
        ).digest()
    ).decode()
    assert client._signature(
        "1700000000000",
        "GET",
        "/api/v3/p2p/balance?token=USDT",
    ) == expected


def test_get_ad_list_maps_dashboard_side_to_counterparty_ad_side():
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={"code": "00000", "msg": "success", "data": []})

    client = BitgetP2PClient(
        api_key="key",
        api_secret="secret",
        passphrase="pass",
        transport=httpx.MockTransport(handler),
    )
    asyncio.run(client.get_ad_list(dashboard_side="BUY", fiat="VND"))
    asyncio.run(client.get_ad_list(dashboard_side="SELL", fiat="VND"))
    assert seen[0]["side"] == "sell"
    assert seen[1]["side"] == "buy"
    assert seen[0]["fiat"] == "VND"


def test_normalize_bitget_public_ads():
    offers = normalize_bitget_offers(
        [
            {
                "merchantName": "Merchant A",
                "price": "25750",
                "minAmount": "1000000",
                "maxAmount": "50000000",
                "completedOrderNum": "1200",
            }
        ]
    )
    assert offers == [
        {
            "nickname": "Merchant A",
            "price": "25750",
            "min_amount": "1000000",
            "max_amount": "50000000",
            "available_usdt": None,
            "account_days": 0,
            "completed_orders": 1200,
            "total_orders": 1200,
        }
    ]


def test_http_error_exposes_safe_bitget_code_and_message():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"code": "40009", "msg": "sign signature error"},
        )

    client = BitgetP2PClient(
        api_key="key",
        api_secret="secret",
        passphrase="pass",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(BitgetAPIError) as error:
        asyncio.run(client.get_currencies())
    assert str(error.value) == "Bitget HTTP 400 · 40009: sign signature error"
    assert "secret" not in str(error.value)


class FakeBitgetClient:
    async def get_currencies(self):
        return {"fiatDetailList": [{"fiat": "VND"}]}

    async def get_user_info(self):
        return {"accountLevel": "starter", "nickName": "Tester"}

    async def get_balance(self, token):
        return {"availableBalance": "100"}

    async def get_ad_list(self, *, dashboard_side, **kwargs):
        price = "25700" if dashboard_side == "BUY" else "25900"
        return [
            {
                "merchantName": f"{dashboard_side} Merchant",
                "price": price,
                "minAmount": "1000000",
                "maxAmount": "10000000",
                "completedOrderNum": "100",
            }
        ]


def test_sync_stores_buy_and_sell_snapshots(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(settings, "bitget_p2p_enabled", True)
    monkeypatch.setattr(settings, "bitget_p2p_live_writes", False)
    with Session(engine) as db:
        result = asyncio.run(sync_bitget_p2p(db, FakeBitgetClient()))
        snapshots = db.scalars(
            select(ExchangeMarketSnapshot).where(ExchangeMarketSnapshot.exchange == "BITGET")
        ).all()
    assert result["success"] is True
    assert result["live_action_performed"] is False
    assert result["offers"] == {"BUY": 1, "SELL": 1}
    assert {snapshot.side for snapshot in snapshots} == {"BUY", "SELL"}


class SearchOnlyBitgetClient(FakeBitgetClient):
    async def get_currencies(self):
        raise BitgetAPIError(
            "Bitget HTTP 400 · 60051: The current merchant level is not supported."
        )

    async def get_user_info(self):
        raise BitgetAPIError(
            "Bitget HTTP 400 · 60051: The current merchant level is not supported."
        )

    async def get_balance(self, token):
        raise BitgetAPIError(
            "Bitget HTTP 400 · 60051: The current merchant level is not supported."
        )


def test_sync_allows_search_only_key_when_merchant_checks_are_unsupported(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(settings, "bitget_p2p_enabled", True)
    monkeypatch.setattr(settings, "bitget_p2p_live_writes", False)
    with Session(engine) as db:
        result = asyncio.run(sync_bitget_p2p(db, SearchOnlyBitgetClient()))
        snapshots = db.scalars(
            select(ExchangeMarketSnapshot).where(ExchangeMarketSnapshot.exchange == "BITGET")
        ).all()

    assert result["success"] is True
    assert result["market_accessible"] is True
    assert result["offers"] == {"BUY": 1, "SELL": 1}
    assert "60051" in result["optional_checks"]["currencies"]
    assert {snapshot.side for snapshot in snapshots} == {"BUY", "SELL"}
