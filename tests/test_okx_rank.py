import asyncio
from decimal import Decimal

import httpx
import pytest

from app.okx_rank import plan_rank_price, read_rank_plan


def market(side, tenth=26060):
    prices = list(range(26100, 26090, -1)) if side == "BUY" else list(range(26040, 26050))
    prices[9] = tenth
    return {"ok": True, "panel": "okx_" + side.lower(), "exchange": "okx", "side": side.lower(),
        "saved_ts": 1000, "age_sec": 2, "cache": "fresh", "error": None,
        "rows": [{"nick": "TEST-" + str(i), "price": price} for i, price in enumerate(prices)]}


def test_buy_tracks_rank_ten_even_when_old_price_would_rank_first():
    result = plan_rank_price(market("BUY", 26020), "BUY", now=1002)
    assert result["action"] == "UPDATE_PRICE" and result["proposed_price"] == "26020"
    assert result["competitor"]["nickname"] == "TEST-9"
    assert result["live_action_performed"] is False


@pytest.mark.parametrize("price,action", [(26059, "HIDE_AD"), (26060, "UPDATE_PRICE"), (26061, "UPDATE_PRICE")])
def test_sell_cost_plus_ten_boundary(price, action):
    result = plan_rank_price(market("SELL", price), "SELL", Decimal("26050"), now=1002)
    assert result["action"] == action and result["minimum_sell_price"] == "26060"
    assert result["live_action_performed"] is False


def test_missing_cost_does_not_hide_or_update():
    result = plan_rank_price(market("SELL"), "SELL", now=1002)
    assert result["action"] == "BLOCKED" and "giá vốn" in result["reason"]


def test_exclude_all_own_ads_without_following_itself():
    data = market("BUY", 26020)
    data["rows"].insert(0, {"nick": " VNTrader ", "price": 99999})
    data["rows"].insert(4, {"nick": "vntrader", "price": 99998})
    result = plan_rank_price(data, "BUY", now=1002)
    assert result["own_ads_excluded"] == 2 and result["proposed_price"] == "26020"


@pytest.mark.parametrize("change", ["stale", "wrong_side", "missing_time", "cache_error", "malformed_price", "too_few"])
def test_bad_source_never_updates_or_hides(change):
    data = market("SELL")
    if change == "stale": data["saved_ts"] = 900
    if change == "wrong_side": data["side"] = "buy"
    if change == "missing_time": del data["saved_ts"]
    if change == "cache_error": data["cache"] = "stale"
    if change == "malformed_price": data["rows"][0]["price"] = "NaN"
    if change == "too_few": data["rows"].pop()
    assert plan_rank_price(data, "SELL", 26050, now=1002)["action"] == "BLOCKED"


def test_source_fetch_has_no_exchange_credentials_or_write():
    calls = []
    def handler(request):
        calls.append(request)
        assert request.method == "GET" and request.url.host == "autop2p.biz"
        assert not any("access" in key.lower() or "api-key" in key.lower() for key in request.headers)
        data = market("BUY")
        import time
        data["saved_ts"] = time.time()
        return httpx.Response(200, json=data)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await read_rank_plan(client, "BUY")
    assert asyncio.run(run())["action"] == "UPDATE_PRICE" and len(calls) == 1


def test_pricing_alert_never_falls_back_to_other_channels(monkeypatch):
    from app.config import settings
    from app.discord_notifications import webhook_url
    monkeypatch.setattr(settings, "discord_webhook_pricing_alerts", "")
    monkeypatch.setattr(settings, "discord_webhook_url", "https://other-account.example")
    assert webhook_url("PRICING_ALERT") == ""


def test_preview_alert_is_labeled_simulation_and_only_for_hide(monkeypatch):
    from app.config import settings
    from app.okx_rank import send_preview_alert
    from unittest.mock import AsyncMock
    monkeypatch.setattr(settings, "discord_webhook_pricing_alerts", "configured-test-hook")
    sender = AsyncMock(return_value=True)
    monkeypatch.setattr("app.discord_notifications.send_discord_message", sender)
    plan = plan_rank_price(market("SELL", 26059), "SELL", 26050, now=1002)
    assert asyncio.run(send_preview_alert(plan))
    event, message = sender.call_args.args
    assert event == "PRICING_ALERT" and "chưa ẩn quảng cáo thật" in message and "26060" in message
    sender.reset_mock()
    plan = plan_rank_price(market("SELL", 26060), "SELL", 26050, now=1002)
    assert not asyncio.run(send_preview_alert(plan)) and sender.call_count == 0
