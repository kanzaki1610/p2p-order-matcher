import asyncio
from decimal import Decimal

import httpx
import pytest
from fastapi import HTTPException
from unittest.mock import AsyncMock

from app.okx_ads import read_ad, price_preview
from app.dashboard import AdPricePreview, preview_okx_ad, inspect_okx_ad
from app.config import settings


def ad(side="sell"):
    return {"adId": "12345", "side": side, "isOwner": True, "cryptoCurrency": "usdt",
        "fiatCurrency": "vnd", "type": "limit", "unitPrice": "26080", "status": "new", "isHidden": False}


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_both_sides_prepare_only_price_without_other_ad_changes(side):
    row = ad(side.lower())
    result = price_preview(row, side, Decimal("26079"), Decimal("26000"), Decimal("26100"))
    assert result["request_body"] == {"adId": "12345", "unitPrice": "26079"}
    assert result["live_action_performed"] is False
    assert "newAdId" in result["note"]


@pytest.mark.parametrize("field,value", [("isOwner", False), ("side", "buy"), ("type", "floating_market"),
    ("status", "canceled"), ("isHidden", True)])
def test_unsafe_ad_preview_blocked(field, value):
    row = ad(); row[field] = value
    with pytest.raises(ValueError):
        price_preview(row, "SELL", "26079", "26000", "26100")


@pytest.mark.parametrize("price,lower,upper", [("25999", "26000", "26100"), ("26101", "26000", "26100"),
    ("26079", "0", "26100"), ("NaN", "26000", "26100"), ("26079.5", "26000", "26100")])
def test_price_limits_required(price, lower, upper):
    with pytest.raises(ValueError):
        price_preview(ad(), "SELL", price, lower, upper)


def test_authenticated_read_uses_get_only(monkeypatch):
    from app import okx_ads
    call = AsyncMock(return_value=ad())
    monkeypatch.setattr(okx_ads, "request", call)
    result = asyncio.run(read_ad(None, "12345"))
    assert result["adId"] == "12345"
    assert call.call_args.args == (None, "GET", "/api/v5/p2p/ad?adId=12345")


def test_dashboard_preview_never_calls_exchange_write(monkeypatch):
    monkeypatch.setattr(settings, "database_schema", "public")
    monkeypatch.setattr("app.dashboard.read_ad", AsyncMock(return_value=ad()))
    data = asyncio.run(preview_okx_ad("12345", AdPricePreview(side="SELL", price=26079, min_price=26000, max_price=26100)))
    assert data["mode"] == "PREVIEW_ONLY" and data["live_action_performed"] is False


def test_friend_ad_connector_not_enabled(monkeypatch):
    monkeypatch.setattr(settings, "database_schema", "kayhap")
    with pytest.raises(HTTPException) as error:
        asyncio.run(inspect_okx_ad("12345"))
    assert error.value.status_code == 403
